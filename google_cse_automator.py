"""
google_cse_automator.py
Lógica de scraping CSE, detección de CAPTCHA y envío aislado al store.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import httpx
from dateutil.relativedelta import relativedelta
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from config.settings import settings
from utils.captcha_guard import CaptchaDetector, CaptchaError
#from utils.fb_url_validator import is_valid_fb_url
from utils.url_clean import clean_url

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuración del store de URLs
# ─────────────────────────────────────────────────────────────────────────────

_DATA_STORE_TOKEN = "42|htoFv3uJ8ZIJMuWoSDQkmLOK0vnv5GSoGbQaKDWBf2cb6b41"
_DATA_STORE_HEADERS = {
    "Authorization": f"Bearer {_DATA_STORE_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# ─────────────────────────────────────────────────────────────────────────────
# Pools de rotación
# ─────────────────────────────────────────────────────────────────────────────
_USER_AGENT_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

_VIEWPORT_POOL: List[Dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

_LOCALE_TZ_POOL: List[Tuple[str, str]] = [
    ("en-US", "America/New_York"), ("en-US", "America/Chicago"), ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"), ("en-CA", "America/Toronto"),
]

_UNIT_MAP: Dict[str, str] = {
    "minuto": "minutes", "minutos": "minutes", "hora": "hours", "horas": "hours",
    "día": "days", "días": "days", "semana": "weeks", "semanas": "weeks",
    "mes": "months", "meses": "months", "año": "years", "años": "years",
}
_RELATIVEDELTA_UNITS = frozenset({"months", "years"})
_RELATIVE_TS_PATTERN = re.compile(
    r"^(hace\s+(\d+)\s+(hora|horas|minuto|minutos|día|días|semana|semanas|mes|meses|año|años))",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Tipos de Datos
# ─────────────────────────────────────────────────────────────────────────────
class ScrapedResult(TypedDict):
    url: str
    published_at: Optional[datetime]
    published_at_raw: Optional[str]

# ─────────────────────────────────────────────────────────────────────────────
# Tor Manager
# ─────────────────────────────────────────────────────────────────────────────
class TorManager:
    """Gestiona renovación de circuitos Tor y expone configuración de proxy."""
    def __init__(self, proxy: str = "socks5://127.0.0.1:9050", control_port: int = 9051, password: Optional[str] = None) -> None:
        self.proxy = proxy
        self.control_port = control_port
        self.password = password
        self._controller: Any = None

    async def renew_circuit(self) -> None:
        """Solicita nuevo circuito Tor sin bloquear el event loop."""
        try:
            from stem import Signal
            from stem.control import Controller

            if self._controller is None:
                self._controller = Controller.from_port(port=self.control_port)
                self._controller.authenticate(password=self.password)

            await asyncio.to_thread(self._controller.signal, Signal.NEWNYM)
            logger.info("Circuito Tor renovado exitosamente.")
        except Exception as exc:
            logger.error(f"Error al renovar circuito Tor: {exc}")
            raise

    def get_proxy_settings(self) -> Dict[str, str]:
        return {"server": self.proxy}

# ─────────────────────────────────────────────────────────────────────────────
# BrowserConfig
# ─────────────────────────────────────────────────────────────────────────────
class BrowserConfig:
    page_load_wait_range: Tuple[float, float] = (4.0, 7.0)
    scroll_pause_range: Tuple[float, float] = (0.4, 1.2)
    between_pages_range: Tuple[float, float] = (4.0, 10.0)
    between_keywords_range: Tuple[float, float] = (5.0, 15.0)
    typing_mean: float = 0.12
    typing_std: float = 0.05
    warmup_url: str = "https://www.google.com"
    warmup_pause_range: Tuple[float, float] = (2.0, 4.0)
    captcha_max_wait_seconds: float = 300.0
    captcha_wait_for_human: bool = False
    max_tor_retries: int = 2

    def pick_user_agent(self) -> str: return random.choice(_USER_AGENT_POOL)
    def pick_viewport(self) -> Dict[str, int]: return random.choice(_VIEWPORT_POOL)
    def pick_locale_tz(self) -> Tuple[str, str]: return random.choice(_LOCALE_TZ_POOL)

    def jitter_wait(self, low: float, high: float) -> float:
        mid = (low + high) / 2.0
        sigma = (high - low) / 6.0
        base = random.gauss(mid, sigma)
        extra = random.expovariate(5.0)
        return max(low, min(high + extra * 0.5, base + extra * 0.2))

    def build_context_options(self, proxy: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        locale, tz_id = self.pick_locale_tz()
        opts: Dict[str, Any] = {
            "user_agent": self.pick_user_agent(),
            "locale": locale,
            "timezone_id": tz_id,
            "viewport": self.pick_viewport(),
            "extra_http_headers": {
                "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
            },
            "java_script_enabled": True,
        }
        if proxy:
            opts["proxy"] = proxy
        return opts

# ─────────────────────────────────────────────────────────────────────────────
# GoogleCSEAutomator
# ─────────────────────────────────────────────────────────────────────────────
class GoogleCSEAutomator:
    def __init__(self, cse_id: str, config: Optional[BrowserConfig] = None) -> None:
        self._search_url = f"https://cse.google.com/cse?cx={cse_id}"
        self.cfg = config or BrowserConfig()
        self._scraped_results: List[ScrapedResult] = []

    async def _human_sleep(self, low: float, high: float) -> None:
        await asyncio.sleep(self.cfg.jitter_wait(low, high))

    async def _human_type(self, page: Page, selector: str, text: str) -> None:
        await page.click(selector)
        for char in text:
            await page.keyboard.type(char)
            delay = max(0.04, random.gauss(self.cfg.typing_mean, self.cfg.typing_std))
            await asyncio.sleep(delay)

    async def _arc_move_and_click(self, page: Page, locator: Any) -> None:
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click()
                return
            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            viewport = self.cfg.pick_viewport()
            start_x, start_y = random.uniform(0, viewport["width"]), random.uniform(0, viewport["height"])
            steps = random.randint(12, 25)
            for step in range(steps + 1):
                t = step / steps
                t_ease = (1 - math.cos(math.pi * t)) / 2
                arc_offset = math.sin(math.pi * t) * random.uniform(-30, 30)
                hyp = max(1, math.hypot(target_x - start_x, target_y - start_y))
                perp_x, perp_y = -(target_y - start_y) / hyp, (target_x - start_x) / hyp
                x = start_x + (target_x - start_x) * t_ease + perp_x * arc_offset
                y = start_y + (target_y - start_y) * t_ease + perp_y * arc_offset
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.005, 0.025))
            await page.mouse.click(target_x, target_y)
        except Exception as exc:
            logger.debug(f"arc_move falló, click directo: {exc}")
            await locator.click()

    @staticmethod
    async def _apply_stealth(page: Page) -> None:
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const p = [{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:''},
                               {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
                               {name:'Native Client',filename:'internal-nacl-plugin',description:''}];
                    p.refresh=()=>{}; p.item=(i)=>p[i]; p.namedItem=(n)=>p.find(x=>x.name===n)||null;
                    Object.defineProperty(p,'length',{get:()=>p.length}); return p;
                }, configurable: true
            });
            if (!window.chrome) window.chrome = {runtime:{}, loadTimes:function(){}, csi:function(){}, app:{}};
            Object.defineProperty(Notification, 'permission', {get: () => 'default', configurable: true});
            const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx) { const d = ctx.getImageData(0,0,this.width||1,this.height||1); for(let i=0;i<d.data.length;i+=100) d.data[i]^=(Math.random()*2)|0; ctx.putImageData(d,0,0); }
                return _toDataURL.apply(this, arguments);
            };
        """)

    async def _warmup_session(self, page: Page) -> None:
        try:
            await page.goto(self.cfg.warmup_url, wait_until="domcontentloaded")
            await self._human_sleep(*self.cfg.warmup_pause_range)
            await page.evaluate(f"window.scrollTo(0, {random.randint(50, 200)})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        except Exception as exc:
            logger.debug(f"Warmup ignorado: {exc}")

    @staticmethod
    def _parse_datetime_from_relative(snippet_text: str) -> Optional[datetime]:
        if not snippet_text: return None
        match = _RELATIVE_TS_PATTERN.match(snippet_text.strip())
        if not match: return None
        value = int(match.group(2))
        unit_es = match.group(3).lower()
        delta_unit = _UNIT_MAP.get(unit_es)
        if not delta_unit: return None
        try:
            now = datetime.now(timezone.utc)
            if delta_unit in _RELATIVEDELTA_UNITS: return now + relativedelta(**{delta_unit: -value})
            return now - timedelta(**{delta_unit: value})
        except (ValueError, TypeError, OverflowError) as exc:
            logger.warning(f"Error calculando datetime '{snippet_text[:40]}': {exc}")
            return None

    async def _extract_page_results(self, page: Page, keyword: str) -> None:
        await self._incremental_scroll(page)
        await CaptchaDetector.check(page, keyword)
        try:
            await page.wait_for_selector(".gsc-webResult", timeout=10_000)
        except PlaywrightTimeoutError:
            await CaptchaDetector.check(page, keyword)
            logger.info(f"Sin resultados para keyword='{keyword}' (timeout legítimo).")
            return

        results_container = page.locator(".gsc-expansionArea .gsc-webResult")
        count = await results_container.count()
        for i in range(count):
            try:
                container = results_container.nth(i)
                link = container.locator(".gs-title a.gs-title").first
                href = await link.get_attribute("data-ctorig") or await link.get_attribute("href")
                if not href or "google.com" in href: continue
                #if not is_valid_fb_url(href): continue

                snippet_text = (await container.locator(".gs-snippet").first.text_content() or "").strip()
                ts_match = _RELATIVE_TS_PATTERN.match(snippet_text)
                self._scraped_results.append(ScrapedResult(
                    url=clean_url(href),
                    published_at=self._parse_datetime_from_relative(snippet_text) if ts_match else None,
                    published_at_raw=ts_match.group(1) if ts_match else None,
                ))
            except Exception as exc:
                logger.error(f"Error procesando resultado #{i}: {exc}")

    async def _incremental_scroll(self, page: Page) -> None:
        page_height = await page.evaluate("document.body.scrollHeight")
        current_y = 0.0
        viewport_h = await page.evaluate("window.innerHeight") or 768
        while current_y < page_height:
            current_y = min(current_y + random.uniform(viewport_h * 0.3, viewport_h * 0.8), page_height)
            await page.evaluate(f"window.scrollTo(0, {current_y})")
            await asyncio.sleep(random.uniform(*self.cfg.scroll_pause_range))

    async def _send_urls_to_store(self) -> Tuple[int, int]:
        """Envía URLs al API SIN proxy Tor y manejando certificados locales."""
        valid_urls = [item["url"] for item in self._scraped_results if item.get("url")]
        if not valid_urls:
            return 0, 0

        sent_ok = failed = 0
        # proxy=None (singular) fuerza conexión directa en httpx >= 0.23.0.
        # verify=False evita CERTIFICATE_VERIFY_FAILED en endpoints con CA interna.



        async with httpx.AsyncClient(
            headers=_DATA_STORE_HEADERS,
            timeout=10.0,
            proxy=None,
            verify=False
        ) as client:
            for post_url in valid_urls:
                platform = "instagram" if "instagram" in post_url else "facebook"
                _DATA_STORE_ENDPOINT = f"https://notires.rem.cu/api/{platform}/urls"
                try:
                    response = await client.post(_DATA_STORE_ENDPOINT, json={"post_url": post_url})
                    if response.is_success:
                        sent_ok += 1
                    else:
                        failed += 1
                        logger.warning(f"Store rechazó URL (HTTP {response.status_code}): {post_url[:80]}")
                except httpx.RequestError as exc:
                    failed += 1
                    logger.error(f"Error de conexión enviando URL: {exc}")
        return sent_ok, failed

    async def _solve_date_filter(self, page: Page) -> None:
        try:
            dropdown = page.locator(".gsc-selected-option-container").first
            await self._arc_move_and_click(page, dropdown)
            await page.locator(".gsc-option-menu-item", has_text="Date").wait_for(state="visible", timeout=5000)
            await self._arc_move_and_click(page, page.locator(".gsc-option-menu-item", has_text="Date"))
            await page.wait_for_load_state("networkidle")
            await self._human_sleep(1.5, 3.0)
        except Exception as exc:
            logger.warning(f"No se pudo activar filtro de fecha: {exc}")

    @staticmethod
    def _play_alert_sound() -> None:
        try:
            import platform
            if platform.system() == "Windows":
                import winsound; winsound.Beep(1000, 500)
            else:
                sys.stdout.write("\a"); sys.stdout.flush()
        except Exception: pass

    async def run_keyword(self, page: Page, keyword: str, total_pages: int = 3) -> List[ScrapedResult]:
        self._scraped_results.clear()
        await page.goto(self._search_url, wait_until="domcontentloaded")
        await CaptchaDetector.check(page, keyword)
        await self._human_sleep(*self.cfg.page_load_wait_range)

        search_box = page.locator("input.gsc-input")
        await self._arc_move_and_click(page, search_box)
        await self._human_type(page, "input.gsc-input", keyword)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle")
        await CaptchaDetector.check(page, keyword)
        await self._solve_date_filter(page)

        for current_p in range(1, total_pages + 1):
            try:
                await self._extract_page_results(page, keyword)
            except CaptchaError:
                if self.cfg.captcha_wait_for_human:
                    self._play_alert_sound()
                    logger.warning("Modo espera manual activo. Resuelva el CAPTCHA.")
                    resolved = await CaptchaDetector.wait_for_human_resolution(
                        page=page, keyword=keyword, max_wait=self.cfg.captcha_max_wait_seconds
                    )
                    if resolved:
                        await self._extract_page_results(page, keyword)
                    else:
                        raise
                else:
                    raise

            if current_p < total_pages:
                next_btn = page.locator(".gsc-cursor-page:not(.gsc-cursor-current-page)", has_text=str(current_p + 1))
                if await next_btn.is_visible():
                    await self._arc_move_and_click(page, next_btn)
                    await page.locator(".gsc-cursor-current-page").filter(has_text=str(current_p + 1)).wait_for(state="visible", timeout=10_000)
                    await self._human_sleep(*self.cfg.between_pages_range)
                else:
                    break

        sent_ok, failed = await self._send_urls_to_store()
        logger.info(f"Store: {sent_ok} URLs enviadas, {failed} fallidas (keyword='{keyword}').")
        return self._scraped_results.copy()