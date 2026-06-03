"""
google_cse_automator.py
Motor de scraping sobre Google Custom Search Engine (CSE).

Anti-detección integrada (5 capas)
────────────────────────────────────
  Capa 1 – Fingerprint coherente por sesión
    BrowserFingerprint: UA + platform + WebGL + viewport coherentes entre sí.

  Capa 2 – Init scripts de stealth (12 parches JS)
    Inyectados antes de cualquier carga via add_init_script():
    webdriver, plugins, canvas noise, WebGL, AudioContext, WebRTC, screen
    metrics, Permissions API, performance.now(), Notification, iframe
    propagation y window.chrome.

  Capa 3 – Comportamiento humano completo
    · _inject_referrer     → simula que el usuario llegó desde Google/Bing
    · _arc_move_and_click  → curvas de Bézier con easing cosenoidal
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
  "sqlite" → PostRepository.bulk_insert_new() — URLs únicas, sin duplicados
  "api"    → httpx POST al endpoint HTTP externo
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict, Optional

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
    generate_fingerprint,       # noqa: F401 (usado en run_scraper)
    micro_delay,
    simulate_distraction,
    simulate_idle,
    simulate_page_focus_blur,   # ← integrado entre keywords
    simulate_reading_pause,
)
from anti_detection.human_behavior import (
    human_scroll,               # ← reemplaza raw evaluate
    human_type,                 # ← reemplaza implementación inline
)

from config.settings import settings
from database import PostCreate, PostRepository
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
    """

    page_load_wait_range:    tuple[float, float] = (3.5, 7.0)
    scroll_pause_range:      tuple[float, float] = (0.3, 1.1)
    between_pages_range:     tuple[float, float] = (4.0, 11.0)
    between_keywords_range:  tuple[float, float] = (6.0, 18.0)
    warmup_pause_range:      tuple[float, float] = (2.0, 5.0)
    typing_wpm_range:        tuple[int, int]     = (65, 115)
    warmup_url:              str                 = "https://www.google.com"
    captcha_max_wait_seconds: float              = 300.0
    captcha_wait_for_human:  bool                = False
    # Probabilidad de simular distracción entre páginas
    distraction_probability: float               = 0.15
    # Probabilidad de simular cambio de pestaña entre keywords
    focus_blur_probability:  float               = 0.25

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
    SQLite. Esta clase recibe una ``Page`` activa y el ``PostRepository``
    ya inicializado; no abre ni cierra recursos de DB internamente.

    Args:
        cse_id:       ID del Custom Search Engine de Google.
        platform:     Plataforma objetivo ("instagram" | "facebook" | …).
        post_repo:    Repositorio SQLite de posts. None → solo modo "api".
        config:       BrowserConfig. Se crea uno por defecto si None.
        browser_type: "firefox" | "chromium".
    """

    def __init__(
        self,
        cse_id: str,
        platform: str = "",
        post_repo: PostRepository | None = None,
        config: BrowserConfig | None = None,
        browser_type: str = "firefox",
    ) -> None:
        self._search_url  = f"https://cse.google.com/cse?cx={cse_id}"
        self._platform    = platform
        self._post_repo   = post_repo
        self.cfg          = config or BrowserConfig()
        self.browser_type = browser_type
        # Buffer de resultados de la página actual (se limpia por página)
        self._scraped_results: list[ScrapedResult] = []
        self._session_store = SessionStore(settings.SESSION_DIR)

    # ── Helpers de timing ────────────────────────────────────────────────────

    async def _human_sleep(self, low: float, high: float) -> None:
        """Pausa jitter usando la distribución gaussiana + exponencial."""
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    # ── Referrer injection ───────────────────────────────────────────────────

    async def _inject_referrer(self, page: Page) -> None:
        """
        Navega brevemente a un referrer plausible antes de ir al CSE.

        Establece ``document.referrer`` con un origen legítimo (Google, Bing, etc.)
        para que el CSE vea la petición como parte de una sesión de navegación
        normal y no como acceso directo desde un script.

        Se usa ``wait_until="commit"`` para no esperar la carga completa del
        referrer (solo que el navegador lo haya iniciado).

        Args:
            page: Página activa del contexto actual.
        """
        referrer = random.choice(_PLAUSIBLE_REFERRERS)
        try:
            await page.goto(referrer, wait_until="commit", timeout=8_000)
            # Pausa corta simulando que el usuario leyó algo en la página
            await asyncio.sleep(random.uniform(0.8, 2.2))
            logger.debug("Referrer inyectado: %s", referrer)
        except Exception as exc:
            logger.debug("Referrer injection ignorada (non-critical): %s", exc)

    # ── Movimiento de ratón con Bézier ───────────────────────────────────────

    async def _arc_move_and_click(self, page: Page, locator: Any) -> None:
        """
        Mueve el ratón hasta el elemento con una curva de Bézier cúbica
        y easing cosenoidal (aceleración/desaceleración natural).

        Obtiene el bounding box del elemento para apuntar a un punto
        aleatorio dentro de él (no siempre el centro exacto).
        Si no puede obtener el bounding box, hace click directo.

        Args:
            page:    Página activa.
            locator: Locator de Playwright del elemento destino.
        """
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click()
                return

            target_x = box["x"] + box["width"]  * random.uniform(0.25, 0.75)
            target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)

            viewport = page.viewport_size or {"width": 1280, "height": 800}
            start_x = random.uniform(viewport["width"]  * 0.1, viewport["width"]  * 0.9)
            start_y = random.uniform(viewport["height"] * 0.1, viewport["height"] * 0.7)

            steps = random.randint(14, 28)
            for step in range(steps + 1):
                t      = step / steps
                t_ease = (1 - math.cos(math.pi * t)) / 2       # easing cosenoidal
                arc    = math.sin(math.pi * t) * random.uniform(-25, 25)
                hyp    = max(1, math.hypot(target_x - start_x, target_y - start_y))
                # Vector perpendicular para el arco
                perp_x = -(target_y - start_y) / hyp
                perp_y  =  (target_x - start_x) / hyp
                x = start_x + (target_x - start_x) * t_ease + perp_x * arc
                y = start_y + (target_y - start_y) * t_ease + perp_y * arc
                await page.mouse.move(x, y)
                # Velocidad variable: lenta al inicio/fin, rápida en el centro
                speed_factor = 1.6 - math.sin(math.pi * t_ease)
                await asyncio.sleep(random.uniform(0.004, 0.018) * speed_factor)

            # Pausa de "apunte" antes del click
            await micro_delay(60, 180)
            await page.mouse.click(target_x, target_y)
            # Micro-pausa de reacción post-click
            await micro_delay(40, 120)

        except Exception as exc:
            logger.debug("arc_move falló, usando click directo: %s", exc)
            await locator.click()

    # ── Stealth y warmup ─────────────────────────────────────────────────────

    @staticmethod
    async def apply_stealth(page: Page, fingerprint: BrowserFingerprint) -> None:
        """
        Inyecta los 12 parches JS de evasión como init script de la página.

        ``add_init_script`` garantiza que el script se ejecuta ANTES de que
        cualquier script de la página se cargue, incluyendo los detectores
        de bot. No es posible interceptar la inyección desde la página.

        Parches incluidos:
          navigator.webdriver, platform, hardwareConcurrency, deviceMemory,
          connection API, plugins (por browser), canvas noise, WebGL vendor/
          renderer, AudioContext noise, WebRTC IP leak, screen metrics con
          chrome frame, Permissions API, performance.now() precision,
          Notification.permission, iframe propagation.

        Args:
            page:        Página recién creada (antes de cualquier goto).
            fingerprint: Fingerprint de la sesión actual.
        """
        await page.add_init_script(fingerprint.stealth_js)
        logger.debug("Stealth init script aplicado (%d bytes).", len(fingerprint.stealth_js))

    async def _warmup_session(self, page: Page) -> None:
        """
        Calienta la sesión: navega a Google y simula actividad humana real.

        CORRECCIÓN: reemplaza ``page.evaluate("window.scrollTo(...)")`` por
        ``human_scroll``, que usa ráfagas con pausas gaussianas. El evaluate
        directo produce un desplazamiento instantáneo sin eventos de wheel,
        detectable como automatización por los sistemas de análisis de Google.

        Args:
            page: Página activa del contexto recién creado.
        """
        try:
            await page.goto(self.cfg.warmup_url, wait_until="domcontentloaded", timeout=15_000)
            await simulate_reading_pause(page, words_estimate=random.randint(10, 30))
            # human_scroll en lugar de evaluate("window.scrollTo"): genera
            # eventos wheel reales con distribución estadística natural.
            scroll_amount = random.randint(50, 300)
            await human_scroll(page, direction="down", amount=scroll_amount)
            await simulate_idle(page, duration_seconds=random.uniform(1.0, 2.5))
            logger.debug("Session warmup completado en %s.", self.cfg.warmup_url)
        except Exception as exc:
            logger.debug("Warmup ignorado (non-critical): %s", exc)

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
        """
        Scroll incremental de la página de resultados usando ``human_scroll``.

        ``human_scroll`` del módulo ``anti_detection.human_behavior`` usa
        ráfagas variables separadas por pausas de lectura, que es más natural
        que el scroll uniforme generado por ``evaluate('window.scrollTo...')``.
        Incluye movimientos de ratón ocasionales durante el scroll.

        Args:
            page: Página activa con resultados CSE cargados.
        """
        page_height = await page.evaluate("document.body.scrollHeight")
        viewport_h  = await page.evaluate("window.innerHeight") or 768

        # Dividir en segmentos de viewport y hacer scroll humano por cada uno
        segments = max(2, int(page_height / viewport_h))
        for seg in range(segments):
            segment_px = int(page_height / segments)
            # human_scroll del módulo: ráfagas con pausas gaussianas
            await human_scroll(page, direction="down", amount=segment_px)

            # Movimiento de ratón aleatorio mientras "lee" (15% por segmento)
            if random.random() < 0.15:
                vp = page.viewport_size or {"width": 1280, "height": 800}
                await page.mouse.move(
                    random.uniform(vp["width"] * 0.1, vp["width"] * 0.9),
                    random.uniform(vp["height"] * 0.1, vp["height"] * 0.8),
                )
            # Pausa de lectura entre segmentos
            await asyncio.sleep(random.uniform(*self.cfg.scroll_pause_range))

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

        CORRECCIÓN: el parámetro era ``page: Page``; en run_scraper.py se
        pasa un ``BrowserContext``. Playwright acepta ``.route()`` en ambos,
        pero la semántica correcta aquí es nivel contexto.

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

    async def _save_to_sqlite(self, keyword: str) -> tuple[int, int]:
        """
        Persiste los resultados del buffer en SQLite vía PostRepository.

        Usa ``bulk_insert_new`` que hace ``INSERT OR IGNORE``: las URLs
        que ya existen en la tabla se omiten silenciosamente. Solo se
        insertan URLs nuevas, con ``scrapt_at`` fijado al momento actual.

        Args:
            keyword: Keyword que originó estos resultados.

        Returns:
            Tupla ``(insertados, omitidos)``.
        """
        if not self._post_repo:
            logger.warning("_save_to_sqlite: post_repo no configurado.")
            return 0, 0

        posts = [
            PostCreate(
                url=item["url"],
                keyword=keyword,
                platform=item["platform"],
            )
            for item in self._scraped_results
            if item.get("url")
        ]
        if not posts:
            return 0, 0

        return await self._post_repo.bulk_insert_new(posts)

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
          "sqlite" → inserta en tabla ``posts`` vía PostRepository (INSERT OR IGNORE)
          "api"    → POST al endpoint HTTP externo

        Cualquier valor no reconocido produce un WARNING y no persiste nada,
        para evitar pérdida silenciosa de datos.

        Args:
            keyword: Keyword que originó los resultados del buffer actual.
        """
        mode = settings.OUTPUT_MODE.strip().lower()

        if mode == "sqlite":
            inserted, skipped = await self._save_to_sqlite(keyword)
            logger.info(
                "[SQLite] %d insertados, %d omitidos | keyword='%s'",
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
                "OUTPUT_MODE='%s' no reconocido (válidos: 'sqlite', 'api'). "
                "Resultados NO persistidos.",
                settings.OUTPUT_MODE,
            )

    # ── Filtro de fecha en CSE ───────────────────────────────────────────────

    async def _apply_date_filter(self, page: Page) -> None:
        """
        Activa el filtro 'Date' del CSE para ordenar resultados por fecha.

        Usa ``_arc_move_and_click`` para que la interacción con el dropdown
        parezca humana. Si el filtro no está disponible (algunos CSE no lo
        tienen), la excepción se captura y el scraping continúa sin filtro.

        Args:
            page: Página activa con resultados CSE cargados.
        """
        try:
            dropdown = page.locator(".gsc-selected-option-container").first
            await self._arc_move_and_click(page, dropdown)
            date_option = page.locator(".gsc-option-menu-item", has_text="Date")
            await date_option.wait_for(state="visible", timeout=5_000)
            await self._arc_move_and_click(page, date_option)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await self._human_sleep(1.5, 3.0)
        except Exception as exc:
            logger.warning("No se pudo activar filtro de fecha: %s", exc)

    # ── Alerta sonora ────────────────────────────────────────────────────────

    @staticmethod
    def _play_alert_sound() -> None:
        """Beep de alerta al detectar CAPTCHA (útil en modo headless=False)."""
        try:
            import platform as _plt
            if _plt.system() == "Windows":
                import winsound
                winsound.Beep(1000, 600)
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            pass

    # ── API pública ──────────────────────────────────────────────────────────

    async def setup_page(self, page: Page, fingerprint: BrowserFingerprint) -> None:
        """
        Prepara una página nueva con anti-detección completa.

        DEBE llamarse justo después de ``context.new_page()`` y ANTES
        de cualquier navegación, para que los init scripts se ejecuten
        antes que cualquier JS de la página destino.

        Aplica:
          - 12 parches JS de stealth via add_init_script
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

        Flujo anti-detección completo:
          1. _inject_referrer      → establece document.referrer legítimo
          2. goto CSE              → navegar al motor con fingerprint activo
          3. CaptchaDetector.check → verificar CAPTCHA pre-búsqueda
          4. human_type            → escribir con velocidad gaussiana + typos
          5. _apply_date_filter    → interacción humana con el dropdown
          6. Por cada página:
             a. _incremental_scroll (human_scroll) → simular lectura
             b. CaptchaDetector.check              → verificar CAPTCHA
             c. _extract_page_results              → parsear resultados
             d. _persist_results                   → guardar en SQLite o API
             e. simulate_reading_pause             → pausa proporcional al texto
             f. simulate_distraction (15%)         → comportamiento errático
             g. _arc_move_and_click en paginación  → clic humano
          7. simulate_page_focus_blur (25%)        → simular cambio de pestaña

        Args:
            page:        Página activa (``setup_page`` debe haberse llamado antes).
            keyword:     Término de búsqueda.
            total_pages: Páginas máximas de resultados a procesar.

        Raises:
            CaptchaError: Si el CAPTCHA no pudo resolverse (escala al orquestador).
        """
        self._scraped_results.clear()

        # ── 1. Inyectar referrer plausible ───────────────────────────────────
        await self._inject_referrer(page)

        # ── 2. Navegar al CSE ────────────────────────────────────────────────
        await page.goto(self._search_url, wait_until="domcontentloaded", timeout=20_000)
        await CaptchaDetector.check(page, keyword)
        await self._human_sleep(*self.cfg.page_load_wait_range)

        # ── 3. Escribir keyword con human_type del módulo ────────────────────
        # human_type incluye: click en el campo, escritura gaussiana,
        # typos con Backspace, pausas extra en espacios y puntuación.
        search_box_selector = "input.gsc-input"
        search_box = page.locator(search_box_selector)
        await self._arc_move_and_click(page, search_box)
        await human_type(page, search_box_selector, keyword, clear_first=True)
        await asyncio.sleep(random.uniform(0.3, 0.9))
        await page.keyboard.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=15_000)
        await CaptchaDetector.check(page, keyword)

        # ── 4. Aplicar filtro de fecha ────────────────────────────────────────
        await self._apply_date_filter(page)

        # ── 5. Procesar páginas de resultados ─────────────────────────────────
        for current_p in range(1, total_pages + 1):
            logger.info("Página %d/%d | keyword='%s'", current_p, total_pages, keyword)

            try:
                await self._extract_page_results(page, keyword)

            except CaptchaError as cap_err:
                logger.warning(
                    "CAPTCHA detectado (signal=%s). Intentando auto-resolver...",
                    cap_err.signal,
                )
                self._play_alert_sound()

                # Fase 1: auto-solver de checkbox (reCAPTCHA / hCaptcha / Turnstile)
                auto_solved = await CaptchaAutosolver.try_solve_checkbox(
                    page=page, keyword=keyword, max_attempts=2
                )
                if auto_solved:
                    logger.info("CAPTCHA resuelto. Reextrayendo resultados...")
                    await self._extract_page_results(page, keyword)

                # Fase 2: espera resolución manual (headless=False)
                elif self.cfg.captcha_wait_for_human:
                    logger.warning("Auto-solver falló. Esperando resolución manual...")
                    resolved = await CaptchaDetector.wait_for_human_resolution(
                        page=page,
                        keyword=keyword,
                        max_wait=self.cfg.captcha_max_wait_seconds,
                    )
                    if resolved:
                        await self._extract_page_results(page, keyword)
                    else:
                        raise

                # Fase 3: escalar al orquestador (rotará identidad)
                else:
                    raise

            # Persistir resultados de esta página y limpiar el buffer.
            # Se persiste POR PÁGINA para no acumular en memoria si hay muchas.
            await self._persist_results(keyword)
            self._scraped_results.clear()

            # ── Comportamiento post-página ─────────────────────────────────
            if current_p < total_pages:
                # Simular lectura de los resultados obtenidos
                await simulate_reading_pause(page, words_estimate=random.randint(40, 80))

                # Distracción ocasional: ratón a las esquinas
                if random.random() < self.cfg.distraction_probability:
                    logger.debug("Simulando distracción entre páginas.")
                    await simulate_distraction(page)

                # Navegar a la siguiente página con click humano
                next_btn = page.locator(
                    ".gsc-cursor-page:not(.gsc-cursor-current-page)",
                    has_text=str(current_p + 1),
                )
                if await next_btn.is_visible():
                    await self._arc_move_and_click(page, next_btn)
                    await page.locator(".gsc-cursor-current-page").filter(
                        has_text=str(current_p + 1)
                    ).wait_for(state="visible", timeout=12_000)
                    await self._human_sleep(*self.cfg.between_pages_range)
                else:
                    logger.debug(
                        "No hay página %d para keyword='%s'.", current_p + 1, keyword
                    )
                    break

        # ── 6. Simular cambio de pestaña entre keywords (25%) ─────────────────
        # simulate_page_focus_blur dispara eventos visibilitychange + focus,
        # que los detectores monitorizan: un bot nunca pierde el foco.
        if random.random() < self.cfg.focus_blur_probability:
            logger.debug("Simulando focus/blur entre keywords.")
            await simulate_page_focus_blur(page)