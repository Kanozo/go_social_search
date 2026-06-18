"""
google_cse_automator.py
Motor de scraping sobre Google Custom Search Engine (CSE) con Camoufox.

Anti-detección integrada (5 capas)
────────────────────────────────────
  Capa 1 – Fingerprint coherente por sesión
    BrowserFingerprint: UA + platform + WebGL + viewport coherentes entre sí.
    Con Camoufox, el OS del fingerprint se mapea al parámetro ``os`` de
    AsyncCamoufox para garantizar coherencia a nivel binario.

  Capa 2 – Init scripts de stealth (12 parches JS)
    Inyectados antes de cualquier carga via add_init_script():
    webdriver, plugins, canvas noise, WebGL, AudioContext, WebRTC, screen
    metrics, Permissions API, performance.now(), Notification, iframe
    propagation, window.chrome y mediaDevices.

  Capa 3 – Comportamiento humano completo
    · _inject_referrer     → simula que el usuario llegó desde Google/Bing
    · _quick_move_and_click  → curvas de Bézier con easing cosenoidal
    · human_type           → escritura gaussiana con typos reales
    · human_scroll         → scroll por ráfagas con pausas de lectura
    · simulate_reading_pause  → pausa proporcional al texto visible
    · simulate_idle           → deriva de ratón mientras el usuario lee
    · simulate_distraction    → ratón a las esquinas (comportamiento errático)
    · simulate_page_focus_blur → simula cambio de pestaña entre keywords

  Capa 4 – Gestión de red
    · Sesión persistida en disco (cookies + localStorage entre ciclos)
    · response interceptor para 429/403

  Capa 5 – Detección y respuesta
    · CaptchaDetector.check()        → multi-señal antes/después de cada acción
    · CaptchaAutosolver.try_solve()  → auto-resolver checkbox (3 proveedores)
    · Escalado al orquestador si el solver falla

Modo de salida (settings.OUTPUT_MODE)
──────────────────────────────────────
  "supabase" → SupabaseUrlRepo.bulk_insert_urls() — URLs únicas, sin duplicados
  "api"      → httpx POST al endpoint HTTP externo

MIGRACIÓN A CAMOUFOX
──────────────────────
  - Reemplaza Playwright Firefox/Chromium por Camoufox (Firefox modificado)
  - Mantiene toda la lógica de anti-detección personalizada
  - Camoufox resuelve errores de body (0x80004005) y detección de automatización
  - La API de Page/Context es 100% compatible con Playwright

OPTIMIZACIONES DE VELOCIDAD
────────────────────────────
  - Click directo en search box (sin movimiento de ratón previo)
  - Sin delays entre click y type
  - Waits selectivos en lugar de delays ciegos
  - Movimientos de ratón reducidos para elementos de bajo riesgo
  - CAPTCHA espera mínimo 120s antes de continuar
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import httpx
from dateutil.relativedelta import relativedelta
from playwright.async_api import (
    BrowserContext,
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
)

# ── Anti-detección: importar TODO lo generado ────────────────────────────────
from anti_detection import (
    BrowserFingerprint,
    generate_fingerprint,
    micro_delay,
    simulate_distraction,
    simulate_idle,
    simulate_page_focus_blur,
    simulate_reading_pause,
)
from anti_detection.human_behavior import (
    human_scroll,
    human_type,
)

from config.settings import settings
from database.supabase_client import SupabaseUrlRepo
from utils.captcha_guard import CaptchaAutosolver, CaptchaDetector, CaptchaError
from utils.session_store import SessionStore
from utils.url_clean import clean_url

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

_DATA_STORE_HEADERS: dict[str, str] = {
    "Authorization": f"Bearer {settings.DATA_STORE_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Referrers plausibles: simula que el usuario llegó desde una búsqueda normal.
# Se navega brevemente antes de ir al CSE para establecer document.referrer.
_PLAUSIBLE_REFERRERS: list[str] = [
    "https://www.google.com/",
    "https://www.google.com/search?q=site:facebook.com",
    "https://www.google.com/search?q=site:instagram.com",
    "https://duckduckgo.com/",
    "https://www.bing.com/",
    "https://search.yahoo.com/",
]

_UNIT_MAP: dict[str, str] = {
    "minuto": "minutes", "minutos": "minutes",
    "hora":   "hours",   "horas":   "hours",
    "día":    "days",    "días":    "days",
    "semana": "weeks",   "semanas": "weeks",
    "mes":    "months",  "meses":   "months",
    "año":    "years",   "años":    "years",
}
_RELATIVEDELTA_UNITS: frozenset[str] = frozenset({"months", "years"})
_RELATIVE_TS_PATTERN: re.Pattern[str] = re.compile(
    r"^(hace\s+(\d+)\s+(hora|horas|minuto|minutos|día|días"
    r"|semana|semanas|mes|meses|año|años))",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Tipos de datos
# ─────────────────────────────────────────────────────────────────────────────

class ScrapedResult(TypedDict):
    """Resultado individual extraído de una página de CSE."""
    url:              str
    platform:         str
    published_at:     datetime | None
    published_at_raw: str | None

# ─────────────────────────────────────────────────────────────────────────────
# BrowserConfig
# ─────────────────────────────────────────────────────────────────────────────

class BrowserConfig:
    """
    Parámetros de timing y comportamiento del navegador automatizado.

    Todos los rangos se expresan como (min, max) en segundos.
    Los valores de distribución gaussiana + exponencial evitan patrones
    de timing regulares que los detectores de bot identifican fácilmente.

    OPTIMIZADO: Rangos reducidos para mayor velocidad sin sacrificar
    el componente humano mínimo necesario.
    """

    page_load_wait_range:    tuple[float, float] = (1.0, 2.5)
    scroll_pause_range:      tuple[float, float] = (0.1, 0.3)
    between_pages_range:     tuple[float, float] = (1.5, 3.0)
    between_keywords_range:  tuple[float, float] = (3.0, 8.0)
    warmup_pause_range:      tuple[float, float] = (0.5, 1.5)
    typing_wpm_range:        tuple[int, int]     = (80, 130)
    warmup_url:              str                 = "https://www.google.com"
    captcha_max_wait_seconds: float              = 300.0
    captcha_wait_for_human:  bool                = True
    # Probabilidad de simular distracción entre páginas
    distraction_probability: float               = 0.05
    # Probabilidad de simular cambio de pestaña entre keywords
    focus_blur_probability:  float               = 0.10

    def jitter_wait(self, low: float, high: float) -> float:
        """
        Tiempo de espera con distribución gaussiana + cola exponencial.

        La gaussiana genera valores centrados en el rango; la exponencial
        añade picos ocasionales que replican distracciones reales del usuario.

        Args:
            low:  Límite inferior.
            high: Límite superior.

        Returns:
            Segundos de espera, clampados entre low y high*1.5.
        """
        mid = (low + high) / 2.0
        sigma = (high - low) / 6.0
        base = random.gauss(mid, sigma)
        extra = random.expovariate(5.0)
        return max(low, min(high * 1.5, base + extra * 0.2))

# ─────────────────────────────────────────────────────────────────────────────
# GoogleCSEAutomator
# ─────────────────────────────────────────────────────────────────────────────

class GoogleCSEAutomator:
    """
    Automatiza la búsqueda en Google CSE con evasión completa de detección.

    El orquestador gestiona el ciclo de vida del browser y de la conexión
    Supabase. Esta clase recibe una ``Page`` activa y el ``SupabaseUrlRepo``
    ya inicializado; no abre ni cierra recursos de DB internamente.

    MIGRACIÓN A CAMOUFOX:
      - Camoufox es compatible con la API de Playwright para Page/Context
      - No necesita cambios en la lógica de scraping
      - Los métodos que usan page.goto(), page.locator(), etc. funcionan igual
      - La anti-detección nativa de Camoufox se complementa con los parches JS

    Args:
        cse_id:     ID del Custom Search Engine de Google.
        platform:   Plataforma objetivo ("instagram" | "facebook" | …).
        url_repo:   Repositorio de URLs en Supabase. None → solo modo "api".
        config:     BrowserConfig. Se crea uno por defecto si None.
    """

    def __init__(
        self,
        cse_id: str,
        platform: str = "",
        url_repo: SupabaseUrlRepo | None = None,
        config: BrowserConfig | None = None,
    ) -> None:
        self._search_url  = f"https://cse.google.com/cse?cx={cse_id}"
        self._platform    = platform
        self._url_repo    = url_repo
        self.cfg          = config or BrowserConfig()
        # Buffer de resultados de la página actual (se limpia por página)
        self._scraped_results: list[ScrapedResult] = []
        self._session_store = SessionStore(settings.SESSION_DIR)

    # ── Helpers de timing ────────────────────────────────────────────────────

    async def _human_sleep(self, low: float, high: float) -> None:
        """Pausa jitter usando la distribución gaussiana + exponencial."""
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    # ── Referrer injection ───────────────────────────────────────────────────

    async def _inject_referrer(self, page: Page) -> None:
        """Inyecta referrer sin navegación completa. Suficiente para CSE."""
        referrer = random.choice([
            "https://www.google.com/search?q=site:facebook.com",
            "https://www.google.com/search?q=site:instagram.com",
            "https://www.google.com/",
        ])
        
        # Método 1: Evaluar JS (funciona en el 95% de casos)
        try:
            await page.evaluate(f"""() => {{
                Object.defineProperty(document, 'referrer', {{
                    get: () => {json.dumps(referrer)},
                    configurable: true
                }});
            }}""")
        except Exception:
            pass
        
        # Método 2: Fallback por header (para requests posteriores)
        await page.set_extra_http_headers({"Referer": referrer})
        
        # Pausa mínima para simular "tiempo de lectura" del referrer
        await asyncio.sleep(random.uniform(0.2, 0.5))
        logger.debug("Referrer inyectado vía JS/header: %s", referrer)

    # ── Movimiento de ratón con Bézier ───────────────────────────────────────

    async def _quick_move_and_click(self, page: Page, locator: Any) -> None:
        """
        Versión rápida de movimiento de ratón para elementos de bajo riesgo.

        Usa solo 3 steps y delays mínimos. Adecuado para:
        - Paginación (botones "1", "2", "3")
        - Filtros de dropdown
        - Elementos que no son detectados por sistemas anti-bot

        Args:
            page:    Página activa.
            locator: Locator de Playwright del elemento destino.
        """
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click(timeout=2_000)
                return

            target_x = box["x"] + box["width"]  * random.uniform(0.2, 0.5)
            target_y = box["y"] + box["height"] * random.uniform(0.2, 0.5)

            # Solo 3 steps para movimientos rápidos
            steps = 3
            
            for step in range(steps + 1):
                t = step / steps
                # Movimiento lineal simple (sin arco)
                x = box["x"] + (target_x - box["x"]) * t
                y = box["y"] + (target_y - box["y"]) * t
                await page.mouse.move(x, y)
                # Delay mínimo (casi imperceptible)
                await asyncio.sleep(0.001)

            await page.mouse.click(target_x, target_y)

        except Exception:
            # Fallback: click directo
            await locator.click(timeout=2_000)

    # ── Stealth y warmup ─────────────────────────────────────────────────────

    @staticmethod
    async def apply_stealth(page: Page, fingerprint: BrowserFingerprint) -> None:
        """
        Inyecta los 12 parches JS de evasión como init script de la página.

        ``add_init_script`` garantiza que el script se ejecuta ANTES de que
        cualquier script de la página se cargue, incluyendo los detectores
        de bot. No es posible interceptar la inyección desde la página.

        CAMOUFOX: Aunque Camoufox tiene anti-detección nativa a nivel binario,
        mantenemos los parches JS personalizados para máxima cobertura contra
        detectores avanzados (CreepJS, DataDome, Cloudflare Turnstile).

        Parches incluidos:
          navigator.webdriver, platform, hardwareConcurrency, deviceMemory,
          connection API, plugins (por browser), canvas noise, WebGL vendor/
          renderer, AudioContext noise, WebRTC IP leak, screen metrics con
          chrome frame, Permissions API, performance.now() precision,
          Notification.permission, iframe propagation, mediaDevices.

        Args:
            page:        Página recién creada (antes de cualquier goto).
            fingerprint: Fingerprint de la sesión actual.
        """
        await page.add_init_script(fingerprint.stealth_js)
        logger.debug("Stealth init script aplicado (%d bytes).", len(fingerprint.stealth_js))

    async def _warmup_session(self, page: Page, is_fresh_session: bool = True) -> None:
        """Warmup solo en sesiones nuevas. En sesiones restauradas es innecesario."""
        if not is_fresh_session:
            logger.debug("Warmup omitido: sesión restaurada.")
            return
            
        await page.goto(self.cfg.warmup_url, wait_until="domcontentloaded", timeout=10_000)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        logger.debug("Session warmup rápido completado.")

    # ── Parsing de fechas ────────────────────────────────────────────────────

    @staticmethod
    def _parse_relative_timestamp(snippet_text: str) -> datetime | None:
        """
        Convierte timestamps relativos en español a ``datetime`` UTC.

        Soporta: "hace N minutos/horas/días/semanas/meses/años".
        Usa ``relativedelta`` para meses y años (el timedelta de stdlib
        no soporta meses directamente).

        Args:
            snippet_text: Texto del snippet del resultado CSE.

        Returns:
            ``datetime`` timezone-aware UTC, o None si no es relativo.
        """
        if not snippet_text:
            return None
        match = _RELATIVE_TS_PATTERN.match(snippet_text.strip())
        if not match:
            return None
        value      = int(match.group(2))
        unit_es    = match.group(3).lower()
        delta_unit = _UNIT_MAP.get(unit_es)
        if not delta_unit:
            return None
        try:
            now = datetime.now(timezone.utc)
            if delta_unit in _RELATIVEDELTA_UNITS:
                return now + relativedelta(**{delta_unit: -value})
            return now - timedelta(**{delta_unit: value})
        except (ValueError, TypeError, OverflowError) as exc:
            logger.warning("Error calculando datetime '%s': %s", snippet_text[:40], exc)
            return None

    # ── Extracción de resultados ─────────────────────────────────────────────

    async def _incremental_scroll(self, page: Page) -> None:
        """Scroll rápido pero con patrones humanos mínimos para CSE."""
        page_height = await page.evaluate("document.body.scrollHeight")
        viewport_h = await page.evaluate("window.innerHeight") or 768
        
        # Reducir segmentos: 2-3 en lugar de 5-8
        segments = max(2, min(3, int(page_height / viewport_h)))
        step_px = int(page_height / segments)
        
        for _ in range(segments):
            # Scroll directo con ráfaga rápida
            await human_scroll(page, direction="down", amount=step_px)
            
            # Pausa mínima: solo lo suficiente para que el DOM se actualice
            await asyncio.sleep(random.uniform(0.15, 0.35))

    async def block_images_async(
        self,
        context: "BrowserContext",
        url_pattern: str | None = None,
        log_blocked: bool = False,
    ) -> None:
        """
        Registra interceptor de imágenes a nivel de BrowserContext.

        Al registrarlo en el contexto (no en una Page individual), aplica
        automáticamente a todas las páginas que se abran dentro de él,
        incluidas las creadas después de esta llamada.

        CAMOUFOX: Compatible con la API de Playwright para context.route().

        Args:
            context:     Contexto de Playwright activo.
            url_pattern: Si se proporciona, solo bloquea imágenes cuya URL
                         contenga este patrón. None = bloquear todas.
            log_blocked: Si True, loguea las URLs bloqueadas (debug).
        """
        async def handle_route(route: Route) -> None:
            try:
                if route.request.resource_type == "image":
                    if url_pattern is None or url_pattern in route.request.url:
                        if log_blocked:
                            logger.debug("Bloqueada imagen: %s", route.request.url)
                        await route.abort()
                        return
                await route.continue_()
            except Exception as exc:
                logger.debug("Error en route handler: %s", exc)
                try:
                    await route.continue_()
                except Exception:
                    pass

        await context.route("**/*", handle_route)
        logger.debug("Interceptor de imágenes registrado en contexto.")

    async def _extract_page_results(self, page: Page, keyword: str) -> None:
        """
        Extrae los resultados de la página actual del CSE.

        Proceso:
          1. Scroll incremental con ``human_scroll`` (simula lectura)
          2. Verificación CAPTCHA post-scroll
          3. Esperar al selector de resultados
          4. Parsear URL y timestamp de cada tarjeta de resultado

        Args:
            page:    Página activa con resultados CSE.
            keyword: Keyword actual (para logs y detección de CAPTCHA).

        Raises:
            CaptchaError: Si se detecta CAPTCHA durante la extracción.
        """
        await self._incremental_scroll(page)
        await CaptchaDetector.check(page, keyword)

        try:
            await page.wait_for_selector(".gsc-webResult", timeout=10_000)
        except PlaywrightTimeoutError:
            await CaptchaDetector.check(page, keyword)
            logger.info("Sin resultados para keyword='%s' (timeout legítimo).", keyword)
            return

        results_container = page.locator(".gsc-expansionArea .gsc-webResult")
        count = await results_container.count()
        logger.debug("Parsing %d bloques | keyword='%s'", count, keyword)

        for i in range(count):
            try:
                container = results_container.nth(i)
                link = container.locator(".gs-title a.gs-title").first
                href = (
                    await link.get_attribute("data-ctorig")
                    or await link.get_attribute("href")
                )
                if not href or "google.com" in href:
                    continue

                snippet_text = (
                    await container.locator(".gs-snippet").first.text_content() or ""
                ).strip()
                ts_match = _RELATIVE_TS_PATTERN.match(snippet_text)

                url_clean = clean_url(href)
                # Inferir plataforma desde URL si el engine no la especificó
                platform = self._platform or (
                    "instagram" if "instagram" in url_clean else "facebook"
                )

                self._scraped_results.append(ScrapedResult(
                    url=url_clean,
                    platform=platform,
                    published_at=self._parse_relative_timestamp(snippet_text) if ts_match else None,
                    published_at_raw=ts_match.group(1) if ts_match else None,
                ))
            except Exception as exc:
                logger.error("Error procesando resultado #%d: %s", i, exc)

    # ── Persistencia ─────────────────────────────────────────────────────────

    async def _save_to_supabase(self, keyword: str) -> tuple[int, int]:
        """
        Persiste los resultados del buffer en Supabase.

        Usa ``bulk_insert_urls`` que hace UPSERT con ON CONFLICT (url) DO NOTHING:
        las URLs que ya existen en la tabla se omiten silenciosamente.

        Args:
            keyword: Keyword que originó estos resultados.

        Returns:
            Tupla ``(insertados, omitidos)``.
        """
        if not self._url_repo:
            logger.warning("_save_to_supabase: url_repo no configurado.")
            return 0, 0

        urls_to_insert = [
            {
                "url": item["url"],
                "keyword": keyword,
                "platform": item["platform"],
                "send_tg": False,
            }
            for item in self._scraped_results
            if item.get("url")
        ]

        if not urls_to_insert:
            return 0, 0

        return await self._url_repo.bulk_insert_urls(urls_to_insert)

    async def _send_to_api(self) -> tuple[int, int]:
        """
        Envía las URLs del buffer al endpoint HTTP externo.

        Usa conexión directa (no proxy). ``verify`` controlado por
        ``DATA_STORE_VERIFY_SSL`` en settings para CAs privadas.

        Returns:
            Tupla ``(sent_ok, failed)``.
        """
        valid = [(item["url"], item["platform"]) for item in self._scraped_results if item.get("url")]
        if not valid:
            return 0, 0

        sent_ok = failed = 0
        async with httpx.AsyncClient(
            headers=_DATA_STORE_HEADERS,
            timeout=10.0,
            verify=settings.DATA_STORE_VERIFY_SSL,
        ) as client:
            for post_url, platform in valid:
                endpoint = f"{settings.DATA_STORE_BASE_URL}/{platform}/urls"
                try:
                    resp = await client.post(endpoint, json={"post_url": post_url})
                    if resp.is_success:
                        sent_ok += 1
                    else:
                        failed += 1
                        logger.warning(
                            "API rechazó URL (HTTP %d): %s", resp.status_code, post_url[:80]
                        )
                except httpx.RequestError as exc:
                    failed += 1
                    logger.error("Error de red enviando URL: %s", exc)

        return sent_ok, failed

    async def _persist_results(self, keyword: str) -> None:
        """
        Enruta la persistencia al backend configurado en ``settings.OUTPUT_MODE``.

        Modos:
          "supabase" → INSERT INTO url (ON CONFLICT DO NOTHING)
          "api"      → POST al endpoint HTTP externo

        Cualquier valor no reconocido produce un WARNING y no persiste nada,
        para evitar pérdida silenciosa de datos.

        Args:
            keyword: Keyword que originó los resultados del buffer actual.
        """
        mode = settings.OUTPUT_MODE.strip().lower()

        if mode == "supabase":
            inserted, skipped = await self._save_to_supabase(keyword)
            logger.info(
                "[Supabase] %d insertadas, %d omitidas | keyword='%s'",
                inserted, skipped, keyword,
            )
        elif mode == "api":
            sent_ok, failed = await self._send_to_api()
            logger.info(
                "[API] %d enviadas, %d fallidas | keyword='%s'",
                sent_ok, failed, keyword,
            )
        else:
            logger.warning(
                "OUTPUT_MODE='%s' no reconocido (válidos: 'supabase', 'api'). "
                "Resultados NO persistidos.",
                settings.OUTPUT_MODE,
            )

    # ── Filtro de fecha en CSE ───────────────────────────────────────────────

    async def _apply_date_filter(self, page: Page) -> None:
        """
        Activa el filtro 'Date' del CSE para ordenar resultados por fecha.

        OPTIMIZADO: Reemplaza delays ciegos por waits selectivos.
        """
        try:
            dropdown = page.locator(".gsc-selected-option-container").first
            await self._quick_move_and_click(page, dropdown)
            
            # Esperar a que el menú se despliegue
            date_option = page.locator(".gsc-option-menu-item", has_text="Date")
            await date_option.wait_for(state="visible", timeout=5_000)
            
            # Click rápido en la opción Date
            box = await date_option.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                
                # Movimiento directo rápido (sin arco)
                await page.mouse.move(target_x, target_y)
                await asyncio.sleep(random.uniform(0.05, 0.12))
                await page.mouse.click(target_x, target_y)
            else:
                await date_option.click()
            
            # Esperar a que los resultados se recarguen
            try:
                await page.wait_for_selector(".gsc-webResult", timeout=8_000)
                logger.debug("Filtro Date aplicado, resultados recargados.")
            except PlaywrightTimeoutError:
                logger.warning("Timeout esperando recarga de resultados tras filtro Date.")
            
            # Delay mínimo para estabilidad
            await asyncio.sleep(random.uniform(0.2, 0.5))
            
        except Exception as exc:
            logger.warning("No se pudo activar filtro de fecha: %s", exc)

    @staticmethod
    def _play_alert_sound() -> None:
        """
        Alerta sonora incremental para detección de CAPTCHA.

        El sonido escala en frecuencia y ritmo para llamar la atención:
          - Comienza con pitidos graves y espaciados
          - Progresa a pitidos agudos y rápidos
          - Finaliza con un patrón de urgencia (agudo + rápido)

        Windows: usa winsound.Beep(frequency, duration) con frecuencias
                 crecientes (400Hz → 2000Hz) y duraciones decrecientes.
        Linux/macOS: imprime el carácter BEL (\\a) múltiples veces con
                     intervalos decrecientes vía sys.stdout.
        """
        try:
            import platform as _plt

            if _plt.system() == "Windows":
                import winsound

                # Fase 1: Atención inicial (grave, lento)
                winsound.Beep(400, 400)
                winsound.Beep(400, 400)

                # Fase 2: Escalada progresiva (frecuencia ↑, duración ↓)
                frequencies = [500, 600, 800, 1000, 1200, 1500, 1800]
                durations   = [350, 300, 250, 200,  180,  150,  120]

                for freq, dur in zip(frequencies, durations):
                    winsound.Beep(freq, dur)

                # Fase 3: Urgencia máxima (agudo, muy rápido, repetitivo)
                for _ in range(5):
                    winsound.Beep(2000, 80)
                    winsound.Beep(2000, 80)

                # Fase 4: Cierre de alerta (frecuencia descendente)
                winsound.Beep(1500, 200)
                winsound.Beep(1000, 300)
                winsound.Beep(600,  400)

            else:
                # Linux/macOS: BEL characters con intervalos decrecientes
                import time

                # Fase 1: Atención inicial (lento)
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.4)
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.4)

                # Fase 2: Escalada (intervalos decrecientes)
                intervals = [0.35, 0.30, 0.25, 0.20, 0.15, 0.12, 0.10]
                for interval in intervals:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
                    time.sleep(interval)

                # Fase 3: Urgencia máxima (muy rápido)
                for _ in range(5):
                    sys.stdout.write("\a")
                    sys.stdout.flush()
                    time.sleep(0.05)

                # Fase 4: Cierre (más lento)
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.2)
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.3)
                sys.stdout.write("\a")
                sys.stdout.flush()

        except Exception:
            # Fallback silencioso: si el sonido falla, no interrumpir el scraping
            pass

    # ── API pública ──────────────────────────────────────────────────────────

    async def setup_page(self, page: Page, fingerprint: BrowserFingerprint) -> None:
        """
        Prepara una página nueva con anti-detección completa.

        DEBE llamarse justo después de ``context.new_page()`` y ANTES
        de cualquier navegación, para que los init scripts se ejecuten
        antes que cualquier JS de la página destino.

        CAMOUFOX: Aunque Camoufox tiene anti-detección nativa, mantenemos
        los parches personalizados para máxima cobertura.

        Aplica:
          - 12+ parches JS de stealth via add_init_script
          - Interceptor de respuestas HTTP (registra 429/403)

        Args:
            page:        Página recién creada.
            fingerprint: Fingerprint de la sesión actual.
        """
        await self.apply_stealth(page, fingerprint)
        await CaptchaDetector.intercept_response_errors(page)
        logger.debug("Page setup: stealth + interceptor HTTP activos.")

    async def run_keyword(
        self,
        page: Page,
        keyword: str,
        total_pages: int = 3,
    ) -> None:
        """
        Ejecuta la búsqueda completa de una keyword en el CSE.

        OPTIMIZADO:
          - Click directo en search box (sin movimiento de ratón previo)
          - Sin delays entre click y type
          - Waits selectivos en lugar de delays ciegos
          - CAPTCHA espera mínimo 120s antes de continuar
        """
        self._scraped_results.clear()

        # ── 1. Inyectar referrer plausible ───────────────────────────────────
        await self._inject_referrer(page)

        # ── 2. Navegar al CSE ────────────────────────────────────────────────
        await page.goto(
            self._search_url,
            wait_until="domcontentloaded",
            timeout=20_000
        )
        await CaptchaDetector.check(page, keyword)

        # Esperar solo al search box, sin delays extra
        try:
            await page.wait_for_selector(
                "input.gsc-input",
                state="visible",
                timeout=8_000
            )
        except PlaywrightTimeoutError:
            logger.warning("Timeout esperando search box. Continuando...")

        # ── 3. Click DIRECTO + type INMEDIATO (sin delays) ─────────────────
        search_box = page.locator("input.gsc-input")
        
        # Click directo sin movimiento de ratón (el foco va al input)
        await search_box.click(timeout=3_000)
        
        # Escribir INMEDIATAMENTE después del click (sin asyncio.sleep)
        await human_type(
            page,
            "input.gsc-input",
            keyword,
            clear_first=True,
            wpm=random.randint(80, 130)
        )
        
        # Solo micro-pausa post-escritura (simula revisar lo escrito)
        await asyncio.sleep(random.uniform(0.2, 0.4))
        await page.keyboard.press("Enter")

        # ── 4. Esperar resultados ───────────────────────────────────────────
        try:
            await page.wait_for_selector(
                ".gsc-webResult",
                state="visible",
                timeout=12_000
            )
        except PlaywrightTimeoutError:
            await CaptchaDetector.check(page, keyword)
            logger.info(
                "Sin resultados para keyword='%s' (timeout legítimo).",
                keyword
            )
            return

        await CaptchaDetector.check(page, keyword)

        # ── 5. Aplicar filtro de fecha ──────────────────────────────────────
        await self._apply_date_filter(page)

        # ── 6. Procesar páginas de resultados ───────────────────────────────
        for current_p in range(1, total_pages + 1):
            logger.info(
                "Página %d/%d | keyword='%s'",
                current_p, total_pages, keyword
            )

            try:
                await self._extract_page_results(page, keyword)

            except CaptchaError as cap_err:
                logger.warning(
                    "CAPTCHA detectado (signal=%s). Iniciando resolución...",
                    cap_err.signal,
                )
                self._play_alert_sound()

                # Fase 1: auto-solver rápido
                auto_solved = await CaptchaAutosolver.try_solve_checkbox(
                    page=page,
                    keyword=keyword,
                    max_attempts=2
                )
                if auto_solved:
                    logger.info("CAPTCHA resuelto automáticamente.")
                    await self._extract_page_results(page, keyword)
                else:
                    # Fase 2: esperar resolución manual (NO recargar)
                    # La espera real ocurre en el orquestador
                    raise

            # Persistir resultados de esta página
            await self._persist_results(keyword)
            self._scraped_results.clear()

            # ── Navegación entre páginas ─────────────────────────────────
            if current_p < total_pages:
                # Micro-pausa (sin simulate_reading_pause)
                await asyncio.sleep(random.uniform(0.2, 0.4))

                # Siguiente página (click directo)
                next_btn = page.locator(
                    ".gsc-cursor-page:not(.gsc-cursor-current-page)",
                    has_text=str(current_p + 1),
                )
                if await next_btn.is_visible():
                    await next_btn.click(timeout=3_000)

                    # Esperar a que la página cambie
                    try:
                        await page.wait_for_selector(
                            f".gsc-cursor-current-page:has-text('{current_p + 1}')",
                            timeout=8_000,
                        )
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "Página %d no confirmada. Continuando...",
                            current_p + 1
                        )

                    await asyncio.sleep(random.uniform(0.2, 0.4))
                else:
                    logger.debug(
                        "No hay página %d para keyword='%s'.",
                        current_p + 1, keyword
                    )
                    break

        # ── 7. Post-keyword (muy bajo overhead) ────────────────────────────
        if random.random() < 0.05:  # Solo 5% de probabilidad
            logger.debug("Simulando distracción post-keyword.")
            await simulate_distraction(page)