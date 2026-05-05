"""
google_cse_automator.py  (v4 — sin MongoDB, envío a data_url_store)

Cambios respecto a v3:
    - Eliminadas todas las referencias a DatabaseManager, GoogleResultRepository
      y GoogleResultInDB.
    - _save_results_to_db() reemplazado por _send_urls_to_store(): envía cada
      URL individualmente al endpoint http://data_url_store/url vía POST async
      usando httpx.
    - Parámetro persist_to_db eliminado de run_with_page() y run().
    - Token de autenticación leído desde settings.DATA_STORE_TOKEN.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple, TypedDict

import httpx
from dateutil.relativedelta import relativedelta
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config.settings import settings
from utils.captcha_guard import CaptchaDetector, CaptchaError
from utils.fb_url_validator import is_valid_fb_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuración del store de URLs
# ---------------------------------------------------------------------------

_DATA_STORE_ENDPOINT = "https://notires.rem.cu/api/facebook/urls"
DATA_STORE_TOKEN = "42|htoFv3uJ8ZIJMuWoSDQkmLOK0vnv5GSoGbQaKDWBf2cb6b41"
_DATA_STORE_HEADERS = {
    "Authorization": f"Bearer {DATA_STORE_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Pools de valores para rotación
# ---------------------------------------------------------------------------

_USER_AGENT_POOL: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

_VIEWPORT_POOL: list[dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

_LOCALE_TZ_POOL: list[tuple[str, str]] = [
    ("en-US", "America/New_York"),
    ("en-US", "America/Chicago"),
    ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"),
    ("en-CA", "America/Toronto"),
]

_RELATIVEDELTA_UNITS = frozenset({"months", "years"})

_UNIT_MAP: dict[str, str] = {
    "minuto": "minutes", "minutos": "minutes",
    "hora": "hours",     "horas": "hours",
    "día": "days",       "días": "days",
    "semana": "weeks",   "semanas": "weeks",
    "mes": "months",     "meses": "months",
    "año": "years",      "años": "years",
}

_RELATIVE_TS_PATTERN = re.compile(
    r"^(hace\s+(\d+)\s+"
    r"(hora|horas|minuto|minutos|día|días|semana|semanas|mes|meses|año|años))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# BrowserConfig
# ---------------------------------------------------------------------------

class BrowserConfig:
    """Parámetros de configuración con pools de rotación anti-fingerprint."""

    user_agent_pool: list[str] = _USER_AGENT_POOL
    viewport_pool: list[dict[str, int]] = _VIEWPORT_POOL
    locale_tz_pool: list[tuple[str, str]] = _LOCALE_TZ_POOL

    page_load_wait_range: tuple[float, float] = (4.0, 7.0)
    scroll_pause_range: tuple[float, float] = (0.4, 1.2)
    between_pages_range: tuple[float, float] = (4.0, 10.0)
    between_keywords_range: tuple[float, float] = (5.0, 15.0)

    typing_mean: float = 0.12
    typing_std: float = 0.05

    warmup_url: str = "https://www.google.com"
    warmup_pause_range: tuple[float, float] = (2.0, 4.0)

    # ── Opciones de CAPTCHA ──────────────────────────────────────────────
    captcha_wait_for_human: bool = True
    captcha_max_wait_seconds: float = 300.0

    def pick_user_agent(self) -> str:
        return random.choice(self.user_agent_pool)

    def pick_viewport(self) -> dict[str, int]:
        return random.choice(self.viewport_pool)

    def pick_locale_tz(self) -> tuple[str, str]:
        return random.choice(self.locale_tz_pool)

    def jitter_wait(self, low: float, high: float) -> float:
        """Tiempo de espera con distribución gaussiana + cola exponencial."""
        mid = (low + high) / 2.0
        sigma = (high - low) / 6.0
        base = random.gauss(mid, sigma)
        extra = random.expovariate(5.0)
        return max(low, min(high + extra * 0.5, base + extra * 0.2))


# ---------------------------------------------------------------------------
# Tipos de datos
# ---------------------------------------------------------------------------

class ScrapedResult(TypedDict):
    url: str
    published_at: Optional[datetime]
    published_at_raw: Optional[str]


# ---------------------------------------------------------------------------
# GoogleCSEAutomator
# ---------------------------------------------------------------------------

class GoogleCSEAutomator:
    """
    Orquestador de scraping para Google CSE con detección de CAPTCHA integrada.

    Puntos de detección:
        [P1] Tras page.goto(search_url)             — URL de bloqueo inmediata.
        [P2] Tras wait_for_load_state("networkidle") — página cargada es CAPTCHA.
        [P3] En extract_page_results, antes del wait_for_selector — detección
             temprana que evita el timeout de 10s.
        [P4] En el except de TimeoutError de wait_for_selector — confirmación
             tardía cuando P3 no fue suficiente.
    """

    def __init__(
        self,
        cse_id: str,
        config: Optional[BrowserConfig] = None,
    ) -> None:
        self._search_url = f"https://cse.google.com/cse?cx={cse_id}"
        self._scraped_results: list[ScrapedResult] = []
        self.cfg = config or BrowserConfig()

    # ------------------------------------------------------------------ #
    # Helpers de temporización                                            #
    # ------------------------------------------------------------------ #

    async def _human_sleep(self, low: float, high: float) -> None:
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    async def _human_type(self, page: Page, selector: str, text: str) -> None:
        """Escribe texto carácter a carácter con delays gaussianos."""
        await page.click(selector)
        for char in text:
            await page.keyboard.type(char)
            delay = max(0.04, random.gauss(self.cfg.typing_mean, self.cfg.typing_std))
            await asyncio.sleep(delay)

    async def _arc_move_and_click(self, page: Page, locator: Any) -> None:
        """Mueve el mouse en arco senoidal antes de hacer click."""
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click()
                return

            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            viewport = self.cfg.pick_viewport()
            start_x = random.uniform(0, viewport["width"])
            start_y = random.uniform(0, viewport["height"])
            steps = random.randint(12, 25)

            for step in range(steps + 1):
                t = step / steps
                t_ease = (1 - math.cos(math.pi * t)) / 2
                arc_offset = math.sin(math.pi * t) * random.uniform(-30, 30)
                hyp = max(1, math.hypot(target_x - start_x, target_y - start_y))
                perp_x = -(target_y - start_y) / hyp
                perp_y = (target_x - start_x) / hyp
                x = start_x + (target_x - start_x) * t_ease + perp_x * arc_offset
                y = start_y + (target_y - start_y) * t_ease + perp_y * arc_offset
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.005, 0.025))

            await page.mouse.click(target_x, target_y)
        except Exception as exc:
            logger.debug(f"arc_move falló, click directo: {exc}")
            await locator.click()

    async def _incremental_scroll(self, page: Page) -> None:
        """Scroll incremental con velocidad variable."""
        page_height: int = await page.evaluate("document.body.scrollHeight")
        current_y: float = 0.0
        viewport_h: int = await page.evaluate("window.innerHeight") or 768
        while current_y < page_height:
            scroll_step = random.uniform(viewport_h * 0.3, viewport_h * 0.8)
            current_y = min(current_y + scroll_step, page_height)
            await page.evaluate(f"window.scrollTo(0, {current_y})")
            await asyncio.sleep(random.uniform(*self.cfg.scroll_pause_range))

    # ------------------------------------------------------------------ #
    # Stealth                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _apply_stealth(page: Page) -> None:
        """Inyecta scripts CDP para eliminar marcadores de automatización."""
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined, configurable: true,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const p = [
                        {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
                        {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
                        {name:'Native Client',filename:'internal-nacl-plugin',description:''},
                    ];
                    p.refresh=()=>{}; p.item=(i)=>p[i]; p.namedItem=(n)=>p.find(x=>x.name===n)||null;
                    Object.defineProperty(p,'length',{get:()=>p.length});
                    return p;
                }, configurable: true,
            });
            if (!window.chrome) {
                window.chrome = {runtime:{},loadTimes:function(){},csi:function(){},app:{}};
            }
            Object.defineProperty(Notification,'permission',{get:()=>'default',configurable:true});
            const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const d = ctx.getImageData(0,0,this.width||1,this.height||1);
                    for (let i=0;i<d.data.length;i+=100) d.data[i]^=(Math.random()*2)|0;
                    ctx.putImageData(d,0,0);
                }
                return _toDataURL.apply(this,arguments);
            };
        """)

    # ------------------------------------------------------------------ #
    # Warmup                                                              #
    # ------------------------------------------------------------------ #

    async def _warmup_session(self, page: Page) -> None:
        """Navega a google.com antes del CSE para establecer cookies de sesión."""
        try:
            await page.goto(self.cfg.warmup_url, wait_until="domcontentloaded")
            await self._human_sleep(*self.cfg.warmup_pause_range)
            await page.evaluate(f"window.scrollTo(0, {random.randint(50, 200)})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            logger.debug("Warmup completado.")
        except Exception as exc:
            logger.debug(f"Warmup ignorado: {exc}")

    # ------------------------------------------------------------------ #
    # Lógica de scraping                                                  #
    # ------------------------------------------------------------------ #

    async def solve_date_filter(self, page: Page) -> None:
        """Activa el filtro 'Date' en la UI de Google CSE."""
        try:
            dropdown = page.locator(".gsc-selected-option-container").first
            await self._arc_move_and_click(page, dropdown)
            date_option = page.locator(".gsc-option-menu-item", has_text="Date")
            await date_option.wait_for(state="visible", timeout=5000)
            await self._arc_move_and_click(page, date_option)
            logger.info("Filtro 'Date' activado.")
            await page.wait_for_load_state("networkidle")
            await self._human_sleep(1.5, 3.0)
        except Exception as exc:
            logger.warning(f"No se pudo activar filtro de fecha: {exc}")

    @staticmethod
    def _parse_datetime_from_relative(snippet_text: str) -> Optional[datetime]:
        """Convierte timestamp relativo español a datetime UTC."""
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
            logger.warning(f"Error calculando datetime '{snippet_text[:40]}': {exc}")
            return None

    async def extract_page_results(self, page: Page, keyword: str) -> None:
        """
        Extrae URLs y timestamps de la página actual.

        Puntos de detección de CAPTCHA:
            [P3] Antes de wait_for_selector — check proactivo.
            [P4] En el except de TimeoutError — confirmación tardía.

        Args:
            page:    Página activa de Playwright.
            keyword: Keyword activa (necesaria para CaptchaError).
        """
        await self._incremental_scroll(page)

        # ── [P3] Detección proactiva ANTES del wait_for_selector ─────────
        await CaptchaDetector.check(page, keyword)

        try:
            await page.wait_for_selector(".gsc-webResult", timeout=10_000)
        except PlaywrightTimeoutError:
            # ── [P4] Timeout: re-chequear si es CAPTCHA o simplemente sin resultados
            logger.warning(
                "Timeout esperando .gsc-webResult. "
                "Verificando si es CAPTCHA o búsqueda sin resultados..."
            )
            await CaptchaDetector.check(page, keyword)
            logger.info(f"Sin resultados para keyword='{keyword}' (timeout legítimo).")
            return

        results_container = page.locator(".gsc-expansionArea .gsc-webResult")
        count = await results_container.count()
        page_count = 0

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
                if not is_valid_fb_url(href):
                    logger.debug(f"URL filtrada: {href[:80]}")
                    continue

                snippet_text = (
                    await container.locator(".gs-snippet").first.text_content() or ""
                ).strip()

                ts_match = _RELATIVE_TS_PATTERN.match(snippet_text)
                published_at_raw = ts_match.group(1) if ts_match else None
                published_at = (
                    self._parse_datetime_from_relative(snippet_text) if ts_match else None
                )
                if not ts_match:
                    logger.debug(f"Timestamp no parseado: {snippet_text[:60]!r}")

                self._scraped_results.append(
                    ScrapedResult(
                        url=href,
                        published_at=published_at,
                        published_at_raw=published_at_raw,
                    )
                )
                page_count += 1

            except Exception as exc:
                logger.error(f"Error procesando resultado #{i}: {exc}")

        logger.info(f"{page_count} resultados extraídos de esta página.")

    async def _send_urls_to_store(self) -> Tuple[int, int]:
        """
        Envía cada URL scrapeada al endpoint data_url_store, una por una.

        Usa un cliente httpx async compartido por el lote para reutilizar
        la conexión HTTP. SSL deshabilitado (verify=False) según configuración
        original del servicio.

        Returns:
            Tuple (enviadas_ok, fallidas) con los conteos del lote.
        """
        valid_urls = [item["url"] for item in self._scraped_results if item["url"]]
        if not valid_urls:
            return 0, 0

        sent_ok = 0
        failed = 0

        async with httpx.AsyncClient(
            headers=_DATA_STORE_HEADERS,
            verify=False,
            timeout=10.0,
        ) as client:
            for post_url in valid_urls:
                try:
                    response = await client.post(
                        _DATA_STORE_ENDPOINT,
                        json={"post_url": post_url},
                    )
                    if response.is_success:
                        sent_ok += 1
                        logger.debug(f"URL enviada OK: {post_url[:80]}")
                    else:
                        failed += 1
                        logger.warning(
                            f"Store rechazó URL (HTTP {response.status_code}): "
                            f"{post_url[:80]}"
                        )
                except httpx.RequestError as exc:
                    failed += 1
                    logger.error(f"Error de conexión enviando URL '{post_url[:80]}': {exc}")

        return sent_ok, failed

    # ------------------------------------------------------------------ #
    # Context factory                                                     #
    # ------------------------------------------------------------------ #

    async def create_stealth_context(self, browser: Browser) -> BrowserContext:
        """Crea BrowserContext con parámetros rotados anti-fingerprint."""
        locale, tz_id = self.cfg.pick_locale_tz()
        viewport = self.cfg.pick_viewport()
        return await browser.new_context(
            user_agent=self.cfg.pick_user_agent(),
            locale=locale,
            timezone_id=tz_id,
            viewport=viewport,
            extra_http_headers={
                "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
            },
            java_script_enabled=True,
        )

    # ------------------------------------------------------------------ #
    # Entry points                                                        #
    # ------------------------------------------------------------------ #

    async def run_with_page(
        self,
        page: Page,
        keyword: str,
        total_pages: int = 3,
    ) -> list[ScrapedResult]:
        """
        Ejecuta el scraping sobre una Page ya abierta y envía las URLs al store.

        Manejo de CaptchaError:
            - Si cfg.captcha_wait_for_human=True: espera resolución manual
              y reintenta extract_page_results en la misma página.
            - Si False: propaga la excepción al caller.

        Args:
            page:        Página con stealth aplicado.
            keyword:     Término de búsqueda.
            total_pages: Páginas a extraer.

        Returns:
            Lista de ScrapedResult extraídos.
        """
        self._scraped_results.clear()

        await page.goto(self._search_url, wait_until="domcontentloaded")

        # ── [P1] Detección tras goto ──────────────────────────────────────
        await CaptchaDetector.check(page, keyword)
        await self._human_sleep(*self.cfg.page_load_wait_range)

        search_box = page.locator("input.gsc-input")
        await self._arc_move_and_click(page, search_box)
        await self._human_type(page, "input.gsc-input", keyword)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle")

        # ── [P2] Detección tras carga de resultados de búsqueda ──────────
        await CaptchaDetector.check(page, keyword)

        await self.solve_date_filter(page)

        for current_p in range(1, total_pages + 1):
            try:
                await self.extract_page_results(page, keyword)

            except CaptchaError:
                if self.cfg.captcha_wait_for_human:
                    logger.warning(
                        "Modo espera manual activo. "
                        "El operador debe resolver el CAPTCHA en el navegador."
                    )
                    resolved = await CaptchaDetector.wait_for_human_resolution(
                        page=page,
                        keyword=keyword,
                        max_wait=self.cfg.captcha_max_wait_seconds,
                    )
                    if resolved:
                        await self.extract_page_results(page, keyword)
                    else:
                        logger.error(
                            f"CAPTCHA no resuelto a tiempo para '{keyword}'. "
                            "Abortando keyword."
                        )
                        raise
                else:
                    raise

            if current_p < total_pages:
                next_p = current_p + 1

                # Botón de página siguiente: mismo número pero NO el actual
                next_page_btn = page.locator(
                    ".gsc-cursor-page:not(.gsc-cursor-current-page)",
                    has_text=str(next_p),
                )

                if await next_page_btn.is_visible():
                    logger.info(f"Navegando a página {next_p}...")
                    await self._arc_move_and_click(page, next_page_btn)

                    # Confirmar que la página cambió: el indicador actual muestra el número
                    await page.locator(".gsc-cursor-current-page") \
                            .filter(has_text=str(next_p)) \
                            .wait_for(state="visible", timeout=10_000)

                    await self._human_sleep(*self.cfg.between_pages_range)
                else:
                    logger.info("Sin más páginas.")
                    break

        sent_ok, failed = await self._send_urls_to_store()
        logger.info(
            f"Store: {sent_ok} URLs enviadas, {failed} fallidas "
            f"(keyword='{keyword}')."
        )

        return self._scraped_results.copy()

    async def run(
        self,
        keyword: str,
        total_pages: int = 3,
        headless: bool = False,
    ) -> list[ScrapedResult]:
        """Standalone: crea y cierra su propio browser."""
        self._scraped_results.clear()
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                ],
            )
            context = await self.create_stealth_context(browser)
            page = await context.new_page()
            await self._apply_stealth(page)
            try:
                await self._warmup_session(page)
                return await self.run_with_page(
                    page=page,
                    keyword=keyword,
                    total_pages=total_pages,
                )
            finally:
                await browser.close()