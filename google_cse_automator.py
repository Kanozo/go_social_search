"""
google_cse_automator.py
Motor de scraping sobre Google Custom Search Engine (CSE).

Modo de salida (settings.OUTPUT_MODE)
──────────────────────────────────────
  "sqlite"  → persiste en la tabla posts vía PostRepository (default)
  "api"     → envía al endpoint HTTP externo vía httpx

La instancia recibe el PostRepository ya construido desde el orquestador
para evitar abrir/cerrar la DB en cada keyword.
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
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
)

from anti_detection import (
    BrowserFingerprint,
    generate_fingerprint,
    micro_delay,
    simulate_distraction,
    simulate_idle,
    simulate_reading_pause,
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

_PLAUSIBLE_REFERRERS: list[str] = [
    "https://www.google.com/",
    "https://www.google.com/search?q=site:facebook.com",
    "https://duckduckgo.com/",
    "https://www.bing.com/",
]

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
    platform: str
    published_at: datetime | None
    published_at_raw: str | None


# ─────────────────────────────────────────────────────────────────────────────
# BrowserConfig
# ─────────────────────────────────────────────────────────────────────────────

class BrowserConfig:
    """
    Parámetros de timing y comportamiento del navegador automatizado.
    Todos los rangos en segundos (min, max).
    """

    page_load_wait_range:     tuple[float, float] = (3.5, 7.0)
    scroll_pause_range:       tuple[float, float] = (0.3, 1.1)
    between_pages_range:      tuple[float, float] = (4.0, 11.0)
    between_keywords_range:   tuple[float, float] = (6.0, 18.0)
    warmup_pause_range:       tuple[float, float] = (2.0, 5.0)
    typing_mean:              float = 0.11
    typing_std:               float = 0.04
    warmup_url:               str   = "https://www.google.com"
    captcha_max_wait_seconds: float = 300.0
    captcha_wait_for_human:   bool  = False
    distraction_probability:  float = 0.15

    def jitter_wait(self, low: float, high: float) -> float:
        """Distribución gaussiana + cola exponencial para tiempos de espera."""
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

    El orquestador es responsable del ciclo de vida del browser/contexto y
    de la conexión a SQLite. Esta clase solo recibe una ``Page`` activa y el
    repositorio ya inicializado.

    Args:
        cse_id:       ID del Custom Search Engine de Google.
        platform:     Plataforma objetivo del engine ("instagram", "facebook", …).
        post_repo:    Repositorio de posts (SQLite). None → solo modo "api".
        config:       Instancia de BrowserConfig. Se crea una por defecto si None.
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
        self._search_url = f"https://cse.google.com/cse?cx={cse_id}"
        self._platform = platform
        self._post_repo = post_repo
        self.cfg = config or BrowserConfig()
        self.browser_type = browser_type
        self._scraped_results: list[ScrapedResult] = []
        self._session_store = SessionStore(settings.SESSION_DIR)

    # ── Helpers de timing ────────────────────────────────────────────────────

    async def _human_sleep(self, low: float, high: float) -> None:
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    # ── Interacción con la página ────────────────────────────────────────────

    async def _human_type_on_page(self, page: Page, selector: str, text: str) -> None:
        """Escritura con velocidad gaussiana y typos ocasionales."""
        await page.click(selector)
        for char in text:
            if random.random() < 0.02:
                await page.keyboard.type(random.choice("qwertyuiopasdfghjklzxcvbnm"))
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.keyboard.press("Backspace")
            await page.keyboard.type(char)
            delay = max(0.04, random.gauss(self.cfg.typing_mean, self.cfg.typing_std))
            if char in " .,;:!?":
                delay *= random.uniform(1.4, 2.2)
            await asyncio.sleep(delay)

    async def _arc_move_and_click(self, page: Page, locator: Any) -> None:
        """Movimiento de ratón con arco de Bézier + easing cosenoidal."""
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
        """Inyecta el init script de stealth antes de cualquier navegación."""
        await page.add_init_script(fingerprint.stealth_js)
        logger.debug("Stealth init script applied (%d bytes)", len(fingerprint.stealth_js))

    async def _warmup_session(self, page: Page) -> None:
        """Navega a Google y simula actividad antes de ir al CSE."""
        try:
            await page.goto(self.cfg.warmup_url, wait_until="domcontentloaded", timeout=15_000)
            await simulate_reading_pause(page, words_estimate=random.randint(10, 30))
            await page.evaluate(f"window.scrollTo(0, {random.randint(50, 250)})")
            await asyncio.sleep(random.uniform(0.4, 1.2))
            await simulate_idle(page, duration_seconds=random.uniform(1.0, 2.5))
            logger.debug("Session warmup completado.")
        except Exception as exc:
            logger.debug("Warmup ignorado (non-critical): %s", exc)

    # ── Parsing de fechas ────────────────────────────────────────────────────

    @staticmethod
    def _parse_relative_timestamp(snippet_text: str) -> datetime | None:
        """Convierte "hace N unidad" → datetime UTC."""
        if not snippet_text:
            return None
        match = _RELATIVE_TS_PATTERN.match(snippet_text.strip())
        if not match:
            return None
        value = int(match.group(2))
        delta_unit = _UNIT_MAP.get(match.group(3).lower())
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
        """Scroll incremental simulando lectura humana."""
        page_height = await page.evaluate("document.body.scrollHeight")
        viewport_h  = await page.evaluate("window.innerHeight") or 768
        current_y   = 0.0
        while current_y < page_height:
            step = random.uniform(viewport_h * 0.25, viewport_h * 0.70)
            current_y = min(current_y + step, page_height)
            await page.evaluate(f"window.scrollTo(0, {current_y})")
            await asyncio.sleep(random.uniform(*self.cfg.scroll_pause_range))
            if random.random() < 0.15:
                vp = page.viewport_size or {"width": 1280, "height": 800}
                await page.mouse.move(
                    random.uniform(vp["width"] * 0.1, vp["width"] * 0.9),
                    random.uniform(vp["height"] * 0.1, vp["height"] * 0.8),
                )

    # google_cse_automator.py
    async def block_images_async(
        self,  # ← ¡Agrega esto!
        page: Page, 
        url_pattern: Optional[str] = None,
        log_blocked: bool = False  # ← Opcional: para debugging
    ) -> None:
        """
        Configura interceptor para bloquear imágenes en Playwright.
        
        Args:
            page: Instancia de Page de Playwright.
            url_pattern: Si se proporciona, solo bloquea imágenes en URLs que contengan este patrón.
            log_blocked: Si True, loguea las URLs bloqueadas para debugging.
        """
        async def handle_route(route: Route) -> None:
            try:
                if route.request.resource_type == "image":
                    if url_pattern is None or url_pattern in route.request.url:
                        if log_blocked:
                            logger.debug("🚫 Bloqueada imagen: %s", route.request.url)
                        await route.abort()
                        return
                await route.continue_()
            except Exception as exc:
                # Nunca dejar que un error en el interceptor rompa la navegación
                logger.debug("Error en route handler: %s", exc)
                await route.continue_()
        
        # ⚠️ IMPORTANTE: El route debe registrarse ANTES de cualquier navegación
        await page.route("**/*", handle_route)
        logger.debug("Interceptor de imágenes registrado en página")

    async def _extract_page_results(self, page: Page, keyword: str) -> None:
        """
        Extrae resultados de la página actual del CSE.

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
        logger.debug("Parsing %d result blocks | keyword='%s'", count, keyword)

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

                # Inferir plataforma desde la URL si no está en el engine
                url_clean = clean_url(href)
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

    # ── Persistencia: SQLite ─────────────────────────────────────────────────

    async def _save_to_sqlite(self, keyword: str) -> tuple[int, int]:
        """
        Persiste los resultados en SQLite vía PostRepository.

        Inserta solo URLs nuevas (INSERT OR IGNORE). Las URLs que ya existen
        se omiten silenciosamente sin lanzar excepción.

        Args:
            keyword: Keyword que originó los resultados (se guarda en cada fila).

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

    # ── Persistencia: API HTTP ───────────────────────────────────────────────

    async def _send_to_api(self) -> tuple[int, int]:
        """
        Envía las URLs al endpoint HTTP externo.

        Usa conexión directa (no proxy). ``verify=False`` porque el endpoint
        interno usa una CA privada.

        Returns:
            Tupla ``(sent_ok, failed)``.
        """
        valid_urls = [
            (item["url"], item["platform"])
            for item in self._scraped_results
            if item.get("url")
        ]
        if not valid_urls:
            return 0, 0

        sent_ok = failed = 0
        async with httpx.AsyncClient(
            headers=_DATA_STORE_HEADERS,
            timeout=10.0,
            verify=settings.DATA_STORE_VERIFY_SSL,
        ) as client:
            for post_url, platform in valid_urls:
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
                    logger.error("Error de conexión enviando URL: %s", exc)

        return sent_ok, failed

    # ── Router de persistencia ───────────────────────────────────────────────

    async def _persist_results(self, keyword: str) -> None:
        """
        Enruta la persistencia al backend configurado en ``settings.OUTPUT_MODE``.

        Modos soportados:
          - "sqlite" → inserta en la tabla posts vía PostRepository
          - "api"    → envía al endpoint HTTP externo

        Cualquier valor distinto produce un warning y no hace nada,
        para evitar pérdida silenciosa de datos.

        Args:
            keyword: Keyword que originó los resultados.
        """
        mode = settings.OUTPUT_MODE.strip().lower()

        if mode == "sqlite":
            inserted, skipped = await self._save_to_sqlite(keyword)
            logger.info(
                "SQLite | %d insertados, %d omitidos | keyword='%s'",
                inserted, skipped, keyword,
            )

        elif mode == "api":
            sent_ok, failed = await self._send_to_api()
            logger.info(
                "API | %d enviadas, %d fallidas | keyword='%s'",
                sent_ok, failed, keyword,
            )

        else:
            logger.warning(
                "OUTPUT_MODE='%s' no reconocido. "
                "Valores válidos: 'sqlite', 'api'. "
                "Los resultados NO se persistieron.",
                settings.OUTPUT_MODE,
            )

    # ── Filtro de fecha en CSE ───────────────────────────────────────────────

    async def _apply_date_filter(self, page: Page) -> None:
        """Activa el filtro 'Date' del CSE para ordenar por fecha."""
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
        Prepara una página nueva: inyecta stealth + interceptor de respuestas HTTP.
        Debe llamarse ANTES de cualquier navegación.
        """
        await self.apply_stealth(page, fingerprint)
        await CaptchaDetector.intercept_response_errors(page)
        logger.debug("Page setup: stealth + response interception active.")

    async def run_keyword(
        self,
        page: Page,
        keyword: str,
        total_pages: int = 3,
    ) -> list[ScrapedResult]:
        """
        Ejecuta la búsqueda completa de un keyword en el CSE.

        Flujo:
          1. Navegar al CSE → verificar CAPTCHA
          2. Escribir keyword con comportamiento humano
          3. Aplicar filtro de fecha
          4. Por cada página: extraer resultados → persistir → pausa → siguiente
          5. Ante CAPTCHA: auto-solver → espera manual → escalar al orquestador

        Args:
            page:        Página activa (``setup_page`` debe haberse llamado antes).
            keyword:     Término de búsqueda.
            total_pages: Páginas máximas de resultados a procesar.

        Returns:
            Copia defensiva de los resultados scrapeados en esta ejecución.

        Raises:
            CaptchaError: Si el CAPTCHA no pudo resolverse y hay que rotar identidad.
        """
        self._scraped_results.clear()

        # 1. Navegar al motor CSE
        await page.goto(self._search_url, wait_until="domcontentloaded", timeout=20_000)
        await CaptchaDetector.check(page, keyword)
        await self._human_sleep(*self.cfg.page_load_wait_range)

        # 2. Escribir keyword
        search_box = page.locator("input.gsc-input")
        await self._arc_move_and_click(page, search_box)
        await self._human_type_on_page(page, "input.gsc-input", keyword)
        await asyncio.sleep(random.uniform(0.3, 0.9))
        await page.keyboard.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=15_000)
        await CaptchaDetector.check(page, keyword)

        # 3. Aplicar filtro de fecha
        await self._apply_date_filter(page)

        # 4. Procesar páginas de resultados
        for current_p in range(1, total_pages + 1):
            logger.info(
                "Página %d/%d | keyword='%s'", current_p, total_pages, keyword
            )

            try:
                await self._extract_page_results(page, keyword)

            except CaptchaError as cap_err:
                logger.warning(
                    "CAPTCHA (signal=%s). Intentando resolución automática...",
                    cap_err.signal,
                )
                self._play_alert_sound()

                # Fase 1: auto-solver de checkbox
                auto_solved = await CaptchaAutosolver.try_solve_checkbox(
                    page=page, keyword=keyword, max_attempts=2
                )
                if auto_solved:
                    logger.info("CAPTCHA resuelto. Reextrayendo resultados...")
                    await self._extract_page_results(page, keyword)

                # Fase 2: espera resolución manual
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

                # Fase 3: escalar al orquestador
                else:
                    raise

            # Persistir resultados de esta página antes de continuar
            await self._persist_results(keyword)
            # Limpiar buffer para la siguiente página (evitar re-insertar)
            self._scraped_results.clear()

            # Comportamiento post-página
            if current_p < total_pages:
                await simulate_reading_pause(page, words_estimate=random.randint(40, 80))

                if random.random() < self.cfg.distraction_probability:
                    logger.debug("Simulando distracción entre páginas.")
                    await simulate_distraction(page)

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
                        "No hay página %d para keyword='%s'", current_p + 1, keyword
                    )
                    break

        # Retornar lista vacía (ya se persistió y limpió por páginas)
        return []