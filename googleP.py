"""
Google Advanced Search scraper using Playwright async.

Busca keywords en facebook.com e instagram.com usando Google Advanced Search,
recopila URLs por tabs (Todo, Noticias, Imágenes, Videos, Videos cortos, Web)
y persiste los resultados.
"""

import asyncio
import logging
import random
import re
import httpx
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Awaitable
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from playwright.async_api import (
    Page, async_playwright, BrowserContext,
    Locator, TimeoutError as PlaywrightTimeout
)
from typing import Optional

from database import SQLiteManager, PostRepository, KeywordRepository, PostCreate
from config.settings import settings

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("google_scraper")

_DATA_STORE_HEADERS: dict[str, str] = {
    "Authorization": f"Bearer {settings.DATA_STORE_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
ADVANCED_SEARCH_URL = "https://www.google.com/advanced_search"
MAX_RESULT_PAGES = 2
AUTH_WAIT_TIMEOUT_MS = 120_000          # 2 min para resolver CAPTCHA manual
NAVIGATION_TIMEOUT_MS = 30_000
SCROLL_PAUSE_MS = (800, 1_400)          # rango aleatorio entre scrolls
PAGE_LOAD_PAUSE_MS = (1_500, 3_000)
TYPING_DELAY_MS = (60, 130)             # delay por caracter

TARGET_SITES = ["facebook.com", "instagram.com"]

# Tabs con scroll infinito (sin paginación)
SCROLL_ONLY_TABS = {"Imágenes", "Videos", "Videos cortos"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------
class TabName(StrEnum):
    TODO          = "Todo"
    NOTICIAS      = "Noticias"
    IMAGENES      = "Im\u00e1genes"    # Imágenes
    VIDEOS        = "V\u00eddeos"      # Vídeos  (í = \u00ed)
    VIDEOS_CORTOS = "V\u00eddeos cortos"
    WEB           = "Web"


# ---------------------------------------------------------------------------
# Helpers anti-scraping
# ---------------------------------------------------------------------------
async def _random_sleep(min_ms: int, max_ms: int) -> None:
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def _human_type(page: Page, selector: str, text: str) -> None:
    """Simula escritura humana con delays variables."""
    await page.click(selector)
    await page.fill(selector, "")          # limpiar antes
    for char in text:
        await page.type(selector, char, delay=random.randint(*TYPING_DELAY_MS))
        if random.random() < 0.05:         # 5% de micro-pausa extra
            await _random_sleep(200, 500)


async def _move_mouse_randomly(page: Page) -> None:
    """Mueve el mouse a posición aleatoria para simular comportamiento humano."""
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    x = random.randint(100, viewport["width"] - 100)
    y = random.randint(100, viewport["height"] - 100)
    await page.mouse.move(x, y)


async def _scroll_to_bottom(page: Page) -> None:
    """Scroll progresivo hasta el final de la página."""
    last_height = await page.evaluate("document.body.scrollHeight")
    while True:
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await _random_sleep(*SCROLL_PAUSE_MS)
        await _move_mouse_randomly(page)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    logger.debug("Scroll hasta el final completado")


# ---------------------------------------------------------------------------
# Extracción de URLs
# ---------------------------------------------------------------------------
def _extract_domain_urls(raw_urls: list[str], target_domain: str) -> list[str]:
    """
    Filtra y normaliza URLs que pertenecen al dominio objetivo.

    Args:
        raw_urls: Lista de URLs crudas extraídas del DOM.
        target_domain: Dominio a filtrar (ej. 'facebook.com').

    Returns:
        Lista de URLs únicas del dominio objetivo.
    """
    filtered: list[str] = []
    for url in raw_urls:
        try:
            parsed = urlparse(url)
            # Google redirige por /url?q=... — extraer la URL real
            if parsed.path == "/url" and "q=" in (parsed.query or ""):
                import urllib.parse
                qs = urllib.parse.parse_qs(parsed.query)
                real_urls = qs.get("q", [])
                url = real_urls[0] if real_urls else url
                parsed = urlparse(url)

            hostname = parsed.netloc.lower().lstrip("www.")
            if target_domain in hostname and parsed.scheme in ("http", "https"):
                filtered.append(url)
        except Exception:
            continue
    return list(dict.fromkeys(filtered))  # dedup preservando orden


async def _collect_urls_from_page(page: Page, target_domain: str) -> list[str]:
    """Extrae todos los hrefs de la página actual filtrados por dominio."""
    anchors = await page.eval_on_selector_all(
        "a[href]",
        "elements => elements.map(el => el.href)"
    )
    return _extract_domain_urls(anchors, target_domain)


# ---------------------------------------------------------------------------
# Detección y manejo de bloqueos
# ---------------------------------------------------------------------------
async def _check_and_handle_auth(page: Page) -> bool:
    """
    Detecta CAPTCHA / login de Google y espera resolución manual.

    Returns:
        True si se resolvió (o no había bloqueo), False si timeout.
    """
    captcha_selectors = [
        "form#captcha-form",
        "div#recaptcha",
        "iframe[src*='recaptcha']",
        "div.g-recaptcha",
    ]
    for selector in captcha_selectors:
        if await page.query_selector(selector):
            logger.warning(
                "⚠️  CAPTCHA detectado. Tienes 2 minutos para resolverlo manualmente..."
            )
            try:
                await page.wait_for_function(
                    "!document.querySelector('form#captcha-form') && "
                    "!document.querySelector('div#recaptcha')",
                    timeout=AUTH_WAIT_TIMEOUT_MS,
                )
                logger.info("✅ CAPTCHA resuelto, continuando...")
                return True
            except PlaywrightTimeoutError:
                logger.error("❌ Timeout esperando resolución de CAPTCHA")
                return False
    return True


async def _click_reset_tools_if_present(page: Page) -> None:
    """Hace click en 'Restablecer las herramientas de búsqueda' si aparece."""
    try:
        reset_selectors = [
            "a[href*='tbas=0']",
            "a:has-text('Restablecer las herramientas de búsqueda')",
            "text=Restablecer las herramientas de búsqueda",
        ]
        reset_link = await page.query_selector("a[href*='tbas=0']")

        reset_link: Optional[Locator] = None
        for selector in reset_selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=2_000)
                reset_link = locator
                logger.warning(
                    f"⚠️ Banner 'Restablecer herramientas' detectado ({selector})"
                )
                break
            except PlaywrightTimeout:
                continue

        if reset_link:
            logger.info("Haciendo click en 'Restablecer herramientas de búsqueda'")
            await reset_link.click()
            await _random_sleep(*PAGE_LOAD_PAUSE_MS)
    except Exception as exc:
        logger.debug(f"Reset tools no encontrado o error: {exc}")


# ---------------------------------------------------------------------------
# Paginación
# ---------------------------------------------------------------------------
async def _go_to_next_page(page: Page) -> bool:
    """
    Navega a la siguiente página de resultados.

    Returns:
        True si navegó exitosamente, False si no hay más páginas.
    """
    try:
        next_button = await page.query_selector("#pnnext, a[aria-label='Página siguiente']")
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

async def _select_date_filter_24h(page: Page) -> None:
    """
    Selecciona 'Últimas 24 horas' usando data-v='d' directo sobre el listbox,
    que ya está en el DOM aunque no sea visible (Google lo renderiza oculto).
    """
    try:
        label = page.locator("label.mNAued", has_text="ltima actualizaci")  # parcial, evita tilde
        await label.wait_for(timeout=5_000)

        container = label.locator("xpath=following-sibling::div[1]")

        # Click en combobox para desplegar
        combobox = container.locator("div[role='combobox']")
        await combobox.scroll_into_view_if_needed()
        await _random_sleep(200, 400)
        await combobox.click()
        await _random_sleep(300, 500)

        # Opción por data-v="d" — sin depender de texto con tildes
        option = container.locator("div[role='option'][data-v='d']")
        await option.wait_for(state="visible", timeout=3_000)
        await option.click()
        await _random_sleep(300, 500)

        # Verificar que el hidden input se actualizó
        qdr_value = await page.input_value("input[name='as_qdr']")
        logger.debug(f"Filtro de fecha aplicado: as_qdr='{qdr_value}'")

    except PlaywrightTimeoutError:
        # Fallback: forzar el valor directamente en el hidden input + span visible
        logger.warning("Fallback: seteando as_qdr='d' via JS")
        await page.evaluate("""
            const input = document.querySelector("input[name='as_qdr']");
            if (input) {
                input.value = 'd';
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }
        """)
    except Exception as exc:
        logger.warning(f"Error seleccionando filtro de fecha: {exc}")

# ---------------------------------------------------------------------------
# Búsqueda avanzada
# ---------------------------------------------------------------------------
async def _perform_advanced_search(
    page: Page,
    keyword: str,
    site: str,
) -> bool:
    """
    Rellena y envía el formulario de Google Advanced Search.

    Args:
        page: Página de Playwright activa.
        keyword: Término exacto a buscar (as_epq).
        site: Dominio a filtrar (as_sitesearch).

    Returns:
        True si la búsqueda se realizó correctamente.
    """
    logger.info(f"Abriendo búsqueda avanzada: '{keyword}' en {site}")

    await page.goto(ADVANCED_SEARCH_URL, timeout=NAVIGATION_TIMEOUT_MS)
    await _random_sleep(*PAGE_LOAD_PAUSE_MS)

    if not await _check_and_handle_auth(page):
        return False

    # Escribir keyword y sitio como humano
    await _human_type(page, "input[name='as_epq']", keyword)
    await _random_sleep(400, 800)
    await _human_type(page, "input[name='as_sitesearch']", site)
    await _random_sleep(600, 1_000)

    # Seleccionar "Últimas 24 horas" en el combobox de fecha
    try:
        await _select_date_filter_24h(page)
    except Exception as exc:
        logger.warning(f"No se pudo seleccionar filtro de fecha: {exc}")

    await _move_mouse_randomly(page)
    await _random_sleep(400, 700)

    # Click en botón "Búsqueda avanzada"
    search_button = await page.query_selector(
        "div.niO4u.VDgVie.SlP8xc, "
        "input[type='submit'][value*='squeda'], "
        "button[type='submit']"
    )
    if not search_button:
        logger.error("Botón de búsqueda avanzada no encontrado")
        return False

    await search_button.click()
    await page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
    await _random_sleep(*PAGE_LOAD_PAUSE_MS)

    if not await _check_and_handle_auth(page):
        return False

    await _click_reset_tools_if_present(page)
    return True


# ---------------------------------------------------------------------------
# Recolección por tabs
# ---------------------------------------------------------------------------
async def _collect_paginated_tab(
    page: Page,
    target_domain: str,
) -> list[str]:
    """Recorre hasta MAX_RESULT_PAGES páginas recogiendo URLs."""
    collected: list[str] = []
    for page_num in range(1, MAX_RESULT_PAGES + 1):
        logger.info(f"  Página {page_num}/{MAX_RESULT_PAGES}")
        await _check_and_handle_auth(page)
        await _click_reset_tools_if_present(page)

        page_urls = await _collect_urls_from_page(page, target_domain)
        collected.extend(page_urls)
        logger.debug(f"  URLs encontradas en página {page_num}: {len(page_urls)}")

        if page_num < MAX_RESULT_PAGES:
            has_next = await _go_to_next_page(page)
            if not has_next:
                logger.info("  No hay más páginas, terminando paginación")
                break

        await _random_sleep(*PAGE_LOAD_PAUSE_MS)

    return list(dict.fromkeys(collected))


async def _collect_scroll_tab(page: Page, target_domain: str) -> list[str]:
    """Scroll hasta el final y recoge URLs (tabs sin paginación)."""
    await _scroll_to_bottom(page)
    return await _collect_urls_from_page(page, target_domain)


async def _click_tab(page: Page, tab_name: str) -> bool:
    """
    Click físico en el tab usando el texto Unicode exacto.
    Usa locator con has_text parcial para evitar problemas de encoding.
    """
    # Mapa de tab → fragmento sin tilde para el selector parcial
    SAFE_PARTIAL: dict[str, str] = {
        TabName.TODO:          "Todo",
        TabName.NOTICIAS:      "Noticias",
        TabName.IMAGENES:      "m\u00e1genes",   # 'ágenes' — evita la Í mayúscula
        TabName.VIDEOS:        "deos",            # 'deos' — único para Vídeos
        TabName.VIDEOS_CORTOS: "deos cortos",     # distingue de Vídeos solo
        TabName.WEB:           "Web",
    }

    partial_text = SAFE_PARTIAL.get(tab_name, tab_name)

    try:
        # Los tabs están en div[role='listitem'] > a > div > span.R1QWuf
        tab_element = page.locator(
            f"div[role='listitem'] span.R1QWuf",
            has_text=partial_text,
        ).first

        await tab_element.wait_for(state="visible", timeout=5_000)
        await tab_element.scroll_into_view_if_needed()
        await _random_sleep(200, 400)
        await tab_element.click()
        await page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
        await _random_sleep(*PAGE_LOAD_PAUSE_MS)
        logger.info(f"Tab '{tab_name}' activado")
        return True

    except PlaywrightTimeoutError:
        logger.warning(f"Timeout activando tab '{tab_name}'")
        return False
    except Exception as exc:
        logger.error(f"Error haciendo click en tab '{tab_name}': {exc}")
        return False


def _build_posts(urls: list[str], keyword: str) -> list[PostCreate]:
    """
    Convierte una lista de URLs en objetos PostCreate.

    Args:
        urls:    Lista de URLs ya filtradas por dominio.
        keyword: Keyword que originó estos resultados.

    Returns:
        Lista de PostCreate con platform inferida de la URL.
    """
    posts: list[PostCreate] = []
    for url in urls:
        platform = "facebook" if "facebook" in url else "instagram"
        posts.append(PostCreate(keyword=keyword, url=url, platform=platform))
    return posts


async def _scrape_all_tabs(
    page: Page,
    target_domain: str,
    keyword: str,
) -> dict[TabName, list[str]]:
    """
    Itera sobre todos los tabs recolectando URLs según la estrategia de cada uno.

    Args:
        page: Página tras ejecutar la búsqueda avanzada.
        target_domain: Dominio objetivo a filtrar.

    Returns:
        Diccionario {tab -> lista de URLs}.
    """


    # Tab "Todo" es el inicial (ya estamos en él)
    logger.info(f"[Tab: {TabName.TODO}]")
    todo_urls = await _collect_paginated_tab(page, target_domain)
    await _save_results(_build_posts(todo_urls, keyword))

    # Tabs restantes en orden
    tab_sequence = [
        TabName.NOTICIAS,
        TabName.IMAGENES,
        TabName.VIDEOS,
        TabName.VIDEOS_CORTOS,
        TabName.WEB,
    ]

    for tab in tab_sequence:
        logger.info(f"[Tab: {tab}]")
        success = await _click_tab(page, tab.value)
        if not success:
            logger.warning(f"Saltando tab '{tab}' — no disponible")
            todo_urls = []
            continue

        await _check_and_handle_auth(page)

        if tab in SCROLL_ONLY_TABS:
            todo_urls = await _collect_scroll_tab(page, target_domain)
        else:
            todo_urls = await _collect_paginated_tab(page, target_domain)


        logger.info("  [%s] %d URLs → persistiendo...", tab, len(todo_urls))
        await _save_results(_build_posts(todo_urls, keyword))

        logger.info(f"  Total URLs en '{tab}': {len(todo_urls)}")
        await _random_sleep(*PAGE_LOAD_PAUSE_MS)



async def _save_to_sqlite(posts: list[PostCreate]) -> tuple[int, int]:
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
    
async def _send_to_api(posts:list[PostCreate]) -> tuple[int, int]:
    """
    Envía las URLs del buffer al endpoint HTTP externo.

    Usa conexión directa (no proxy). ``verify`` controlado por
    ``DATA_STORE_VERIFY_SSL`` en settings para CAs privadas.

    Returns:
        Tupla ``(sent_ok, failed)``.
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
    
# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
async def _save_results(results: list[PostCreate]) -> None:
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
        inserted, skipped = await _save_to_sqlite(results)
        logger.info(
            "[SQLite] %d insertados, %d omitidos",
            inserted, skipped
        )
    elif mode == "api":
        sent_ok, failed = await _send_to_api(results)
        logger.info(
            "[API] %d enviadas, %d fallidas",
            sent_ok, failed
        )
    else:
        logger.warning(
            "OUTPUT_MODE='%s' no reconocido (válidos: 'sqlite', 'api'). "
            "Resultados NO persistidos.",
            settings.OUTPUT_MODE,
        )


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------
async def run_scraping_service() -> None:
    """
    Punto de entrada principal del servicio de scraping.

    Flujo:
        1. Obtiene keywords.
        2. Por cada keyword × sitio abre una sesión de búsqueda.
        3. Recopila URLs por tab.
        4. Persiste resultados.
    """
    
    db_manager = SQLiteManager("url_scraper.db")
    await db_manager.connect()
    keyword_repo = KeywordRepository(db_manager)
    keywords: list[str] = await keyword_repo.get_google_search_keywords()

    random.shuffle(keywords)

    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        for keyword in keywords:
            for site in TARGET_SITES:
                logger.info(f"=== Keyword: '{keyword}' | Sitio: {site} ===")

                context: BrowserContext = await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    locale="es-ES",
                    timezone_id="Europe/Madrid",
                    extra_http_headers={
                        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )

                # Inyectar script anti-detección
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    window.chrome = { runtime: {} };
                """)

                page: Page = await context.new_page()

                try:
                    search_ok = await _perform_advanced_search(page, keyword, site)
                    if not search_ok:
                        logger.error(
                            f"Búsqueda fallida para '{keyword}' en {site}, saltando..."
                        )
                        continue

                    await _scrape_all_tabs(page, site, keyword)

                except Exception as exc:
                    logger.exception(
                        "Error inesperado procesando '%s' en %s: %s",
                        keyword, site, exc,
                    )
                finally:
                    await context.close()

                # Pausa variable entre búsquedas — clave anti-detección
                inter_search_delay = random.uniform(4.0, 9.0)
                logger.info("Pausa entre búsquedas: %.1fs", inter_search_delay)
                await asyncio.sleep(inter_search_delay)

        await browser.close()

    logger.info("✅ Servicio de scraping completado")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(run_scraping_service())
