"""
Google Advanced Search scraper using Playwright async.

Busca keywords en facebook.com e instagram.com usando Google Advanced Search,
recopila URLs por tabs (Todo, Noticias, Imágenes, Videos, Videos cortos, Web)
y persiste los resultados página a página para máxima resiliencia.
"""

import asyncio
import logging
import random
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config.settings import settings
from database import KeywordRepository, PostCreate, PostRepository, SQLiteManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("google_scraper")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
ADVANCED_SEARCH_URL = "https://www.google.com/advanced_search"
MAX_RESULT_PAGES = settings.TOTAL_PAGES_PER_KEYWORD
AUTH_WAIT_TIMEOUT_MS = 120_000
NAVIGATION_TIMEOUT_MS = 30_000
SCROLL_PAUSE_MS = (800, 1_400)
PAGE_LOAD_PAUSE_MS = (1_500, 3_000)
TYPING_DELAY_MS = (60, 130)

TARGET_SITES = ["facebook.com", "instagram.com"]
SCROLL_ONLY_TABS: frozenset[str] = frozenset({"Imágenes", "Vídeos", "Vídeos cortos"})

_DATA_STORE_HEADERS: dict[str, str] = {
    "Authorization": f"Bearer {settings.DATA_STORE_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)

# Script inyectado una sola vez por contexto — oculta señales de automatización
_ANTI_DETECTION_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    window.chrome = { runtime: {} };
"""


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------
class TabName(StrEnum):
    TODO          = "Todo"
    NOTICIAS      = "Noticias"
    IMAGENES      = "Im\u00e1genes"
    VIDEOS        = "V\u00eddeos"
    VIDEOS_CORTOS = "V\u00eddeos cortos"
    WEB           = "Web"


# Texto parcial sin tildes problemáticas para selectores Playwright
_TAB_PARTIAL_TEXT: dict[TabName, str] = {
    TabName.TODO:          "Todo",
    TabName.NOTICIAS:      "Noticias",
    TabName.IMAGENES:      "m\u00e1genes",
    TabName.VIDEOS:        "deos",
    TabName.VIDEOS_CORTOS: "deos cortos",
    TabName.WEB:           "Web",
}

_TAB_SEQUENCE: tuple[TabName, ...] = (
    TabName.NOTICIAS,
    TabName.IMAGENES,
    TabName.VIDEOS,
    TabName.VIDEOS_CORTOS,
    TabName.WEB,
)


# ---------------------------------------------------------------------------
# Helpers anti-scraping (funciones puras, sin estado)
# ---------------------------------------------------------------------------
async def _random_sleep(min_ms: int, max_ms: int) -> None:
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def _human_type(page: Page, selector: str, text: str) -> None:
    """Simula escritura humana con delays variables por carácter."""
    await page.click(selector)
    await page.fill(selector, "")
    for char in text:
        await page.type(selector, char, delay=random.randint(*TYPING_DELAY_MS))
        if random.random() < 0.05:
            await _random_sleep(200, 500)


async def _move_mouse_randomly(page: Page) -> None:
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    x = random.randint(100, viewport["width"] - 100)
    y = random.randint(100, viewport["height"] - 100)
    await page.mouse.move(x, y)


async def _scroll_to_bottom(page: Page) -> None:
    """Scroll progresivo hasta el final de la página."""
    last_height: int = await page.evaluate("document.body.scrollHeight")
    while True:
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await _random_sleep(*SCROLL_PAUSE_MS)
        await _move_mouse_randomly(page)
        new_height: int = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height


# ---------------------------------------------------------------------------
# Extracción de URLs (función pura)
# ---------------------------------------------------------------------------
def _extract_domain_urls(raw_urls: list[str], target_domain: str) -> list[str]:
    """
    Filtra y normaliza URLs que pertenecen al dominio objetivo.

    Args:
        raw_urls: Lista de URLs crudas extraídas del DOM.
        target_domain: Dominio a filtrar (ej. 'facebook.com').

    Returns:
        Lista de URLs únicas del dominio objetivo, preservando orden.
    """
    filtered: list[str] = []
    for url in raw_urls:
        try:
            parsed = urlparse(url)
            # Google redirige por /url?q=... — extraer la URL real
            if parsed.path == "/url" and "q=" in (parsed.query or ""):
                qs = urllib.parse.parse_qs(parsed.query)
                real_urls = qs.get("q", [])
                url = real_urls[0] if real_urls else url
                parsed = urlparse(url)

            hostname = parsed.netloc.lower().lstrip("www.")
            if target_domain in hostname and parsed.scheme in ("http", "https"):
                filtered.append(url)
        except Exception:
            continue
    return list(dict.fromkeys(filtered))


def _build_posts(urls: list[str], keyword: str) -> list[PostCreate]:
    """Convierte URLs en PostCreate infiriendo la plataforma del hostname."""
    return [
        PostCreate(
            keyword=keyword,
            url=url,
            platform="facebook" if "facebook" in url else "instagram",
        )
        for url in urls
    ]


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
async def _save_to_sqlite(posts: list[PostCreate]) -> tuple[int, int]:
    """
    Inserta posts en SQLite vía PostRepository (INSERT OR IGNORE).

    Returns:
        Tupla (insertados, omitidos).
    """
    if not posts:
        return 0, 0

    db_path = settings.SESSION_DIR.parent / "url_scraper.db"
    db = SQLiteManager(db_path)
    await db.connect()
    try:
        repo = PostRepository(db)
        return await repo.bulk_insert_new(posts)
    finally:
        await db.disconnect()


async def _send_to_api(posts: list[PostCreate]) -> tuple[int, int]:
    """
    Envía posts al endpoint HTTP externo.

    Returns:
        Tupla (enviados_ok, fallidos).
    """
    valid = [(item.url, item.platform) for item in posts if item.url]
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


async def _save_results(posts: list[PostCreate]) -> None:
    """
    Enruta la persistencia al backend configurado en ``settings.OUTPUT_MODE``.

    Modos soportados: ``"sqlite"`` | ``"api"``
    """
    if not posts:
        return

    mode = settings.OUTPUT_MODE.strip().lower()

    if mode == "sqlite":
        inserted, skipped = await _save_to_sqlite(posts)
        logger.info("[SQLite] %d insertados, %d omitidos (total batch: %d)", inserted, skipped, len(posts))
    elif mode == "api":
        sent_ok, failed = await _send_to_api(posts)
        logger.info("[API] %d enviadas, %d fallidas (total batch: %d)", sent_ok, failed, len(posts))
    else:
        logger.warning(
            "OUTPUT_MODE='%s' no reconocido (válidos: 'sqlite', 'api'). "
            "Resultados NO persistidos.",
            settings.OUTPUT_MODE,
        )


# ---------------------------------------------------------------------------
# Clase principal
# ---------------------------------------------------------------------------
class GoogleScrapingService:
    """
    Servicio de scraping de Google Advanced Search.

    Mantiene el browser vivo durante todo el ciclo de vida y crea
    un contexto nuevo y aislado por cada combinación keyword×sitio,
    garantizando el perfil anti-detección sin acumular estado entre búsquedas.

    Usage:
        async with GoogleScrapingService() as service:
            await service.run()
    """

    def __init__(self) -> None:
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "GoogleScrapingService":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        logger.info("Browser iniciado")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._close_context()
        if self._browser:
            await self._browser.close()
        await self._playwright.stop()
        logger.info("Browser cerrado")

    # ------------------------------------------------------------------
    # Gestión de contexto
    # ------------------------------------------------------------------
    async def _close_context(self) -> None:
        """Cierra el contexto activo sin tocar el browser."""
        if self._context:
            try:
                await self._context.close()
            except Exception as exc:
                logger.debug("Error cerrando contexto (ignorado): %s", exc)
            finally:
                self._context = None
                self._page = None

    async def _new_context(self) -> Page:
        """
        Crea un contexto limpio con fingerprint aleatorio y devuelve la página.
        Cierra el contexto previo si existe.
        """
        await self._close_context()

        self._context = await self._browser.new_context(  # type: ignore[union-attr]
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 800},
            locale="es-ES",
            timezone_id="America/Havana",
            extra_http_headers={
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        await self._context.add_init_script(_ANTI_DETECTION_SCRIPT)
        self._page = await self._context.new_page()
        return self._page

    # ------------------------------------------------------------------
    # Detección de bloqueos
    # ------------------------------------------------------------------
    async def _check_and_handle_auth(self, page: Page) -> bool:
        """
        Detecta CAPTCHA / login de Google y espera resolución manual (2 min).

        Returns:
            True si no hay bloqueo o se resolvió, False si timeout.
        """
        captcha_selectors = [
            "form#captcha-form",
            "div#recaptcha",
            "iframe[src*='recaptcha']",
            "div.g-recaptcha",
        ]
        for selector in captcha_selectors:
            if await page.query_selector(selector):
                logger.warning("⚠️  CAPTCHA detectado — tienes 2 minutos para resolverlo...")
                try:
                    await page.wait_for_function(
                        "!document.querySelector('form#captcha-form') && "
                        "!document.querySelector('div#recaptcha')",
                        timeout=AUTH_WAIT_TIMEOUT_MS,
                    )
                    logger.info("✅ CAPTCHA resuelto")
                    return True
                except PlaywrightTimeoutError:
                    logger.error("❌ Timeout esperando CAPTCHA — contexto será renovado")
                    return False
        return True

    async def _click_reset_tools_if_present(self, page: Page) -> None:
        """Hace click en 'Restablecer las herramientas de búsqueda' si aparece."""
        reset_selectors = [
            "a[href*='tbas=0']",
            "a:has-text('Restablecer las herramientas de búsqueda')",
            "text=Restablecer las herramientas de búsqueda",
        ]
        reset_link: Optional[Locator] = None
        for selector in reset_selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=2_000)
                reset_link = locator
                logger.warning("⚠️ Banner 'Restablecer herramientas' detectado (%s)", selector)
                break
            except PlaywrightTimeoutError:
                continue

        if reset_link:
            try:
                await reset_link.click()
                await _random_sleep(*PAGE_LOAD_PAUSE_MS)
            except Exception as exc:
                logger.debug("Error haciendo click en reset tools: %s", exc)

    # ------------------------------------------------------------------
    # Formulario de búsqueda
    # ------------------------------------------------------------------
    async def _select_date_filter_24h(self, page: Page) -> None:
        """Selecciona 'Últimas 24 horas' en el combobox de fecha."""
        try:
            label = page.locator("label.mNAued", has_text="ltima actualizaci")
            await label.wait_for(timeout=5_000)

            container = label.locator("xpath=following-sibling::div[1]")
            combobox = container.locator("div[role='combobox']")
            await combobox.scroll_into_view_if_needed()
            await _random_sleep(200, 400)
            await combobox.click()
            await _random_sleep(300, 500)

            option = container.locator("div[role='option'][data-v='d']")
            await option.wait_for(state="visible", timeout=3_000)
            await option.click()
            await _random_sleep(300, 500)

            qdr_value = await page.input_value("input[name='as_qdr']")
            logger.debug("Filtro de fecha aplicado: as_qdr='%s'", qdr_value)

        except PlaywrightTimeoutError:
            logger.warning("Fallback: forzando as_qdr='d' via JS")
            await page.evaluate("""
                const input = document.querySelector("input[name='as_qdr']");
                if (input) {
                    input.value = 'd';
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                }
            """)
        except Exception as exc:
            logger.warning("Error seleccionando filtro de fecha: %s", exc)

    async def _perform_advanced_search(
        self,
        page: Page,
        keyword: str,
        site: str,
    ) -> bool:
        """
        Rellena y envía el formulario de Google Advanced Search.

        Returns:
            True si la búsqueda se realizó correctamente.
        """
        logger.info("Abriendo búsqueda avanzada: '%s' en %s", keyword, site)

        try:
            await page.goto(ADVANCED_SEARCH_URL, timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.error("Timeout navegando a Google Advanced Search")
            return False

        await _random_sleep(*PAGE_LOAD_PAUSE_MS)

        if not await self._check_and_handle_auth(page):
            return False

        await _human_type(page, "input[name='as_epq']", keyword)
        await _random_sleep(400, 800)
        await _human_type(page, "input[name='as_sitesearch']", site)
        await _random_sleep(600, 1_000)

        try:
            await self._select_date_filter_24h(page)
        except Exception as exc:
            logger.warning("No se pudo seleccionar filtro de fecha: %s", exc)

        await _move_mouse_randomly(page)
        await _random_sleep(400, 700)

        search_button = await page.query_selector(
            "div.niO4u.VDgVie.SlP8xc, "
            "input[type='submit'][value*='squeda'], "
            "button[type='submit']"
        )
        if not search_button:
            logger.error("Botón de búsqueda avanzada no encontrado")
            return False

        try:
            await search_button.click()
            await page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.error("Timeout esperando resultados de búsqueda")
            return False

        await _random_sleep(*PAGE_LOAD_PAUSE_MS)

        if not await self._check_and_handle_auth(page):
            return False

        await self._click_reset_tools_if_present(page)
        return True

    # ------------------------------------------------------------------
    # Extracción por página
    # ------------------------------------------------------------------
    async def _collect_urls_from_page(self, page: Page, target_domain: str) -> list[str]:
        """Extrae hrefs de la página actual filtrados por dominio."""
        anchors: list[str] = await page.eval_on_selector_all(
            "a[href]",
            "elements => elements.map(el => el.href)",
        )
        return _extract_domain_urls(anchors, target_domain)

    async def _go_to_next_page(self, page: Page) -> bool:
        """
        Navega a la siguiente página de resultados.

        Returns:
            True si navegó exitosamente, False si no hay más páginas.
        """
        try:
            next_button = await page.query_selector(
                "#pnnext, a[aria-label='Página siguiente']"
            )
            if not next_button:
                logger.debug("No hay página siguiente")
                return False
            await _move_mouse_randomly(page)
            await next_button.click()
            await page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            await _random_sleep(*PAGE_LOAD_PAUSE_MS)
            return True
        except PlaywrightTimeoutError:
            logger.warning("Timeout navegando a la página siguiente")
            return False

    async def _update_keyword(self, keyword):
        try:
            db_manager = SQLiteManager("url_scraper.db")
            await db_manager.connect()

            keyword_repo = KeywordRepository(db_manager)
            await keyword_repo.mark_scraped(keyword)
        finally: 
            await db_manager.disconnect()
    # ------------------------------------------------------------------
    # Recolección por tab
    # ------------------------------------------------------------------
    async def _collect_paginated_tab(
        self,
        page: Page,
        target_domain: str,
        keyword: str,
    ) -> int:
        """
        Recorre hasta MAX_RESULT_PAGES páginas, persistiendo al terminar cada una.

        Returns:
            Total de URLs únicas persistidas en este tab.
        """
        total_persisted = 0
        seen_in_tab: set[str] = set()  # dedup cross-página dentro del mismo tab

        for page_num in range(1, MAX_RESULT_PAGES + 1):
            logger.info("  Página %d/%d", page_num, MAX_RESULT_PAGES)

            if not await self._check_and_handle_auth(page):
                logger.warning("  Auth fallida en página %d — abortando paginación", page_num)
                break

            await self._click_reset_tools_if_present(page)

            try:
                page_urls = await self._collect_urls_from_page(page, target_domain)
            except Exception as exc:
                logger.error("  Error extrayendo URLs en página %d: %s", page_num, exc)
                page_urls = []

            # Filtrar URLs ya vistas en este tab
            new_urls = [u for u in page_urls if u not in seen_in_tab]
            seen_in_tab.update(new_urls)

            if new_urls:
                posts = _build_posts(new_urls, keyword)
                try:
                    await _save_results(posts)
                    await self._update_keyword(keyword)
                    total_persisted += len(new_urls)
                except Exception as exc:
                    logger.error("  Error persistiendo página %d: %s", page_num, exc)
            else:
                logger.debug("  Página %d sin URLs nuevas", page_num)

            logger.info(
                "  Página %d: %d URLs nuevas | acumulado tab: %d",
                page_num, len(new_urls), total_persisted,
            )

            if page_num < MAX_RESULT_PAGES:
                has_next = await self._go_to_next_page(page)
                if not has_next:
                    logger.info("  No hay más páginas")
                    break

            await _random_sleep(*PAGE_LOAD_PAUSE_MS)

        return total_persisted

    async def _collect_scroll_tab(
        self,
        page: Page,
        target_domain: str,
        keyword: str,
    ) -> int:
        """
        Scroll hasta el final, extrae URLs y persiste en un único batch.

        Returns:
            Total de URLs persistidas.
        """
        try:
            await _scroll_to_bottom(page)
            urls = await self._collect_urls_from_page(page, target_domain)
        except Exception as exc:
            logger.error("  Error durante scroll/extracción: %s", exc)
            return 0

        if not urls:
            return 0

        posts = _build_posts(urls, keyword)
        try:
            await _save_results(posts)
            await self._update_keyword(keyword)
        except Exception as exc:
            logger.error("  Error persistiendo tab scroll: %s", exc)
            return 0

        return len(urls)

    async def _click_tab(self, page: Page, tab_name: TabName) -> bool:
        """
        Click físico en el tab usando texto parcial sin tildes problemáticas.

        Returns:
            True si el tab fue activado correctamente.
        """
        partial_text = _TAB_PARTIAL_TEXT.get(tab_name, tab_name.value)
        try:
            tab_element = page.locator(
                "div[role='listitem'] span.R1QWuf",
                has_text=partial_text,
            ).first
            await tab_element.wait_for(state="visible", timeout=5_000)
            await tab_element.scroll_into_view_if_needed()
            await _random_sleep(200, 400)
            await tab_element.click()
            await page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            await _random_sleep(*PAGE_LOAD_PAUSE_MS)
            logger.info("Tab '%s' activado", tab_name)
            return True
        except PlaywrightTimeoutError:
            logger.warning("Timeout activando tab '%s' — saltando", tab_name)
            return False
        except Exception as exc:
            logger.error("Error haciendo click en tab '%s': %s", tab_name, exc)
            return False

    # ------------------------------------------------------------------
    # Orquestador de tabs
    # ------------------------------------------------------------------
    async def _scrape_all_tabs(
        self,
        page: Page,
        target_domain: str,
        keyword: str,
    ) -> None:
        """
        Itera sobre todos los tabs recolectando y persistiendo URLs.
        Captura excepciones por tab para garantizar continuidad.
        """
        # Tab "Todo" — activo por defecto al llegar de la búsqueda
        logger.info("[Tab: %s]", TabName.TODO)
        try:
            total = await self._collect_paginated_tab(page, target_domain, keyword)
            logger.info("[Tab: %s] Total persistido: %d URLs", TabName.TODO, total)
        except Exception as exc:
            logger.error("[Tab: %s] Error inesperado: %s", TabName.TODO, exc)

        for tab in _TAB_SEQUENCE:
            logger.info("[Tab: %s]", tab)
            try:
                activated = await self._click_tab(page, tab)
                if not activated:
                    logger.warning("[Tab: %s] No disponible — saltando", tab)
                    continue

                if not await self._check_and_handle_auth(page):
                    logger.warning("[Tab: %s] Auth fallida — saltando", tab)
                    continue

                if tab.value in SCROLL_ONLY_TABS:
                    total = await self._collect_scroll_tab(page, target_domain, keyword)
                else:
                    total = await self._collect_paginated_tab(page, target_domain, keyword)

                logger.info("[Tab: %s] Total persistido: %d URLs", tab, total)

            except Exception as exc:
                logger.error("[Tab: %s] Error inesperado — continuando con siguiente tab: %s", tab, exc)

            await _random_sleep(*PAGE_LOAD_PAUSE_MS)

    # ------------------------------------------------------------------
    # Punto de entrada
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """
        Orquesta el ciclo completo: keywords → sitios → tabs → persistencia.

        Flujo:
            1. Carga keywords desde la DB.
            2. Por cada keyword×sitio: crea contexto limpio, busca, extrae.
            3. Errores críticos por combinación renuevan el contexto y continúan.
            4. El browser permanece vivo durante todo el proceso.
        """
        try:
            db_manager = SQLiteManager("url_scraper.db")
            await db_manager.connect()

            keyword_repo = KeywordRepository(db_manager)
            keywords: list[str] = await keyword_repo.get_google_search_keywords()
        finally: 
            await db_manager.disconnect()

        #random.shuffle(keywords)
        total_keywords = len(keywords)
        logger.info("Iniciando scraping — %d keywords × %d sitios", total_keywords, len(TARGET_SITES))

        for keyword_idx, keyword in enumerate(keywords, start=1):
            for site in TARGET_SITES:
                logger.info(
                    "=== [%d/%d] Keyword: '%s' | Sitio: %s ===",
                    keyword_idx, total_keywords, keyword, site,
                )

                try:
                    page = await self._new_context()
                    search_ok = await self._perform_advanced_search(page, keyword, site)

                    if not search_ok:
                        logger.error(
                            "Búsqueda fallida para '%s' en %s — renovando contexto y continuando",
                            keyword, site,
                        )
                        continue  # _new_context() en la siguiente iteración limpia el contexto

                    await self._scrape_all_tabs(page, site, keyword)

                except PlaywrightTimeoutError as exc:
                    logger.error(
                        "Timeout crítico en '%s' | %s: %s — renovando contexto",
                        keyword, site, exc,
                    )
                except Exception as exc:
                    logger.exception(
                        "Error inesperado en '%s' | %s: %s — renovando contexto",
                        keyword, site, exc,
                    )
                # El contexto se cierra en la próxima llamada a _new_context()
                # o en __aexit__ si es la última iteración.

                inter_search_delay = random.uniform(4.0, 9.0)
                logger.info("Pausa entre búsquedas: %.1fs", inter_search_delay)
                await asyncio.sleep(inter_search_delay)

        logger.info("✅ Servicio de scraping completado")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def run_scraping_service() -> None:
    """Wrapper de compatibilidad para el entry point original."""
    async with GoogleScrapingService() as service:
        await service.run()


if __name__ == "__main__":
    asyncio.run(run_scraping_service())