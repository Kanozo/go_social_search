"""
Orquestador principal del scraper con ejecución concurrente por worker.
Cada worker es stateless: pide lote, procesa en UN motor aleatorio FB + UN motor aleatorio IG, marca, desconecta.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys
from pathlib import Path
from typing import Final, Any

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from anti_detection import BrowserFingerprint, generate_fingerprint
from config.settings import settings
from database.models import EngineConfig, KeywordRecord
from database.supabase_client import SupabaseManager
from google_cse_automator import BrowserConfig, GoogleCSEAutomator
from utils.captcha_guard import CaptchaError
from utils.session_store import SessionStore

logger = logging.getLogger(__name__)

MAX_CONCURRENT_WORKERS: Final[int] = settings.MAX_CONCURRENT_WORKERS
KEYWORDS_PER_BATCH: Final[int] = settings.KEYWORDS_PER_BATCH
CAPTCHA_TIMEOUT_SECONDS: Final[int] = 120  # 2 minutos para resolver


class WorkerConfig:
    """Configuración de un worker de scraping."""

    def __init__(self, worker_id: int) -> None:
        self.worker_id = worker_id
        self.engines = self._load_all_engines()

    def _load_all_engines(self) -> list[EngineConfig]:
        """Carga todos los motores configurados para ambas plataformas."""
        engines = []
        for platform, engine_list in settings.ENGINES.items():
            for engine_dict in engine_list:
                engines.append(
                    EngineConfig(
                        name=engine_dict["name"],
                        engine_id=engine_dict["engine_id"],
                        platform=platform,
                    )
                )
        return engines


class ScraperWorker:
    """
    Worker stateless de scraping.
    Ciclo: conecta → reclama lote → procesa en 1 motor FB aleatorio + 1 motor IG aleatorio → desconecta.
    """

    def __init__(self, worker_id: int) -> None:
        self.worker_id = worker_id
        self.config = WorkerConfig(worker_id)
        self._running = False
        self._cfg = BrowserConfig()
        self._session_store = SessionStore(settings.SESSION_DIR)
        self._current_headless: bool = settings.BROWSER_HEADLESS_DEFAULT

    def _session_name(self) -> str:
        """Genera nombre de sesión único por worker para evitar colisiones."""
        return f"worker_{self.worker_id}_google"

    async def _db_connect(self) -> SupabaseManager:
        """Conecta a Supabase y retorna el manager."""
        db = SupabaseManager()
        await db.connect()
        return db

    @staticmethod
    def _map_platform_to_camoufox_os(navigator_platform: str) -> str:
        platform_lower = navigator_platform.lower()
        if "win" in platform_lower:
            return "windows"
        if "mac" in platform_lower:
            return "macos"
        if "linux" in platform_lower:
            return "linux"
        return "windows"

    async def _launch_camoufox(
        self,
        headless: bool,
        camoufox_os: str,
    ) -> tuple[AsyncCamoufox, Browser]:
        """Inicia una instancia de Camoufox."""
        camoufox_params: dict = {
            "headless": headless,
            "humanize": settings.CAMOUFOX_HUMANIZE,
            "geoip": settings.CAMOUFOX_GEOIP,
            "os": camoufox_os,
        }

        from utils.proxy_manager import proxy_manager
        if proxy_manager.is_enabled:
            camoufox_params["proxy"] = proxy_manager.playwright_proxy

        camoufox_instance = AsyncCamoufox(**camoufox_params)
        browser = await camoufox_instance.__aenter__()
        
        mode_str = "HEADLESS" if headless else "VISIBLE"
        logger.info(
            "[Worker-%d] Camoufox iniciado en modo %s | OS=%s",
            self.worker_id, mode_str, camoufox_os,
        )
        return camoufox_instance, browser

    async def _save_context_state(
        self, 
        context: BrowserContext,
    ) -> dict[str, Any] | None:
        """Guarda estado de sesión con nombre único por worker."""
        if not settings.SESSION_PERSIST or not context:
            return None
        try:
            state = await context.storage_state()
            self._session_store.save_state_dict(self._session_name(), state)
            logger.debug(
                "[Worker-%d] Sesión guardada: %s", 
                self.worker_id, self._session_name(),
            )
            return state
        except Exception:
            return None

    async def _create_context_and_page(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
    ) -> tuple[BrowserContext, Page]:
        """Crea contexto con sesión aislada por worker."""
        context_options = fingerprint.build_context_options()
        
        if settings.SESSION_PERSIST:
            saved_state = self._session_store.load_state_dict(self._session_name())
            if saved_state:
                context_options["storage_state"] = saved_state
                logger.debug(
                    "[Worker-%d] Sesión cargada: %s (%d cookies)", 
                    self.worker_id, 
                    self._session_name(),
                    len(saved_state.get("cookies", [])),
                )

        context: BrowserContext = await browser.new_context(**context_options)
        await automator.block_images_async(
            context, 
            url_pattern="https://encrypted-tbn0.gstatic.com/images",
        )
        page: Page = await context.new_page()
        await automator.setup_page(page, fingerprint)
        await automator._warmup_session(page)
        
        return context, page

    async def _switch_to_visible(
        self,
        old_camoufox: AsyncCamoufox,
        old_browser: Browser,
        old_context: BrowserContext,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        captcha_url: str,
    ) -> tuple[AsyncCamoufox, Browser, BrowserContext, Page] | None:
        """
        Cambia de headless a visible para que el usuario resuelva el CAPTCHA.
        Preserva la sesión del worker.
        """
        logger.info(
            "[Worker-%d] Cambiando a VISIBLE para resolución de CAPTCHA...",
            self.worker_id,
        )

        # Guardar sesión antes de cerrar
        await self._save_context_state(old_context)

        # Cierre limpio del browser headless
        for resource in (old_context, old_browser):
            try:
                await resource.close()
            except Exception:
                pass
        try:
            await old_camoufox.__aexit__(None, None, None)
        except Exception:
            pass

        await asyncio.sleep(1)

        # Iniciar nuevo browser visible
        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)
        camoufox_params: dict = {
            "headless": False,
            "humanize": settings.CAMOUFOX_HUMANIZE,
            "geoip": settings.CAMOUFOX_GEOIP,
            "os": camoufox_os,
        }
        from utils.proxy_manager import proxy_manager
        if proxy_manager.is_enabled:
            camoufox_params["proxy"] = proxy_manager.playwright_proxy

        try:
            new_camoufox = AsyncCamoufox(**camoufox_params)
            new_browser = await new_camoufox.__aenter__()

            new_context, new_page = await self._create_context_and_page(
                browser=new_browser,
                automator=automator,
                fingerprint=fingerprint,
            )

            # Navegar a la URL del CAPTCHA
            if captcha_url:
                try:
                    await new_page.goto(
                        captcha_url, 
                        wait_until="domcontentloaded", 
                        timeout=20_000,
                    )
                except Exception as nav_exc:
                    logger.warning(
                        "[Worker-%d] Fallo navegando a captcha: %s",
                        self.worker_id, nav_exc,
                    )

            # Mostrar banner de alerta
            try:
                await new_page.evaluate("""
                    () => {
                        const banner = document.createElement('div');
                        banner.id = 'captcha-alert-banner';
                        banner.style.cssText = `position: fixed; top: 0; left: 0; right: 0; background: #e53935; color: white; padding: 20px; text-align: center; font-weight: bold; z-index: 99999; font-size: 18px;`;
                        banner.textContent = '⚠ CAPTCHA DETECTADO — Resuélvelo en menos de 2 minutos';
                        document.body ? document.body.prepend(banner) : document.documentElement.prepend(banner);
                    }
                """)
            except Exception:
                pass

            # Reproducir sonido de alerta
            GoogleCSEAutomator._play_alert_sound()

            logger.info(
                "[Worker-%d] ══ NAVEGADOR VISIBLE ══ Resuelve el CAPTCHA",
                self.worker_id,
            )

            self._current_headless = False
            return new_camoufox, new_browser, new_context, new_page

        except Exception as exc:
            logger.error(
                "[Worker-%d] Error al cambiar a visible: %s",
                self.worker_id, exc,
            )
            return None

    async def _wait_for_captcha_resolution(self, page: Page) -> bool:
        """
        Espera a que el usuario resuelva el CAPTCHA.
        Detecta resolución por aparición de resultados .gsc-webResult.
        
        Returns:
            True si se resolvió, False si timeout.
        """
        logger.info(
            "[Worker-%d] Esperando resolución de CAPTCHA (%ds)...",
            self.worker_id, CAPTCHA_TIMEOUT_SECONDS,
        )

        try:
            # Esperar a que aparezcan resultados (indica CAPTCHA resuelto)
            await page.wait_for_selector(
                ".gsc-webResult",
                state="visible",
                timeout=CAPTCHA_TIMEOUT_SECONDS * 1000,
            )
            
            # Quitar banner de alerta si existe
            try:
                await page.evaluate("""
                    () => {
                        const banner = document.getElementById('captcha-alert-banner');
                        if (banner) banner.remove();
                    }
                """)
            except Exception:
                pass

            logger.info(
                "[Worker-%d] CAPTCHA resuelto por el usuario – continuando...",
                self.worker_id,
            )
            return True

        except PlaywrightTimeoutError:
            logger.warning(
                "[Worker-%d] CAPTCHA NO resuelto en %ds – abortando keyword",
                self.worker_id, CAPTCHA_TIMEOUT_SECONDS,
            )
            return False

    async def _restart_browser_clean(
        self,
        camoufox_instance: AsyncCamoufox | None,
        browser: Browser | None,
        context: BrowserContext | None,
    ) -> tuple[AsyncCamoufox, Browser, BrowserContext, Page]:
        """
        Cierra el browser actual e inicia uno nuevo con perfil limpio.
        Usado cuando el CAPTCHA no se resolvió.
        """
        logger.info(
            "[Worker-%d] Reiniciando browser con perfil limpio...",
            self.worker_id,
        )

        # Cierre limpio
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if camoufox_instance:
            try:
                await camoufox_instance.__aexit__(None, None, None)
            except Exception:
                pass

        await asyncio.sleep(2)

        # Borrar sesión para forzar perfil limpio
        self._session_store.delete(self._session_name())

        # Nuevo fingerprint y browser
        fingerprint: BrowserFingerprint = generate_fingerprint("firefox")
        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)

        self._current_headless = settings.BROWSER_HEADLESS_DEFAULT

        new_camoufox, new_browser = await self._launch_camoufox(
            headless=self._current_headless,
            camoufox_os=camoufox_os,
        )

        new_context, new_page = await self._create_context_and_page(
            browser=new_browser,
            automator=GoogleCSEAutomator(
                cse_id=self.config.engines[0].engine_id,
                platform=self.config.engines[0].platform,
                config=self._cfg,
            ),
            fingerprint=fingerprint,
        )

        return new_camoufox, new_browser, new_context, new_page

    def _select_random_engine_per_platform(self) -> list[EngineConfig]:
        """
        Selecciona exactamente un motor aleatorio por plataforma.
        
        Returns:
            Lista con 1 motor de Facebook y 1 motor de Instagram (aleatorios).
        """
        selected_engines: list[EngineConfig] = []
        
        for platform in ["facebook", "instagram"]:
            platform_engines = [e for e in self.config.engines if e.platform == platform]
            if platform_engines:
                selected = random.choice(platform_engines)
                selected_engines.append(selected)
                logger.debug(
                    "[Worker-%d] Motor seleccionado para %s: %s",
                    self.worker_id, platform, selected.name,
                )
        
        return selected_engines

    async def _process_keyword_on_engine(
        self,
        page: Page,
        keyword: KeywordRecord,
        engine: EngineConfig,
        db: SupabaseManager,
    ) -> tuple[bool, bool]:
        """
        Procesa una keyword en un motor específico.
        
        Returns:
            Tupla (success, captcha_triggered):
            - success: True si se completó sin errores
            - captcha_triggered: True si se detectó CAPTCHA
        """
        logger.info(
            "[Worker-%d] '%s' → motor '%s' (%s)",
            self.worker_id, keyword.term, engine.name, engine.platform,
        )

        try:
            engine_automator = GoogleCSEAutomator(
                cse_id=engine.engine_id,
                platform=engine.platform,
                url_repo=db.url_repo if db else None,
                config=self._cfg,
            )

            await engine_automator.run_keyword(
                page, keyword.term, settings.TOTAL_PAGES_PER_KEYWORD
            )
            
            await engine_automator._persist_results(keyword.term)
            
            logger.info(
                "[Worker-%d] Éxito: '%s' en '%s' (%s)",
                self.worker_id, keyword.term, engine.name, engine.platform,
            )
            return True, False

        except CaptchaError as captcha_exc:
            logger.warning(
                "[Worker-%d] CAPTCHA detectado en '%s' | '%s': %s",
                self.worker_id, keyword.term, engine.name, captcha_exc,
            )
            return False, True

        except Exception as exc:
            logger.error(
                "[Worker-%d] Error en '%s' | '%s': %s",
                self.worker_id, keyword.term, engine.name, exc,
            )
            return False, False


    async def _process_keyword_with_captcha_handling(
        self,
        browser: Browser,
        keyword: KeywordRecord,
        engine: EngineConfig,
        db: SupabaseManager,
        camoufox_instance: AsyncCamoufox,
        context: BrowserContext,
        page: Page,
        fingerprint: BrowserFingerprint,
    ) -> tuple[bool, AsyncCamoufox, Browser, BrowserContext, Page]:
        # Crear el automator para este motor
        engine_automator = GoogleCSEAutomator(
            cse_id=engine.engine_id,
            platform=engine.platform,
            url_repo=db.url_repo if db else None,
            config=self._cfg,
        )
        
        captcha_page = 1  # Trackear en qué página ocurre el CAPTCHA
        
        try:
            # Intentar procesar la keyword normalmente
            result = await engine_automator.run_keyword(
                page=page,
                keyword=keyword.term,
                total_pages=settings.TOTAL_PAGES_PER_KEYWORD,
            )
            
            # Si llegamos aquí, no hubo CAPTCHA o se completó todo
            return result['success'], camoufox_instance, browser, context, page

        except CaptchaError:
            # CAPTCHA detectado - determinar en qué página ocurrió
            # El run_keyword lanzó CaptchaError durante _extract_page_results
            # Necesitamos inspeccionar la página para saber dónde estamos
            
            # Verificar si hay paginador y qué página está activa
            try:
                current_page_elem = page.locator(".gsc-cursor-current-page")
                if await current_page_elem.count() > 0:
                    current_page_text = await current_page_elem.text_content()
                    captcha_page = int(current_page_text.strip()) if current_page_text else 1
                else:
                    # Si no hay paginador, probablemente es página 1
                    captcha_page = 1
            except Exception:
                captcha_page = 1
            
            logger.warning(
                "[Worker-%d] CAPTCHA detectado en página %d de '%s'",
                self.worker_id, captcha_page, keyword.term,
            )
            captcha_triggered = True

        if not captcha_triggered:
            # Sin CAPTCHA, todo normal (ya retornado arriba)
            return True, camoufox_instance, browser, context, page

        # ===== CAPTCHA DETECTADO =====
        captcha_url = ""
        try:
            captcha_url = page.url
        except Exception:
            pass

        # 1. Si está headless, cambiar a visible
        if self._current_headless and settings.BROWSER_VISIBLE_ON_CAPTCHA:
            switch_result = await self._switch_to_visible(
                old_camoufox=camoufox_instance,
                old_browser=browser,
                old_context=context,
                automator=GoogleCSEAutomator(
                    cse_id=engine.engine_id,
                    platform=engine.platform,
                    config=self._cfg,
                ),
                fingerprint=fingerprint,
                captcha_url=captcha_url,
            )

            if switch_result is None:
                # Fallo al cambiar a visible, reiniciar limpio
                new_camoufox, new_browser, new_context, new_page = await self._restart_browser_clean(
                    camoufox_instance, browser, context,
                )
                return False, new_camoufox, new_browser, new_context, new_page

            camoufox_instance, browser, context, page = switch_result

        # 2. Esperar a que el usuario resuelva (2 minutos)
        resolved = await self._wait_for_captcha_resolution(page)

        if resolved:
            # 3a. RESUELTO: Continuar DESDE la página donde ocurrió el CAPTCHA
            logger.info(
                "[Worker-%d] Reintentando '%s' desde página %d tras CAPTCHA resuelto",
                self.worker_id, keyword.term, captcha_page,
            )
            
            try:
                # Usar run_keyword_after_captcha con la página correcta
                result = await engine_automator.run_keyword_after_captcha(
                    page=page,
                    keyword=keyword.term,
                    total_pages=settings.TOTAL_PAGES_PER_KEYWORD,
                    last_page=captcha_page,  # <-- Página donde ocurrió el CAPTCHA
                )
                
                return result.success, camoufox_instance, browser, context, page
                
            except Exception as retry_exc:
                logger.error(
                    "[Worker-%d] Fallo reintento tras CAPTCHA: %s",
                    self.worker_id, retry_exc,
                )
                return False, camoufox_instance, browser, context, page

        else:
            # 3b. NO RESUELTO: Reiniciar browser limpio, saltar keyword
            logger.warning(
                "[Worker-%d] Saltando '%s' por CAPTCHA no resuelto",
                self.worker_id, keyword.term,
            )
            
            new_camoufox, new_browser, new_context, new_page = await self._restart_browser_clean(
                camoufox_instance, browser, context,
            )
            return False, new_camoufox, new_browser, new_context, new_page
            
    async def _process_keyword_all_engines(
        self,
        browser: Browser,
        keyword: KeywordRecord,
        db: SupabaseManager,
        camoufox_instance: AsyncCamoufox,
        context: BrowserContext,
        page: Page,
    ) -> tuple[bool, AsyncCamoufox, Browser, BrowserContext, Page]:
        """
        Procesa una keyword en UN motor aleatorio de FB y UN motor aleatorio de IG.
        Maneja CAPTCHA en cada motor.
        """
        engines_to_run = self._select_random_engine_per_platform()
        
        if not engines_to_run:
            logger.error("[Worker-%d] No hay motores disponibles", self.worker_id)
            return False, camoufox_instance, browser, context, page
        
        fingerprint: BrowserFingerprint = generate_fingerprint("firefox")

        any_success = False

        for engine in engines_to_run:
            if not self._running:
                break

            success, camoufox_instance, browser, context, page = await self._process_keyword_with_captcha_handling(
                browser=browser,
                keyword=keyword,
                engine=engine,
                db=db,
                camoufox_instance=camoufox_instance,
                context=context,
                page=page,
                fingerprint=fingerprint,
            )

            if success:
                any_success = True
            
            # Pequeña pausa entre motores
            await asyncio.sleep(0.5)

        # Guardar sesión al finalizar todos los motores
        await self._save_context_state(context)

        return any_success, camoufox_instance, browser, context, page

    async def _run_batch(self, db: SupabaseManager) -> int:
        """
        Ejecuta un ciclo completo: reclama lote, procesa en motores aleatorios, maneja CAPTCHA.
        """
        if not db.keyword_repo:
            logger.error("[Worker-%d] Keyword repo no disponible", self.worker_id)
            return 0

        batch = await db.keyword_repo.claim_keywords_batch(
            limit=KEYWORDS_PER_BATCH,
        )

        if not batch.keywords:
            logger.info("[Worker-%d] No hay keywords disponibles", self.worker_id)
            return 0

        logger.info(
            "[Worker-%d] Lote de %d keywords", 
            self.worker_id, len(batch.keywords),
        )

        fingerprint: BrowserFingerprint = generate_fingerprint("firefox")
        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)

        camoufox_instance: AsyncCamoufox | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None

        try:
            camoufox_instance, browser = await self._launch_camoufox(
                headless=settings.BROWSER_HEADLESS_DEFAULT,
                camoufox_os=camoufox_os,
            )
            self._current_headless = settings.BROWSER_HEADLESS_DEFAULT

            base_automator = GoogleCSEAutomator(
                cse_id=self.config.engines[0].engine_id,
                platform=self.config.engines[0].platform,
                config=self._cfg,
            )

            context, page = await self._create_context_and_page(
                browser=browser,
                automator=base_automator,
                fingerprint=fingerprint,
            )

            processed_count = 0

            for keyword in batch.keywords:
                if not self._running:
                    break

                success, camoufox_instance, browser, context, page = await self._process_keyword_all_engines(
                    browser=browser,
                    keyword=keyword,
                    db=db,
                    camoufox_instance=camoufox_instance,
                    context=context,
                    page=page,
                )

                if success:
                    processed_count += 1
                    await db.keyword_repo.mark_scraped(keyword.id)

                await asyncio.sleep(self._cfg.jitter_wait(
                    *self._cfg.between_keywords_range
                ))

            return processed_count

        finally:
            logger.info("[Worker-%d] Cerrando browser...", self.worker_id)
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if camoufox_instance:
                try:
                    await camoufox_instance.__aexit__(None, None, None)
                except Exception:
                    pass

    async def run(self) -> None:
        """Loop principal del worker: stateless, infinito."""
        self._running = True
        logger.info("[Worker-%d] Iniciado", self.worker_id)

        while self._running:
            try:
                db = await self._db_connect()
                try:
                    processed = await self._run_batch(db)
                    if processed == 0:
                        await asyncio.sleep(settings.CYCLE_DELAY_SECONDS)
                    else:
                        await asyncio.sleep(1)
                finally:
                    await db.disconnect()
                    
            except Exception as exc:
                logger.error("[Worker-%d] Error en ciclo: %s", self.worker_id, exc)
                await asyncio.sleep(settings.CYCLE_DELAY_SECONDS)

        logger.info("[Worker-%d] Detenido", self.worker_id)

    def stop(self) -> None:
        self._running = False


class WorkerPool:
    """Pool de workers concurrentes. Todos procesan el mismo pool de keywords."""

    def __init__(self) -> None:
        self._workers: list[ScraperWorker] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Inicia todos los workers concurrentemente."""
        logger.info(
            "Iniciando pool de %d workers", MAX_CONCURRENT_WORKERS,
        )

        for worker_id in range(1, MAX_CONCURRENT_WORKERS + 1):
            worker = ScraperWorker(worker_id=worker_id)
            self._workers.append(worker)
            
            task = asyncio.create_task(
                worker.run(),
                name=f"worker-{worker_id}",
            )
            self._tasks.append(task)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        logger.info("Señal de parada recibida. Deteniendo workers...")
        for worker in self._workers:
            worker.stop()


async def main() -> None:
    await WorkerPool().start()


if __name__ == "__main__":
    from pathlib import Path

    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

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