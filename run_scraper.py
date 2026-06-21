"""
run_scraper.py
Orquestador principal del scraper con ejecución concurrente por FILTER.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Final

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from anti_detection import BrowserFingerprint, generate_fingerprint
from config.settings import settings
from database.supabase_client import SupabaseManager
from google_cse_automator import BrowserConfig, GoogleCSEAutomator
from utils.captcha_guard import CaptchaError
from utils.session_store import SessionStore

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# FILTERs a ejecutar concurrentemente
# ─────────────────────────────────────────────────────────────────────────────
FILTERS: list[str] | None = [
    #"Especial",
    # "Kano Cluster CR",
    #"KW FB",
    #"KW IG",
    #"FB CR General Kano",
    
]

MAX_CONCURRENT_BROWSERS: Final[int] = getattr(settings, "MAX_CONCURRENT_BROWSERS", 2)

# ─────────────────────────────────────────────────────────────────────────────
# Fallback engines
# ─────────────────────────────────────────────────────────────────────────────
_FALLBACK_ENGINES: list[dict] = [
    {
        "label":      "IG-KW-Engine",
        "engine_id":  "c4b97eed1414fcb14",
        "platform":   "instagram",
        "keywords": [
            "#Cuba",  "Cuba",  "#CubaVive",
            "#YoSigoAMiPresidente",  "#CubaPorLaSalud",
            "#TumbaElBloqueo",  "#NoMasBloqueo",  "#CubaNoEstaSola",
            "#FidelPorSiempre",  "#CubaCoopera",  "#CubaPorLaVida",
            "#CubaEstaFirme",  "#CubaSoberana",
            "cubanos",  "habana",  "havana",
        ],
    },
]

import platform
import subprocess

def kill_browser_processes() -> None:
    """Mata procesos huérfanos del navegador en Linux."""
    try:
        subprocess.run(["pkill", "-f", "camoufox"], capture_output=True, timeout=5)
        subprocess.run(["pkill", "-f", "firefox"], capture_output=True, timeout=5)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ScraperOrchestrator
# ─────────────────────────────────────────────────────────────────────────────
class ScraperOrchestrator:
    """
    Orquestador que gestiona browser, contextos, Supabase y el ciclo de keywords.
    """

    def __init__(
        self,
        filter_label: str | None = None,
        browser_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._running             = False
        self._filter_label        = filter_label
        self._browser_semaphore   = browser_semaphore
        self._cfg                 = BrowserConfig()
        self._session_store       = SessionStore(settings.SESSION_DIR)
        self._db:        SupabaseManager | None = None
        self._current_headless: bool = getattr(settings, "BROWSER_HEADLESS_DEFAULT", True)

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _db_connect(self) -> None:
        self._db = SupabaseManager()
        await self._db.connect()
        logger.info("Supabase conectado.")

    async def _db_disconnect(self) -> None:
        if self._db:
            await self._db.disconnect()
            self._db = None
            logger.info("Supabase desconectado.")

    # ── Engines config ────────────────────────────────────────────────────────

    async def _fetch_engines_config(self) -> list[dict]:
        if self._db is None or self._db.keyword_repo is None:
            logger.warning("Supabase no inicializado. Usando fallback.")
            return _FALLBACK_ENGINES

        if self._filter_label:
            claimed = await self._db.keyword_repo.claim_keywords(label=self._filter_label, limit=10)
        else:
            claimed = await self._db.keyword_repo.claim_keywords(label=None, limit=10)

        if not claimed:
            logger.warning("No hay keywords disponibles.")
            return _FALLBACK_ENGINES

        engines_by_label: dict[str, dict] = {}
        for kw in claimed:
            lbl = kw.label or "sin_label"
            if lbl not in engines_by_label:
                engines_by_label[lbl] = {
                    "label": lbl,
                    "engine_id": kw.engine,
                    "platform": kw.platform,
                    "keywords": [],
                    "_keyword_ids": [],
                }
            engines_by_label[lbl]["keywords"].append(kw.keyword)
            engines_by_label[lbl]["_keyword_ids"].append(kw.id)

        engines_config = list(engines_by_label.values())
        for engine in engines_config:
            logger.info("[CONFIG] Label='%s' | %d keywords | Engine=%s", engine["label"], len(engine["keywords"]), engine["engine_id"])
        return engines_config

    # ── Browser management ────────────────────────────────────────────────────

    @staticmethod
    def _map_platform_to_camoufox_os(navigator_platform: str) -> str:
        platform_lower = navigator_platform.lower()
        if "win" in platform_lower: return "windows"
        if "mac" in platform_lower: return "macos"
        if "linux" in platform_lower: return "linux"
        return "windows"

    async def _launch_camoufox(
        self,
        headless: bool,
        label: str,
        camoufox_os: str,
    ) -> tuple[AsyncCamoufox, Browser]:
        camoufox_params: dict = {
            "headless": headless,
            "humanize": settings.CAMOUFOX_HUMANIZE,
            "geoip":    settings.CAMOUFOX_GEOIP,
            "os":       camoufox_os,
        }
        from utils.proxy_manager import proxy_manager
        if proxy_manager.is_enabled:
            camoufox_params["proxy"] = proxy_manager.playwright_proxy

        camoufox_instance = AsyncCamoufox(**camoufox_params)
        browser = await camoufox_instance.__aenter__()
        mode_str = "HEADLESS" if headless else "VISIBLE"
        logger.info("[%s] Camoufox iniciado en modo %s | OS=%s", label, mode_str, camoufox_os)
        return camoufox_instance, browser

    async def _save_context_state(self, context: BrowserContext, domain: str = "google.com") -> dict | None:
        if not settings.SESSION_PERSIST or not context: return None
        try:
            state = await context.storage_state()
            return state
        except Exception:
            return None

    async def _create_context_and_page(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        session_domain: str = "google.com",
        storage_state: dict | None = None,
        skip_warmup: bool = False,
    ) -> tuple[BrowserContext, Page]:
        context_options = fingerprint.build_context_options()
        if storage_state:
            context_options["storage_state"] = storage_state
        elif settings.SESSION_PERSIST and session_domain:
            saved_state = self._session_store.load_state_dict(session_domain)
            if saved_state:
                context_options["storage_state"] = saved_state

        context: BrowserContext = await browser.new_context(**context_options)
        await automator.block_images_async(context, url_pattern="https://encrypted-tbn0.gstatic.com/images")
        page: Page = await context.new_page()
        await automator.setup_page(page, fingerprint)
        if not skip_warmup:
            await automator._warmup_session(page)
        return context, page

    async def _rotate_identity(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        old_context: BrowserContext,
        label: str,
        preserve_session: bool = True,
    ) -> tuple[BrowserContext, Page, BrowserFingerprint]:
        storage_state = await self._save_context_state(old_context) if preserve_session else None
        try: await old_context.close()
        except Exception: pass
        
        new_fp = generate_fingerprint("firefox")
        new_context, new_page = await self._create_context_and_page(
            browser=browser, automator=automator, fingerprint=new_fp,
            session_domain="", storage_state=storage_state,
        )
        return new_context, new_page, new_fp

    async def _switch_browser_visibility(
        self,
        old_camoufox: AsyncCamoufox,
        old_browser: Browser,
        old_context: BrowserContext,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        label: str,
        new_headless: bool,
        captcha_url: str = "",
    ) -> tuple[AsyncCamoufox, Browser, BrowserContext, Page]:
        """Cambia la visibilidad del browser preservando fingerprint y sesión."""
        from_mode = "HEADLESS" if not new_headless else "VISIBLE"
        to_mode   = "VISIBLE"  if not new_headless else "HEADLESS"
        logger.info("[%s] Cambiando visibilidad: %s → %s", label, from_mode, to_mode)

        storage_state = await self._save_context_state(old_context)

        # Cierre seguro
        for resource in (old_context, old_browser):
            try: await resource.close()
            except Exception: pass
        try: await old_camoufox.__aexit__(None, None, None)
        except Exception: pass

        await asyncio.sleep(1)
        kill_browser_processes()
        await asyncio.sleep(1)

        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)
        camoufox_params: dict = {
            "headless": new_headless,
            "humanize": settings.CAMOUFOX_HUMANIZE,
            "geoip": settings.CAMOUFOX_GEOIP,
            "os": camoufox_os,
        }
        from utils.proxy_manager import proxy_manager
        if proxy_manager.is_enabled:
            camoufox_params["proxy"] = proxy_manager.playwright_proxy

        new_camoufox = AsyncCamoufox(**camoufox_params)
        new_browser = await new_camoufox.__aenter__()

        new_context, new_page = await self._create_context_and_page(
            browser=new_browser, automator=automator, fingerprint=fingerprint,
            session_domain="google.com", storage_state=storage_state, skip_warmup=True,
        )

        # Navegación segura al CAPTCHA
        if not new_headless and captcha_url:
            try:
                logger.info("[%s] Navegando a URL del CAPTCHA...", label)
                await new_page.goto(captcha_url, wait_until="domcontentloaded", timeout=20_000)
            except Exception as nav_exc:
                logger.warning("[%s] Fallo navegando a captcha: %s. Intentando reload...", label, nav_exc)
                try:
                    await new_page.reload(wait_until="domcontentloaded")
                except Exception:
                    logger.error("[%s] Imposible recuperar página tras cambio de visibilidad.", label)

        if not new_headless:
            try:
                await new_page.evaluate("""
                    () => {
                        const banner = document.createElement('div');
                        banner.style.cssText = `position: fixed; top: 0; left: 0; right: 0; background: #e53935; color: white; padding: 14px; text-align: center; font-weight: bold; z-index: 99999;`;
                        banner.textContent = '⚠ CAPTCHA DETECTADO — Resuélvelo en menos de 2 minutos';
                        document.body ? document.body.prepend(banner) : document.documentElement.prepend(banner);
                    }
                """)
            except Exception: pass
            logger.info("[%s] ══ NAVEGADOR VISIBLE ══ Resuelve el CAPTCHA y presiona ENTER.", label)

        return new_camoufox, new_browser, new_context, new_page
    
    async def _wait_for_captcha_solved(self, page: Page, timeout_seconds: int = 120) -> bool:
        """
        Espera a que el CAPTCHA sea resuelto detectando la reaparición
        del contenedor de resultados (.gsc-webResult).
        Retorna True si se resuelve a tiempo, False en caso de timeout.
        """
        try:
            await page.wait_for_selector(
                ".gsc-webResult", state="visible", timeout=timeout_seconds * 1000
            )
            logger.info("CAPTCHA resuelto – resultados visibles nuevamente.")
            return True
        except PlaywrightTimeoutError:
            logger.warning("Timeout de %ds esperando resolución del CAPTCHA.", timeout_seconds)
            return False
    
    async def _run_engine_keywords(
        self,
        engine_id: str,
        label: str,
        platform: str,
        keywords: list[str],
        keyword_ids: list[int],
        total_pages: int = 3,
    ) -> None:
        semaphore_acquired = False
        active_keyword_ids = set(keyword_ids)

        automator = GoogleCSEAutomator(
            cse_id=engine_id, platform=platform,
            url_repo=self._db.url_repo if self._db else None,
            config=self._cfg,
        )

        self._current_headless = getattr(settings, "BROWSER_HEADLESS_DEFAULT", True)
        fingerprint: BrowserFingerprint = generate_fingerprint("firefox")
        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)

        camoufox_instance: AsyncCamoufox | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None

        try:
            if self._browser_semaphore:
                await self._browser_semaphore.acquire()
                semaphore_acquired = True

            camoufox_instance, browser = await self._launch_camoufox(
                headless=self._current_headless, label=label, camoufox_os=camoufox_os,
            )

            context, page = await self._create_context_and_page(
                browser=browser, automator=automator, fingerprint=fingerprint,
            )

            for idx, (raw_kw, kw_id) in enumerate(zip(keywords, keyword_ids), 1):
                if not self._running:
                    break

                kw = raw_kw.strip()
                if not kw:
                    continue

                logger.info("[%s] [%d/%d] keyword='%s'", label, idx, len(keywords), kw)

                try:
                    await automator.run_keyword(page, kw, total_pages)
                    if self._db and self._db.keyword_repo:
                        await self._db.keyword_repo.mark_scraped(kw_id)
                    active_keyword_ids.discard(kw_id)

                except CaptchaError as captcha_exc:
                    logger.warning("[%s] ⚠ CAPTCHA DETECTADO en '%s'", label, kw)
                    GoogleCSEAutomator._play_alert_sound()

                    captcha_url: str = ""
                    try:
                        captcha_url = page.url
                    except Exception:
                        pass

                    was_headless = self._current_headless
                    browser_crashed = False

                    if getattr(settings, "BROWSER_VISIBLE_ON_CAPTCHA", True):
                        if was_headless:
                            logger.info("[%s] Cambiando a VISIBLE...", label)
                            try:
                                (camoufox_instance, browser, context, page) = await self._switch_browser_visibility(
                                    old_camoufox=camoufox_instance, old_browser=browser,
                                    old_context=context, automator=automator,
                                    fingerprint=fingerprint, label=label,
                                    new_headless=False, captcha_url=captcha_url,
                                )
                                self._current_headless = False
                            except Exception as switch_exc:
                                logger.error("[%s] Fallo crítico al cambiar visibilidad: %s", label, switch_exc)
                                browser_crashed = True
                        else:
                            # Ya visible: intentar navegar al CAPTCHA
                            if captcha_url:
                                try:
                                    if page.is_closed():
                                        page = await context.new_page()
                                    await page.goto(captcha_url, wait_until="domcontentloaded", timeout=10_000)
                                except Exception as nav_exc:
                                    logger.error("[%s] Fallo navegando a captcha (posible crash): %s", label, nav_exc)
                                    browser_crashed = True

                        if not browser_crashed:
                            # ══════════ NUEVA ESPERA AUTOMÁTICA ══════════
                            resolved = await self._wait_for_captcha_solved(page, timeout_seconds=120)
                            if resolved:
                                try:
                                    await automator.run_keyword(page, kw, total_pages)
                                    if self._db and self._db.keyword_repo:
                                        await self._db.keyword_repo.mark_scraped(kw_id)
                                    active_keyword_ids.discard(kw_id)
                                except Exception:
                                    logger.error("[%s] Fallo en reintento post-CAPTCHA.", label)
                            else:
                                # Timeout: forzar reinicio completo del navegador
                                browser_crashed = True
                                logger.warning("[%s] No se resolvió el CAPTCHA en 2 min. Reiniciando navegador con perfil nuevo.", label)

                    if browser_crashed:
                        logger.warning("[%s] 🔄 Reiniciando sesión por fallo/crash...", label)
                        for r in [page, context, browser]:
                            try:
                                await r.close()
                            except:
                                pass
                        if camoufox_instance:
                            try:
                                await camoufox_instance.__aexit__(None, None, None)
                            except:
                                pass

                        kill_browser_processes()
                        await asyncio.sleep(2)

                        # Nueva sesión limpia
                        fingerprint = generate_fingerprint("firefox")
                        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)
                        camoufox_instance, browser = await self._launch_camoufox(
                            headless=self._current_headless, label=label, camoufox_os=camoufox_os,
                        )
                        context, page = await self._create_context_and_page(
                            browser=browser, automator=automator, fingerprint=fingerprint,
                        )
                        # Descartamos la keyword actual para evitar reintentos peligrosos
                        active_keyword_ids.discard(kw_id)
                        if self._db and self._db.keyword_repo:
                            await self._db.keyword_repo.release_keywords([kw_id])

                except Exception as generic_exc:
                    logger.error("[%s] Error inesperado: %s", label, generic_exc)
                    active_keyword_ids.discard(kw_id)
                    if self._db and self._db.keyword_repo:
                        await self._db.keyword_repo.release_keywords([kw_id])
                    try:
                        if page and context:
                            await page.close()
                            page = await context.new_page()
                    except:
                        break

                await asyncio.sleep(self._cfg.jitter_wait(*self._cfg.between_keywords_range))

        finally:
            if active_keyword_ids and self._db and self._db.keyword_repo:
                await self._db.keyword_repo.release_keywords(list(active_keyword_ids))

            if semaphore_acquired and self._browser_semaphore:
                self._browser_semaphore.release()

            for resource in filter(None, [page, context]):
                try:
                    await resource.close()
                except:
                    pass
            if browser:
                try:
                    await browser.close()
                except:
                    pass
            if camoufox_instance:
                try:
                    await camoufox_instance.__aexit__(None, None, None)
                except:
                    pass


    async def _execute_cycle(self) -> None:
        engines: list[dict] = await self._fetch_engines_config()
        if not engines: return

        for engine in engines:
            if not self._running: break
            engine_id = engine.get("engine_id")
            keywords = engine.get("keywords", [])
            label = engine.get("label", "?")
            platform = engine.get("platform", "")
            keyword_ids = engine.get("_keyword_ids", [])

            if not engine_id or not keywords: continue

            try:
                await self._run_engine_keywords(
                    engine_id=engine_id, label=label, platform=platform,
                    keywords=keywords, keyword_ids=keyword_ids,
                    total_pages=settings.TOTAL_PAGES_PER_KEYWORD,
                )
            except Exception as critical_exc:
                logger.error("Fallo crítico en engine '%s': %s", label, critical_exc)
                if self._db and self._db.keyword_repo and keyword_ids:
                    await self._db.keyword_repo.release_keywords(keyword_ids)

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        def stop_handler():
            logger.info("Señal de interrupción. Apagando...")
            self.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_handler)

        await self._db_connect()
        try:
            while self._running:
                await self._execute_cycle()
                if not self._running: break
                await asyncio.sleep(settings.CYCLE_DELAY_SECONDS)
        except asyncio.CancelledError:
            pass
        finally:
            await self._db_disconnect()

    def stop(self) -> None:
        self._running = False


class ConcurrentFilterManager:
    def __init__(self, filters: list[str] | None = None) -> None:
        self._filters = filters
        self._tasks: list[asyncio.Task] = []
        self._orchestrators: list[ScraperOrchestrator] = []
        self._browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

    async def _run_filter_task(self, orchestrator: ScraperOrchestrator, filter_label: str | None) -> None:
        try: await orchestrator.start()
        except Exception as exc: logger.error("Error en filtro '%s': %s", filter_label, exc)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        def signal_handler() -> None: self.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        filters_to_run = self._filters if self._filters else [None]
        self._orchestrators = [
            ScraperOrchestrator(filter_label=flt, browser_semaphore=self._browser_semaphore)
            for flt in filters_to_run
        ]

        launch_delay = settings.CAMOUFOX_LAUNCH_DELAY
        for idx, (orch, flt) in enumerate(zip(self._orchestrators, filters_to_run)):
            task = asyncio.create_task(self._run_filter_task(orch, flt), name=f"filter-{flt or 'all'}")
            self._tasks.append(task)
            if idx < len(self._orchestrators) - 1 and launch_delay > 0:
                await asyncio.sleep(launch_delay)

        try: await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError: pass
        finally:
            for task in self._tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        for orchestrator in self._orchestrators: orchestrator.stop()
        for task in self._tasks:
            if not task.done(): task.cancel()


async def main() -> None:
    await ConcurrentFilterManager(filters=FILTERS).start()

if __name__ == "__main__":
    from pathlib import Path
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / settings.LOG_FILE, encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("camoufox").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)