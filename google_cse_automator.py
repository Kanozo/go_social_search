"""
google_cse_automator.py
Motor de scraping sobre Google Custom Search Engine (CSE).

Integra todas las capas de anti-detección:
  ┌─────────────────────────────────────────────────────────┐
  │  Capa 1 – Fingerprint coherente por sesión              │
  │    BrowserFingerprint: UA + platform + WebGL + viewport │
  ├─────────────────────────────────────────────────────────┤
  │  Capa 2 – Init scripts de stealth (12 parches JS)       │
  │    webdriver, plugins, canvas, WebGL, audio, RTC, etc.  │
  ├─────────────────────────────────────────────────────────┤
  │  Capa 3 – Comportamiento humano                         │
  │    Bézier mouse, typing jitter, scroll por ráfagas,     │
  │    idle simulation, distraction, focus/blur events      │
  ├─────────────────────────────────────────────────────────┤
  │  Capa 4 – Gestión de red                                │
  │    session persistence (cookies + localStorage)         │
  ├─────────────────────────────────────────────────────────┤
  │  Capa 5 – Detección y respuesta                         │
  │    Multi-signal CAPTCHA detection, HTTP 429/403         │
  │    intercept, warmup session, referrer injection        │
  └─────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

import httpx
from dateutil.relativedelta import relativedelta
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from anti_detection import (
    BrowserFingerprint,
    generate_fingerprint,
    human_delay,
    human_move_to,
    micro_delay,
    simulate_distraction,
    simulate_idle,
    simulate_reading_pause,
)
from config.settings import settings
from utils.captcha_guard import CaptchaAutosolver, CaptchaDetector, CaptchaError
from utils.session_store import SessionStore
from utils.url_clean import clean_url
from schemas.google_result_schema import GoogleResultInDB
from database.core_db import DatabaseManager
from database.google_result_db import GoogleResultRepository

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constantes de configuración
# ─────────────────────────────────────────────────────────────────────────────

# Token de autenticación leído desde settings (nunca hardcodeado aquí)
_DATA_STORE_TOKEN: str = settings.DATA_STORE_TOKEN
_DATA_STORE_BASE_URL: str = settings.DATA_STORE_BASE_URL
_DATA_STORE_HEADERS: dict[str, str] = {
    "Authorization": f"Bearer {_DATA_STORE_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Referrers plausibles para inyectar en el contexto antes de CSE
# (simula que el usuario llegó desde una búsqueda normal de Google)
_PLAUSIBLE_REFERRERS: list[str] = [
    "https://www.google.com/",
    "https://www.google.com/search?q=site:facebook.com",
    "https://duckduckgo.com/",
    "https://www.bing.com/",
]

# Patrones de timestamp relativo en español
_UNIT_MAP: dict[str, str] = {
    "minuto": "minutes", "minutos": "minutes",
    "hora": "hours",     "horas": "hours",
    "día": "days",       "días": "days",
    "semana": "weeks",   "semanas": "weeks",
    "mes": "months",     "meses": "months",
    "año": "years",      "años": "years",
}
_RELATIVEDELTA_UNITS: frozenset[str] = frozenset({"months", "years"})
_RELATIVE_TS_PATTERN: re.Pattern[str] = re.compile(
    r"^(hace\s+(\d+)\s+(hora|horas|minuto|minutos|día|días|semana|semanas|mes|meses|año|años))",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Tipos de datos
# ─────────────────────────────────────────────────────────────────────────────

class ScrapedResult(TypedDict):
    """Resultado individual extraído de una página de CSE."""
    url: str
    published_at: datetime | None
    published_at_raw: str | None


# ─────────────────────────────────────────────────────────────────────────────
# BrowserConfig
# ─────────────────────────────────────────────────────────────────────────────

class BrowserConfig:
    """
    Parámetros de timing y comportamiento del navegador automatizado.

    Todos los rangos se expresan como (min, max) en segundos.
    Los métodos ``jitter_wait`` y ``pick_*`` usan distribuciones estadísticas
    para generar valores más naturales que ``random.uniform`` puro.
    """

    # Tiempos de espera (segundos)
    page_load_wait_range: tuple[float, float]    = (3.5, 7.0)
    scroll_pause_range:   tuple[float, float]    = (0.3, 1.1)
    between_pages_range:  tuple[float, float]    = (4.0, 11.0)
    between_keywords_range: tuple[float, float]  = (6.0, 18.0)
    warmup_pause_range:   tuple[float, float]    = (2.0, 5.0)

    # Escritura
    typing_mean: float = 0.11   # segundos por caracter (media gaussiana)
    typing_std:  float = 0.04

    # Session warmup
    warmup_url: str = "https://www.google.com"

    # CAPTCHA handling
    captcha_max_wait_seconds: float = 300.0
    captcha_wait_for_human:   bool  = False

    # Probabilidad de simular distracción entre páginas (0.0 – 1.0)
    distraction_probability: float = 0.15

    def jitter_wait(self, low: float, high: float) -> float:
        """
        Genera un tiempo de espera con distribución gaussiana + cola exponencial.

        La cola exponencial añade picos ocasionales de espera larga que son
        característicos del comportamiento humano (distracciones, lectura).

        Args:
            low:  Límite inferior del rango.
            high: Límite superior del rango.

        Returns:
            Valor de espera en segundos, clampado entre low y high*1.5.
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

    Cada instancia gestiona un único engine CSE. El ciclo de vida del browser
    y el contexto son responsabilidad del orquestador externo (``run_scraper.py``);
    esta clase solo recibe una ``Page`` activa.

    Args:
        cse_id:  ID del Custom Search Engine de Google.
        config:  Instancia de ``BrowserConfig``. Se crea una por defecto si es None.
        browser_type: "firefox" | "chromium". Afecta al pool de UA y parches JS.
    """

    def __init__(
        self,
        cse_id: str,
        config: BrowserConfig | None = None,
        browser_type: str = "firefox",
        db_manager: DatabaseManager | None = None,
        results_repo: GoogleResultRepository | None = None,
        sent_to_endpoint: bool = False
    ) -> None:
        self._search_url = f"https://cse.google.com/cse?cx={cse_id}"
        self.cfg = config or BrowserConfig()
        self.browser_type = browser_type
        self._scraped_results: list[ScrapedResult] = []
        self._session_store = SessionStore(settings.SESSION_DIR)
        self._db_manager = db_manager
        self._results_repo = results_repo
        self.__sent_to_endpoint = sent_to_endpoint

    # ── Helpers de timing ────────────────────────────────────────────────────

    async def _human_sleep(self, low: float, high: float) -> None:
        """Pausa con distribución jitter usando la config del automator."""
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    # ── Interacción con la página ────────────────────────────────────────────

    async def _human_type_on_page(self, page: Page, selector: str, text: str) -> None:
        """
        Escribe texto con velocidad gaussiana y errores tipográficos ocasionales.

        Usa la implementación de ``anti_detection.human_behavior`` en lugar
        de reimplementar la lógica aquí.

        Args:
            page:     Página activa.
            selector: Selector CSS del campo de texto.
            text:     Texto a escribir.
        """
        await page.click(selector)
        for char in text:
            # Simular typo (2% de probabilidad)
            if random.random() < 0.02:
                await page.keyboard.type(random.choice("qwertyuiopasdfghjklzxcvbnm"))
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.keyboard.press("Backspace")
            await page.keyboard.type(char)
            delay = max(0.04, random.gauss(self.cfg.typing_mean, self.cfg.typing_std))
            # Pausas extra en espacios y puntuación
            if char in " .,;:!?":
                delay *= random.uniform(1.4, 2.2)
            await asyncio.sleep(delay)

    async def _arc_move_and_click(self, page: Page, locator: Any) -> None:
        """
        Mueve el ratón con arco de Bézier hasta el elemento y hace click.

        Primero intenta obtener el bounding box para apuntar a un punto
        aleatorio dentro del elemento. Si falla, hace click directo.

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

            # Punto de partida aleatorio en el viewport
            viewport = page.viewport_size or {"width": 1280, "height": 800}
            start_x = random.uniform(viewport["width"]  * 0.1, viewport["width"]  * 0.9)
            start_y = random.uniform(viewport["height"] * 0.1, viewport["height"] * 0.7)

            # Movimiento con easing cosenoidal (acceleration/deceleration)
            steps = random.randint(14, 28)
            for step in range(steps + 1):
                t = step / steps
                t_ease = (1 - math.cos(math.pi * t)) / 2
                arc = math.sin(math.pi * t) * random.uniform(-25, 25)
                hyp = max(1, math.hypot(target_x - start_x, target_y - start_y))
                perp_x = -(target_y - start_y) / hyp
                perp_y  =  (target_x - start_x) / hyp
                x = start_x + (target_x - start_x) * t_ease + perp_x * arc
                y = start_y + (target_y - start_y) * t_ease + perp_y * arc
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.004, 0.022))

            await micro_delay(60, 180)
            await page.mouse.click(target_x, target_y)
            await micro_delay(40, 120)

        except Exception as exc:
            logger.debug("arc_move falló, usando click directo: %s", exc)
            await locator.click()

    # ── Stealth y warmup ─────────────────────────────────────────────────────

    @staticmethod
    async def apply_stealth(page: Page, fingerprint: BrowserFingerprint) -> None:
        """
        Inyecta el script de stealth completo como init script de la página.

        Se llama ANTES de cualquier navegación para que los parches estén
        activos cuando la página comience a cargar sus propios scripts.

        Args:
            page:        Página de Playwright recién creada.
            fingerprint: Fingerprint generado para esta sesión.
        """
        await page.add_init_script(fingerprint.stealth_js)
        logger.debug("Stealth init script applied (%d bytes)", len(fingerprint.stealth_js))

    async def _inject_referrer(self, page: Page) -> None:
        """
        Navega a un referrer plausible antes de ir al CSE.

        Simula que el usuario llegó a Google CSE desde una búsqueda normal.
        Algunos sistemas anti-bot verifican ``document.referrer``.

        Args:
            page: Página activa.
        """
        referrer = random.choice(_PLAUSIBLE_REFERRERS)
        try:
            # Navegar al referrer brevemente (sin esperar carga completa)
            await page.goto(
                referrer,
                wait_until="commit",   # Solo espera a que empiece a cargar
                timeout=8_000,
            )
            await asyncio.sleep(random.uniform(0.8, 2.0))
            logger.debug("Referrer injected: %s", referrer)
        except Exception as exc:
            logger.debug("Referrer injection failed (non-critical): %s", exc)

    async def _warmup_session(self, page: Page) -> None:
        """
        Calienta la sesión navegando a Google y simulando actividad humana.

        Un contexto que va directamente al CSE sin pasar por ninguna otra página
        es sospechoso. El warmup establece cookies de Google y simula historial.

        Args:
            page: Página activa (contexto recién creado).
        """
        try:
            await page.goto(
                self.cfg.warmup_url,
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            # Simular lectura de la página de inicio de Google
            await simulate_reading_pause(page, words_estimate=random.randint(10, 30))

            # Scroll aleatorio
            scroll_amount = random.randint(50, 250)
            await page.evaluate(f"window.scrollTo(0, {scroll_amount})")
            await asyncio.sleep(random.uniform(0.4, 1.2))

            # Movimiento de ratón idle
            await simulate_idle(page, duration_seconds=random.uniform(1.0, 2.5))

            logger.debug("Session warmup completed at %s", self.cfg.warmup_url)
        except Exception as exc:
            logger.debug("Warmup ignorado (non-critical): %s", exc)

    # ── Parsing de fechas ────────────────────────────────────────────────────

    @staticmethod
    def _parse_relative_timestamp(snippet_text: str) -> datetime | None:
        """
        Convierte timestamps relativos en español a ``datetime`` UTC.

        Args:
            snippet_text: Texto del snippet (p.ej. "hace 3 horas" o "hace 2 días").

        Returns:
            ``datetime`` en UTC o None si el texto no es un timestamp relativo.
        """
        if not snippet_text:
            return None
        match = _RELATIVE_TS_PATTERN.match(snippet_text.strip())
        if not match:
            return None
        value = int(match.group(2))
        unit_es = match.group(3).lower()
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
        Scroll incremental de la página de resultados simulando lectura humana.

        Alterna ráfagas de scroll con pausas de "lectura" e incluye
        movimientos de ratón ocasionales sobre los resultados.

        Args:
            page: Página activa con resultados CSE.
        """
        page_height = await page.evaluate("document.body.scrollHeight")
        viewport_h  = await page.evaluate("window.innerHeight") or 768
        current_y   = 0.0

        while current_y < page_height:
            # Ráfaga de scroll (similar a arrastrar la rueda del ratón)
            step = random.uniform(viewport_h * 0.25, viewport_h * 0.70)
            current_y = min(current_y + step, page_height)
            await page.evaluate(f"window.scrollTo(0, {current_y})")
            await asyncio.sleep(random.uniform(*self.cfg.scroll_pause_range))

            # Mover el ratón ocasionalmente mientras scrollea (15% de probabilidad)
            if random.random() < 0.15:
                vp = page.viewport_size or {"width": 1280, "height": 800}
                await page.mouse.move(
                    random.uniform(vp["width"] * 0.1, vp["width"] * 0.9),
                    random.uniform(vp["height"] * 0.1, vp["height"] * 0.8),
                )

    async def _extract_page_results(self, page: Page, keyword: str) -> None:
        """
        Extrae resultados de la página actual del CSE.

        Proceso:
          1. Scroll incremental (simula lectura)
          2. Verificar CAPTCHA post-scroll
          3. Esperar selector de resultados
          4. Parsear URLs y timestamps de cada resultado

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
        logger.debug("Parsing %d result blocks for keyword='%s'", count, keyword)

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

                self._scraped_results.append(ScrapedResult(
                    url=clean_url(href),
                    published_at=self._parse_relative_timestamp(snippet_text) if ts_match else None,
                    published_at_raw=ts_match.group(1) if ts_match else None,
                ))
            except Exception as exc:
                logger.error("Error procesando resultado #%d: %s", i, exc)

    # ── Envío al store ───────────────────────────────────────────────────────

    async def _send_urls_to_store(self) -> tuple[int, int]:
        """
        Envía las URLs scrapeadas al API de almacenamiento.

        Usa conexión directa para el API interno.
        ``verify=False`` porque el endpoint interno usa una CA privada.

        Returns:
            Tupla ``(sent_ok, failed)`` con conteos de éxito y fallo.
        """
        valid_urls = [item["url"] for item in self._scraped_results if item.get("url")]
        if not valid_urls:
            return 0, 0

        sent_ok = failed = 0
        async with httpx.AsyncClient(
            headers=_DATA_STORE_HEADERS,
            timeout=10.0,
            proxy=None,        # Conexión directa para el API interno
            verify=settings.DATA_STORE_VERIFY_SSL,
        ) as client:
            for post_url in valid_urls:
                platform = "instagram" if "instagram" in post_url else "facebook"
                endpoint = f"{_DATA_STORE_BASE_URL}/{platform}/urls"
                try:
                    response = await client.post(endpoint, json={"post_url": post_url})
                    if response.is_success:
                        sent_ok += 1
                    else:
                        failed += 1
                        logger.warning(
                            "Store rechazó URL (HTTP %d): %s",
                            response.status_code, post_url[:80],
                        )
                except httpx.RequestError as exc:
                    failed += 1
                    logger.error("Error de conexión enviando URL: %s", exc)

        return sent_ok, failed

    # ── Filtro de fecha en CSE ───────────────────────────────────────────────

    async def _apply_date_filter(self, page: Page) -> None:
        """
        Activa el filtro "Date" del CSE para ordenar por fecha.

        Usa el movimiento de ratón con arco en lugar de click directo
        para que la interacción con el dropdown parezca humana.

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
        """Emite un beep de alerta cuando se detecta un CAPTCHA."""
        try:
            import platform as _platform
            if _platform.system() == "Windows":
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
        Prepara una página nueva con stealth y response interception.

        Debe llamarse justo después de ``context.new_page()`` y ANTES
        de cualquier navegación.

        Args:
            page:        Página recién creada.
            fingerprint: Fingerprint de la sesión actual.
        """
        # Aplicar todos los parches JS antes de cualquier carga
        await self.apply_stealth(page, fingerprint)
        # Interceptar respuestas HTTP para detectar 429/403
        await CaptchaDetector.intercept_response_errors(page)
        logger.debug("Page setup complete: stealth + response interception active")

    async def run_keyword(
        self,
        page: Page,
        keyword: str,
        total_pages: int = 3,
    ) -> list[ScrapedResult]:
        """
        Ejecuta la búsqueda completa de una keyword en el CSE.

        Flujo completo:
          1. Navegar a la URL del CSE
          2. Verificar CAPTCHA
          3. Escribir la keyword con comportamiento humano
          4. Aplicar filtro de fecha
          5. Para cada página: extraer resultados + navegar a siguiente
          6. Comportamiento idle/distracción aleatorio entre páginas
          7. Enviar URLs al store
          8. Guardar sesión persistente

        Args:
            page:        Página activa (debe haberse aplicado ``setup_page`` primero).
            keyword:     Término de búsqueda.
            total_pages: Número máximo de páginas de resultados a procesar.

        Returns:
            Lista de ``ScrapedResult`` extraídos (copia defensiva).

        Raises:
            CaptchaError: Si se detecta CAPTCHA y ``captcha_wait_for_human=False``.
        """
        self._scraped_results.clear()

        # 1. Navegar al motor CSE
        await page.goto(self._search_url, wait_until="domcontentloaded", timeout=20_000)
        await CaptchaDetector.check(page, keyword)
        await self._human_sleep(*self.cfg.page_load_wait_range)

        # 2. Escribir keyword con comportamiento humano
        search_box = page.locator("input.gsc-input")
        await self._arc_move_and_click(page, search_box)
        await self._human_type_on_page(page, "input.gsc-input", keyword)
        await asyncio.sleep(random.uniform(0.3, 0.9))
        await page.keyboard.press("Enter")

        # 3. Esperar carga y verificar CAPTCHA post-submit
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await CaptchaDetector.check(page, keyword)

        # 4. Aplicar filtro de fecha
        await self._apply_date_filter(page)

        # 5. Procesar páginas de resultados
        for current_p in range(1, total_pages + 1):
            logger.info("Procesando página %d/%d para keyword='%s'", current_p, total_pages, keyword)

            try:
                await self._extract_page_results(page, keyword)
            except CaptchaError as cap_err:
                logger.warning(
                    "CAPTCHA detectado (signal=%s). Intentando resolución automática...",
                    cap_err.signal,
                )
                self._play_alert_sound()

                # ── Fase 1: resolver automáticamente el checkbox ──────────────
                auto_solved = await CaptchaAutosolver.try_solve_checkbox(
                    page=page,
                    keyword=keyword,
                    max_attempts=2,
                )
                if auto_solved:
                    logger.info("CAPTCHA resuelto automáticamente. Reextrayendo resultados...")
                    await self._extract_page_results(page, keyword)

                # ── Fase 2: esperar resolución manual (headless=False) ────────
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

                # ── Fase 3: escalar (relanzar CaptchaError al orquestador) ──
                else:
                    raise

            # Comportamiento post-página: idle, distracción ocasional
            if current_p < total_pages:
                # Simular lectura de resultados
                await simulate_reading_pause(page, words_estimate=random.randint(40, 80))

                # Distracción ocasional (mover ratón fuera del contenido)
                if random.random() < self.cfg.distraction_probability:
                    logger.debug("Simulating user distraction between pages")
                    await simulate_distraction(page)

                # Navegar a la siguiente página
                next_selector = f".gsc-cursor-page:not(.gsc-cursor-current-page)"
                next_btn = page.locator(next_selector, has_text=str(current_p + 1))
                if await next_btn.is_visible():
                    await self._arc_move_and_click(page, next_btn)
                    await page.locator(
                        ".gsc-cursor-current-page",
                    ).filter(has_text=str(current_p + 1)).wait_for(
                        state="visible", timeout=12_000
                    )
                    await self._human_sleep(*self.cfg.between_pages_range)
                else:
                    logger.debug("No hay página %d para keyword='%s'", current_p + 1, keyword)
                    break

        if self.__sent_to_endpoint:
            # # 6. Enviar resultados al store
            sent_ok, failed = await self._send_urls_to_store()
            logger.info(
                "Store: %d enviadas, %d fallidas | keyword='%s'",
                sent_ok, failed, keyword,
            )
        else:
            if self._results_repo:
                inserted, skipped = await self._save_results_to_db(keyword)
                logger.info(f"Persistencia: {inserted} nuevos, {skipped} omitidos.")

        return self._scraped_results.copy()

    async def _save_results_to_db(self, keyword: str) -> tuple[int, int]:
        """
        Persiste los resultados scrapeados. Inserta SOLO nuevos (url, keyword).

        Args:
            keyword: Término de búsqueda asociado.

        Returns:
            Tupla ``(insertados, omitidos)``.
        """
        if not self._results_repo:
            logger.warning("Sin repository configurado: resultados no persistidos.")
            return 0, 0

        docs = [
            GoogleResultInDB(
                url=item["url"],
                published_at=item["published_at"],
                published_at_raw=item.get("published_at_raw"),
                keyword=keyword,
            )
            for item in self._scraped_results
            if item["url"]
        ]

        if not docs:
            logger.info("Sin resultados válidos para persistir.")
            return 0, 0

        return await self._results_repo.bulk_insert_skip_existing(docs)