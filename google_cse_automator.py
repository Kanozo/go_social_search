"""
google_cse_automator.py
Motor de scraping sobre Google Custom Search Engine (CSE) con Camoufox.

CORRECCIONES PAGINADO IMÁGENES (v2.6) – CORRECCIÓN DEFINITIVA
──────────────────────────────────────────────────────────────
  - _navigate_to_page ahora usa el paginador del tab activo (.gsc-tabdActive .gsc-cursor)
    en lugar del primer .gsc-cursor del DOM, que pertenece al tab inactivo.
  - Se elimina ambigüedad entre tabs Web e Image.
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

_FACEBOOK_URL_PATTERNS: tuple[str, ...] = (
    "facebook.com",
    "fb.com",
    "fb.watch",
    "instagram.com",
)

# ─────────────────────────────────────────────────────────────────────────────
# Tipos de datos
# ─────────────────────────────────────────────────────────────────────────────
class ScrapedResult(TypedDict):
    url:              str
    platform:         str
    published_at:     datetime | None
    published_at_raw: str | None

# ─────────────────────────────────────────────────────────────────────────────
# BrowserConfig
# ─────────────────────────────────────────────────────────────────────────────
class BrowserConfig:
    page_load_wait_range:    tuple[float, float] = (1.0, 2.5)
    scroll_pause_range:      tuple[float, float] = (0.1, 0.3)
    between_pages_range:     tuple[float, float] = (1.5, 3.0)
    between_keywords_range:  tuple[float, float] = (3.0, 8.0)
    warmup_pause_range:      tuple[float, float] = (0.5, 1.5)
    typing_wpm_range:        tuple[int, int]     = (80, 130)
    warmup_url:              str                 = "https://www.google.com"
    captcha_max_wait_seconds: float              = 300.0
    captcha_wait_for_human:  bool                = True
    distraction_probability: float               = 0.05
    focus_blur_probability:  float               = 0.10
    max_pagination_retries:  int                 = 2

    def jitter_wait(self, low: float, high: float) -> float:
        mid = (low + high) / 2.0
        sigma = (high - low) / 6.0
        base = random.gauss(mid, sigma)
        extra = random.expovariate(5.0)
        return max(low, min(high * 1.5, base + extra * 0.2))

# ─────────────────────────────────────────────────────────────────────────────
# GoogleCSEAutomator
# ─────────────────────────────────────────────────────────────────────────────
class GoogleCSEAutomator:
    def __init__(
        self,
        cse_id: str,
        platform: str = "",
        url_repo: SupabaseUrlRepo | None = None,
        config: BrowserConfig | None = None,
    ) -> None:
        self._search_url = f"https://cse.google.com/cse?cx={cse_id}"
        self._platform = platform
        self._url_repo = url_repo
        self.cfg = config or BrowserConfig()
        self._scraped_results: list[ScrapedResult] = []
        self._session_store = SessionStore(settings.SESSION_DIR)

    # ── Helpers ─────────────────────────────────────────────────────────────
    async def _human_sleep(self, low: float, high: float) -> None:
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    async def _inject_referrer(self, page: Page) -> None:
        referrer = random.choice([
            "https://www.google.com/search?q=site:facebook.com",
            "https://www.google.com/search?q=site:instagram.com",
            "https://www.google.com/",
        ])
        try:
            await page.evaluate(f"""() => {{
                Object.defineProperty(document, 'referrer', {{
                    get: () => {json.dumps(referrer)},
                    configurable: true
                }});
            }}""")
        except Exception:
            pass
        await page.set_extra_http_headers({"Referer": referrer})
        await asyncio.sleep(random.uniform(0.2, 0.5))
        logger.debug("Referrer inyectado: %s", referrer)

    async def _quick_move_and_click(self, page: Page, locator: Any) -> None:
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click(timeout=2_000)
                return
            target_x = box["x"] + box["width"]  * random.uniform(0.2, 0.5)
            target_y = box["y"] + box["height"] * random.uniform(0.2, 0.5)
            steps = 3
            for step in range(steps + 1):
                t = step / steps
                x = box["x"] + (target_x - box["x"]) * t
                y = box["y"] + (target_y - box["y"]) * t
                await page.mouse.move(x, y)
                await asyncio.sleep(0.001)
            await page.mouse.click(target_x, target_y)
        except Exception:
            await locator.click(timeout=2_000)

    @staticmethod
    async def apply_stealth(page: Page, fingerprint: BrowserFingerprint) -> None:
        await page.add_init_script(fingerprint.stealth_js)
        logger.debug("Stealth JS aplicado (%d bytes).", len(fingerprint.stealth_js))

    async def _warmup_session(self, page: Page, is_fresh_session: bool = True) -> None:
        if not is_fresh_session:
            logger.debug("Warmup omitido.")
            return
        await page.goto(self.cfg.warmup_url, wait_until="domcontentloaded", timeout=10_000)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        logger.debug("Warmup completado.")

    @staticmethod
    def _parse_relative_timestamp(snippet_text: str) -> datetime | None:
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
            logger.warning("Error timestamp: %s", exc)
            return None

    async def _incremental_scroll(self, page: Page) -> None:
        try:
            page_height = await page.evaluate("document.body.scrollHeight")
            viewport_h = await page.evaluate("window.innerHeight") or 768
            segments = max(2, min(3, int(page_height / viewport_h)))
            step_px = int(page_height / segments)
            for _ in range(segments):
                await human_scroll(page, direction="down", amount=step_px)
                await asyncio.sleep(random.uniform(0.15, 0.35))
        except Exception as e:
            logger.warning("Error en scroll incremental: %s", e)

    async def _unblock_images(self, context: BrowserContext) -> None:
        try:
            await context.unroute_all()
            logger.debug("Imágenes desbloqueadas.")
        except Exception as exc:
            logger.debug("Error al desbloquear: %s", exc)

    async def _reblock_images(self, context: BrowserContext, url_pattern: str = "https://encrypted-tbn0.gstatic.com/images") -> None:
        await self.block_images_async(context, url_pattern=url_pattern, log_blocked=False)
        logger.debug("Imágenes bloqueadas de nuevo.")

    async def block_images_async(self, context: BrowserContext, url_pattern: str | None = None, log_blocked: bool = False) -> None:
        async def handle_route(route: Route) -> None:
            try:
                if route.request.resource_type == "image":
                    if url_pattern is None or url_pattern in route.request.url:
                        if log_blocked:
                            logger.debug("Bloqueada: %s", route.request.url)
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
        logger.debug("Interceptor de imágenes activo.")

    # ── Extracción de resultados web ────────────────────────────────────────
    async def _extract_page_results(self, page: Page, keyword: str) -> None:
        await self._incremental_scroll(page)
        await CaptchaDetector.check(page, keyword)
        try:
            await page.wait_for_selector(".gsc-webResult", timeout=10_000)
        except PlaywrightTimeoutError:
            await CaptchaDetector.check(page, keyword)
            logger.info("Sin resultados para '%s'.", keyword)
            return
        results_container = page.locator(".gsc-expansionArea .gsc-webResult")
        count = await results_container.count()
        logger.debug("Parseando %d bloques para '%s'.", count, keyword)
        for i in range(count):
            try:
                container = results_container.nth(i)
                link = container.locator(".gs-title a.gs-title").first
                href = (await link.get_attribute("data-ctorig") or await link.get_attribute("href"))
                if not href or "google.com" in href:
                    continue
                snippet_text = (await container.locator(".gs-snippet").first.text_content() or "").strip()
                ts_match = _RELATIVE_TS_PATTERN.match(snippet_text)
                url_clean = clean_url(href)
                platform = self._platform or ("instagram" if "instagram" in url_clean else "facebook")
                self._scraped_results.append(ScrapedResult(
                    url=url_clean,
                    platform=platform,
                    published_at=self._parse_relative_timestamp(snippet_text) if ts_match else None,
                    published_at_raw=ts_match.group(1) if ts_match else None,
                ))
            except Exception as exc:
                logger.error("Error en resultado #%d: %s", i, exc)

    # ── Persistencia ────────────────────────────────────────────────────────
    async def _save_to_supabase(self, keyword: str) -> tuple[int, int]:
        if not self._url_repo:
            return 0, 0
        urls_to_insert = [
            {"url": item["url"], "keyword": keyword, "platform": item["platform"], "send_tg": False}
            for item in self._scraped_results if item.get("url")
        ]
        if not urls_to_insert:
            return 0, 0
        return await self._url_repo.bulk_insert_urls(urls_to_insert)

    async def _send_to_api(self) -> tuple[int, int]:
        valid = [(item["url"], item["platform"]) for item in self._scraped_results if item.get("url")]
        if not valid:
            return 0, 0
        sent_ok = failed = 0
        async with httpx.AsyncClient(headers=_DATA_STORE_HEADERS, timeout=10.0, verify=settings.DATA_STORE_VERIFY_SSL) as client:
            for post_url, platform in valid:
                endpoint = f"{settings.DATA_STORE_BASE_URL}/{platform}/urls"
                try:
                    resp = await client.post(endpoint, json={"post_url": post_url})
                    if resp.is_success:
                        sent_ok += 1
                    else:
                        failed += 1
                        logger.warning("API rechazó URL (HTTP %d): %s", resp.status_code, post_url[:80])
                except httpx.RequestError as exc:
                    failed += 1
                    logger.error("Error de red: %s", exc)
        return sent_ok, failed

    async def _persist_results(self, keyword: str) -> None:
        mode = settings.OUTPUT_MODE.strip().lower()
        if mode == "supabase":
            inserted, skipped = await self._save_to_supabase(keyword)
            logger.info("[Supabase] %d insertadas, %d omitidas | '%s'", inserted, skipped, keyword)
        elif mode == "api":
            sent_ok, failed = await self._send_to_api()
            logger.info("[API] %d enviadas, %d fallidas | '%s'", sent_ok, failed, keyword)
        else:
            logger.warning("OUTPUT_MODE no reconocido. No se persistió.")

    # ── Filtro de fecha ─────────────────────────────────────────────────────
    async def _apply_date_filter(self, page: Page) -> None:
        try:
            dropdown = page.locator(".gsc-selected-option-container").first
            await self._quick_move_and_click(page, dropdown)
            date_option = page.locator(".gsc-option-menu-item", has_text="Date")
            await date_option.wait_for(state="visible", timeout=5_000)
            box = await date_option.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                await page.mouse.move(target_x, target_y)
                await asyncio.sleep(random.uniform(0.05, 0.12))
                await page.mouse.click(target_x, target_y)
            else:
                await date_option.click()
            try:
                await page.wait_for_selector(".gsc-webResult", timeout=8_000)
                logger.debug("Filtro Date aplicado.")
            except PlaywrightTimeoutError:
                logger.warning("Timeout recarga tras filtro Date.")
            await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception as exc:
            logger.warning("Error en filtro de fecha: %s", exc)

    # ── Navegación unificada (CORREGIDA: usa el paginador del tab activo) ───
    async def _navigate_to_page(self, page: Page, target: int) -> bool:
        target_str = str(target)
        retries = self.cfg.max_pagination_retries
        for attempt in range(1, retries + 1):
            logger.debug("Navegando a página %s (intento %d/%d)", target_str, attempt, retries)
            # Seleccionar SOLO el paginador del tab actualmente activo
            paginator = page.locator(".gsc-tabdActive .gsc-cursor")
            try:
                await paginator.wait_for(state="visible", timeout=5_000)
            except PlaywrightTimeoutError:
                logger.debug("Paginador activo no visible.")
                await asyncio.sleep(0.5)
                continue
            # Botón destino dentro de ese paginador
            page_btn = paginator.locator(f'[aria-label="Page {target_str}"]')
            try:
                await page_btn.scroll_into_view_if_needed()
                await page_btn.wait_for(state="visible", timeout=3_000)
                await page_btn.click(timeout=3_000)
                logger.debug("Clic en página %s exitoso.", target_str)
            except PlaywrightTimeoutError:
                logger.debug("Botón 'Page %s' no encontrado.", target_str)
                return False
            except Exception as exc:
                logger.warning("Error al clic en página %s: %s", target_str, exc)
                continue
            # Confirmar cambio
            try:
                await paginator.locator(
                    f".gsc-cursor-current-page:has-text('{target_str}')"
                ).wait_for(state="visible", timeout=10_000)
                logger.debug("Página %s confirmada.", target_str)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                return True
            except PlaywrightTimeoutError:
                logger.warning("No se confirmó página %s.", target_str)
                continue
        logger.error("Agotados reintentos para página %s.", target_str)
        return False

    # ── Scraping de imágenes ─────────────────────────────────────────────────
    async def _scrape_image_results(self, page: Page, keyword: str, total_pages: int = 3) -> None:
        context = page.context
        image_tab = page.locator('div[aria-label="refinement"][role="tab"]:has-text("Image")')
        try:
            await image_tab.wait_for(state="visible", timeout=5_000)
        except PlaywrightTimeoutError:
            logger.debug("Pestaña Image no encontrada.")
            return
        class_attr = await image_tab.get_attribute("class") or ""
        if "gsc-tabhActive" not in class_attr:
            logger.info("🖼 Cambiando a búsqueda por imágenes...")
            await self._unblock_images(context)
            await asyncio.sleep(0.5)
            try:
                await image_tab.click(timeout=3_000)
            except Exception:
                await self._quick_move_and_click(page, image_tab)
            await asyncio.sleep(2.0)
            # Esperar a que el tab de imágenes esté activo
            try:
                await page.wait_for_selector(".gsc-tabdActive a.gs-previewLink", state="attached", timeout=10_000)
            except PlaywrightTimeoutError:
                logger.warning("El tab de imágenes no se activó correctamente.")
                await self._reblock_images(context)
                return
        else:
            await self._unblock_images(context)
            await asyncio.sleep(0.5)

        # Asegurarse de que hay previews
        try:
            await page.wait_for_selector("a.gs-previewLink", state="attached", timeout=5_000)
        except PlaywrightTimeoutError:
            logger.warning("No se encontraron previews de imágenes.")
            await self._reblock_images(context)
            return

        processed_urls: set[str] = set()
        for img_page in range(1, total_pages + 1):
            logger.info("🖼 Página imágenes %d/%d | '%s'", img_page, total_pages, keyword)
            # Scroll para cargar imágenes
            await self._incremental_scroll(page)
            preview_links = page.locator("a.gs-previewLink")
            link_count = await preview_links.count()
            new_in_page = 0
            for i in range(link_count):
                try:
                    link = preview_links.nth(i)
                    href = (await link.get_attribute("href") or "").strip()
                    if not href or href in processed_urls:
                        continue
                    if not any(p in href for p in _FACEBOOK_URL_PATTERNS):
                        continue
                    url_clean = clean_url(href)
                    if not url_clean:
                        continue
                    processed_urls.add(href)
                    new_in_page += 1
                    platform = self._platform or ("instagram" if "instagram" in url_clean else "facebook")
                    self._scraped_results.append(ScrapedResult(
                        url=url_clean, platform=platform, published_at=None, published_at_raw=None
                    ))
                except Exception as exc:
                    logger.debug("[IMG] Error preview #%d: %s", i, exc)
            logger.info("[IMG] %d URLs nuevas en página %d.", new_in_page, img_page)
            if self._scraped_results:
                await self._persist_results(keyword)
                self._scraped_results.clear()
            if img_page < total_pages:
                # Pequeño scroll extra para asegurar que el paginador esté al alcance
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3)
                success = await self._navigate_to_page(page, img_page + 1)
                if not success:
                    logger.warning("No se pudo navegar a página %d de imágenes.", img_page + 1)
                    break
        await self._reblock_images(context)
        logger.info("[IMG] Total URLs únicas: %d | '%s'", len(processed_urls), keyword)

    @staticmethod
    def _play_alert_sound() -> None:
        try:
            import platform as _plt
            if _plt.system() == "Windows":
                import winsound
                winsound.Beep(400, 400)
                winsound.Beep(400, 400)
                for f, d in zip([500,600,800,1000,1200,1500,1800], [350,300,250,200,180,150,120]):
                    winsound.Beep(f, d)
                for _ in range(5):
                    winsound.Beep(2000, 80)
                winsound.Beep(1500, 200)
                winsound.Beep(1000, 300)
                winsound.Beep(600, 400)
            else:
                import time
                sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.4)
                sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.4)
                for iv in [0.35,0.30,0.25,0.20,0.15,0.12,0.10]:
                    sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(iv)
                for _ in range(5):
                    sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.05)
                sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.2)
                sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.3)
                sys.stdout.write("\a"); sys.stdout.flush()
        except Exception:
            pass

    async def setup_page(self, page: Page, fingerprint: BrowserFingerprint) -> None:
        await self.apply_stealth(page, fingerprint)
        await CaptchaDetector.intercept_response_errors(page)
        logger.debug("Page setup completo.")

    async def run_keyword(self, page: Page, keyword: str, total_pages: int = 3) -> None:
        self._scraped_results.clear()
        await self._inject_referrer(page)
        await page.goto(self._search_url, wait_until="domcontentloaded", timeout=20_000)
        await CaptchaDetector.check(page, keyword)
        try:
            await page.wait_for_selector("input.gsc-input", state="visible", timeout=8_000)
        except PlaywrightTimeoutError:
            logger.warning("Timeout esperando search box.")
        search_box = page.locator("input.gsc-input")
        await search_box.click(timeout=3_000)
        await human_type(page, "input.gsc-input", keyword, clear_first=True, wpm=random.randint(80,130))
        await asyncio.sleep(random.uniform(0.2,0.4))
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_selector(".gsc-webResult", state="visible", timeout=12_000)
        except PlaywrightTimeoutError:
            await CaptchaDetector.check(page, keyword)
            logger.info("Sin resultados para '%s'.", keyword)
            return
        await CaptchaDetector.check(page, keyword)
        await self._apply_date_filter(page)
        for current_p in range(1, total_pages + 1):
            logger.info("Página %d/%d | '%s'", current_p, total_pages, keyword)
            try:
                await self._extract_page_results(page, keyword)
            except CaptchaError as cap_err:
                logger.warning("CAPTCHA detectado (%s).", cap_err.signal)
                self._play_alert_sound()
                if await CaptchaAutosolver.try_solve_checkbox(page=page, keyword=keyword, max_attempts=2):
                    logger.info("CAPTCHA resuelto.")
                    await self._extract_page_results(page, keyword)
                else:
                    raise
            await self._persist_results(keyword)
            self._scraped_results.clear()
            if current_p < total_pages:
                if not await self._navigate_to_page(page, current_p + 1):
                    logger.warning("No se pudo navegar a página %d.", current_p + 1)
                    break
        try:
            await self._scrape_image_results(page, keyword, total_pages)
        except CaptchaError:
            raise
        except Exception as exc:
            logger.error("Error en búsqueda de imágenes: %s", exc)