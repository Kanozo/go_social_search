"""
run_scraper.py
Orquestador principal del scraper con ejecución concurrente por FILTER.
Modo headless por defecto + navegador visible automático ante CAPTCHA.

Correcciones aplicadas en esta versión
──────────────────────────────────────
  BUG FIX  _switch_browser_visibility  → tras abrir el browser visible, navega
           explícitamente a la URL del CAPTCHA (captcha_url). Sin esto la página
           quedaba en about:blank porque el browser headless anterior (que tenía
           la URL cargada) ya se había cerrado.

  BUG FIX  _wait_for_captcha_resolution → con múltiples hilos, input() en
           run_in_executor compite por la misma terminal. Ahora se usa un
           asyncio.Event por label serializado con un Lock global. Solo un hilo
           a la vez puede pedir input; los demás esperan su timeout automático
           sin bloquear ni colisionar.

  IMPROVE  _switch_browser_visibility  → acepta captcha_url: str para navegar
           a la página correcta tras abrir el browser visible.

  IMPROVE  _run_engine_keywords        → captura la URL activa de la página en
           el momento del CaptchaError y la pasa a _switch_browser_visibility.

  IMPROVE  _wait_for_captcha_resolution → si otro hilo ya tiene el turno de
           input, espera timeout silenciosamente (sin bloquear la consola) y
           continúa. Esto evita que 2+ hilos simultáneos rompan la terminal.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from typing import Final

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from anti_detection import BrowserFingerprint, generate_fingerprint
from config.settings import settings
from database import KeywordRepository, PostRepository, SQLiteManager
from google_cse_automator import BrowserConfig, GoogleCSEAutomator
from utils.captcha_guard import CaptchaError
from utils.session_store import SessionStore

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# FILTERs a ejecutar concurrentemente
# ─────────────────────────────────────────────────────────────────────────────
FILTERS: list[str] | None = [
    "KW FB",
    "KW IG",
    "FB CR General Kano",
    "Kano Cluster CR"
]
MAX_CONCURRENT_BROWSERS: Final[int] = getattr(settings, "MAX_CONCURRENT_BROWSERS", 4)

# ─────────────────────────────────────────────────────────────────────────────
# Serialización de input() para entorno multi-hilo
# ─────────────────────────────────────────────────────────────────────────────
# Con múltiples filtros corriendo en paralelo, dos hilos pueden detectar CAPTCHA
# al mismo tiempo y ambos intentar llamar input() → la terminal se corrompe.
# Este Lock garantiza que solo UN hilo a la vez puede pedir ENTER al usuario.
# El resto espera su propio timeout automático sin tocar la consola.
_captcha_input_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Fallback engines
# ─────────────────────────────────────────────────────────────────────────────
_FALLBACK_ENGINES: list[dict] = [
    {
        "label":     "IG-KW-Engine",
        "engine_id": "c4b97eed1414fcb14",
        "platform":  "instagram",
        "keywords": [
            "#Cuba", "Cuba", "#CubaVive",
            "#YoSigoAMiPresidente", "#CubaPorLaSalud",
            "#TumbaElBloqueo", "#NoMasBloqueo", "#CubaNoEstaSola",
            "#FidelPorSiempre", "#CubaCoopera", "#CubaPorLaVida",
            "#CubaEstaFirme", "#CubaSoberana",
            "cubanos", "habana", "havana",
        ],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# ScraperOrchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ScraperOrchestrator:
    """
    Orquestador que gestiona browser, contextos, SQLite y el ciclo de keywords.
    Soporta cambio dinámico de visibilidad del browser ante CAPTCHAs.
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
        self._db:        SQLiteManager     | None = None
        self._kw_repo:   KeywordRepository | None = None
        self._post_repo: PostRepository   | None = None
        self._current_headless: bool = getattr(settings, "BROWSER_HEADLESS_DEFAULT", True)

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _db_connect(self) -> None:
        db_path = settings.SESSION_DIR.parent / "url_scraper.db"
        self._db       = SQLiteManager(db_path)
        await self._db.connect()
        self._kw_repo   = KeywordRepository(self._db)
        self._post_repo = PostRepository(self._db)
        kw_count   = await self._kw_repo.count()
        post_count = await self._post_repo.count()
        logger.info(
            "SQLite conectado | path=%s | keywords=%d | posts=%d",
            db_path, kw_count, post_count,
        )

    async def _db_disconnect(self) -> None:
        if self._db:
            await self._db.disconnect()
            self._db = None
            logger.info("SQLite desconectado.")

    # ── Engines config ────────────────────────────────────────────────────────

    async def _fetch_engines_config(self) -> list[dict]:
        if self._kw_repo is None:
            logger.warning("_fetch_engines_config: kw_repo no inicializado. Usando fallback.")
            return _FALLBACK_ENGINES

        groups = await self._kw_repo.get_engine_groups()
        if not groups:
            logger.warning(
                "Tabla 'keywords' vacía. Usando configuración de fallback. "
                "Carga keywords con: python manage_db.py seed"
            )
            return _FALLBACK_ENGINES

        if self._filter_label:
            groups = [g for g in groups if g.get("label") == self._filter_label]
            if not groups:
                logger.warning(
                    "No se encontró engine con label='%s'. Verifica configuración.",
                    self._filter_label,
                )
                return []

        logger.info(
            "[CONFIG] %d grupos de engine cargados desde SQLite%s.",
            len(groups),
            f" (filtro: '{self._filter_label}')" if self._filter_label else "",
        )
        return groups

    # ── Browser management ────────────────────────────────────────────────────

    async def _launch_browser(self, playwright, headless: bool, label: str) -> Browser:
        base_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
        launch_args = base_args if headless else [*base_args, "--start-maximized"]
        browser = (
            await playwright.firefox.launch(headless=headless, args=launch_args)
            if settings.BROWSER_TYPE == "firefox"
            else await playwright.chromium.launch(headless=headless, args=launch_args)
        )
        mode_str = "HEADLESS" if headless else "VISIBLE"
        logger.info("[%s] Browser '%s' iniciado en modo %s.", label, settings.BROWSER_TYPE, mode_str)
        return browser

    async def _save_context_state(self, context: BrowserContext, domain: str = "google.com") -> dict | None:
        if not settings.SESSION_PERSIST or not context:
            return None
        try:
            state = await context.storage_state()
            logger.debug("[%s] storage_state extraído para '%s'.", self._filter_label or "MAIN", domain)
            return state
        except Exception as exc:
            logger.warning("[%s] No se pudo extraer storage_state: %s", self._filter_label or "MAIN", exc)
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
            logger.debug("Restaurando sesión desde storage_state explícito.")
        elif settings.SESSION_PERSIST and session_domain:
            saved_state = self._session_store.load_state_dict(session_domain)
            if saved_state:
                context_options["storage_state"] = saved_state
                logger.debug("Sesión persistida cargada desde disco para '%s'.", session_domain)

        context: BrowserContext = await browser.new_context(**context_options)
        await automator.block_images_async(
            context, url_pattern="https://encrypted-tbn0.gstatic.com/images"
        )
        page: Page = await context.new_page()
        await automator.setup_page(page, fingerprint)

        if not skip_warmup:
            await automator._warmup_session(page)

        logger.info(
            "Contexto listo | OS=%s | UA=%s… | session=%s | warmup=%s",
            fingerprint.navigator_platform,
            fingerprint.user_agent[:40],
            "restaurada" if context_options.get("storage_state") else "nueva",
            "omitido" if skip_warmup else "OK",
        )
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
        try:
            await old_context.close()
            logger.debug("[%s] Contexto bloqueado cerrado.", label)
        except Exception as exc:
            logger.debug("[%s] Error cerrando contexto: %s", label, exc)

        new_fp = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Nueva identidad | OS=%s | UA=%s…",
            label, new_fp.navigator_platform, new_fp.user_agent[:50],
        )
        new_context, new_page = await self._create_context_and_page(
            browser=browser,
            automator=automator,
            fingerprint=new_fp,
            session_domain="",
            storage_state=storage_state,
        )
        return new_context, new_page, new_fp

    async def _switch_browser_visibility(
        self,
        playwright,
        old_browser: Browser,
        old_context: BrowserContext,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        label: str,
        new_headless: bool,
        captcha_url: str = "",
    ) -> tuple[Browser, BrowserContext, Page]:
        """
        Cambia la visibilidad del browser preservando fingerprint y sesión.

        BUG FIX: cuando new_headless=False (pasamos a visible para resolver
        el CAPTCHA), la página nueva queda en about:blank porque el browser
        headless que tenía la URL ya se cerró. Ahora se navega explícitamente
        a captcha_url para que el usuario vea el CAPTCHA real en pantalla.

        Args:
            captcha_url: URL donde estaba el CAPTCHA cuando se detectó el error.
                         Si está vacía se intenta restaurar desde el storage_state
                         pero la página queda donde esté la sesión restaurada.
        """
        from_mode = "HEADLESS" if not new_headless else "VISIBLE"
        to_mode   = "VISIBLE"  if not new_headless else "HEADLESS"
        logger.info("[%s] Cambiando visibilidad del browser: %s → %s", label, from_mode, to_mode)

        # 1. Guardar estado de sesión antes de cerrar
        storage_state = await self._save_context_state(old_context)

        # 2. Cerrar recursos del browser antiguo
        for resource in (old_context, old_browser):
            try:
                await resource.close()
            except Exception:
                pass

        # 3. Lanzar nuevo browser con modo cambiado
        new_browser = await self._launch_browser(playwright, headless=new_headless, label=label)

        # 4. Crear contexto con mismo fingerprint + sesión restaurada.
        #    skip_warmup=True: no navegar a google.com antes de cargar el CAPTCHA.
        new_context, new_page = await self._create_context_and_page(
            browser=new_browser,
            automator=automator,
            fingerprint=fingerprint,
            session_domain="google.com",
            storage_state=storage_state,
            skip_warmup=True,
        )

        # 5. BUG FIX: navegar a la URL del CAPTCHA en el nuevo browser visible.
        #    El browser headless anterior tenía esa página cargada; al cerrarlo
        #    se pierde. Sin esta navegación el usuario ve about:blank.
        if not new_headless and captcha_url:
            try:
                logger.info("[%s] Navegando a URL del CAPTCHA: %s", label, captcha_url)
                await new_page.goto(
                    captcha_url,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
            except Exception as nav_exc:
                logger.warning(
                    "[%s] No se pudo navegar a '%s': %s. El usuario verá la sesión restaurada.",
                    label, captcha_url, nav_exc,
                )

        # 6. Inyectar banner de notificación
        if not new_headless:
            try:
                await new_page.evaluate("""
                    () => {
                        const banner = document.createElement('div');
                        banner.style.cssText = `
                            position: fixed; top: 0; left: 0; right: 0;
                            background: #e53935; color: white; padding: 14px 12px;
                            text-align: center; font-weight: bold; z-index: 99999;
                            font-family: system-ui, sans-serif;
                            box-shadow: 0 2px 8px rgba(0,0,0,0.3); font-size: 15px;
                        `;
                        banner.textContent =
                            '⚠ CAPTCHA DETECTADO — Resuélvelo aquí y presiona ENTER en la consola para continuar';
                        document.body
                            ? document.body.prepend(banner)
                            : document.documentElement.prepend(banner);
                    }
                """)
            except Exception as exc:
                logger.debug("[%s] No se pudo inyectar banner: %s", label, exc)

            logger.info(
                "[%s] ══ NAVEGADOR VISIBLE ══ Resuelve el CAPTCHA y presiona ENTER en la consola.",
                label,
            )

        return new_browser, new_context, new_page

    async def _wait_for_captcha_resolution(self, label: str) -> None:
        """
        Espera a que el usuario resuelva el CAPTCHA manualmente.

        BUG FIX (multi-hilo): con 2+ filtros corriendo en paralelo, varios
        hilos pueden detectar CAPTCHA al mismo tiempo. Si todos llaman
        input() a la vez la terminal se corrompe (texto superpuesto, ENTER
        de un hilo desbloquea otro, etc.).

        Solución: _captcha_input_lock es un asyncio.Lock() global.
          - El primer hilo en adquirirlo pide ENTER al usuario normalmente.
          - Los demás hilos NO esperan el lock bloqueados; comprueban si
            pueden adquirirlo en tiempo 0 (acquire con timeout=0).
          - Si no pueden, esperan su timeout silenciosamente sin tocar
            la consola y continúan solos cuando expira.

        Esto garantiza que nunca hay dos input() activos al mismo tiempo.
        """
        timeout: int = getattr(settings, "CAPTCHA_MANUAL_TIMEOUT", 300)
        loop = asyncio.get_running_loop()

        # Intentar adquirir el lock de input sin bloquear
        got_input_lock = await asyncio.wait_for(
            asyncio.shield(_captcha_input_lock.acquire()),
            timeout=0.1,       # si en 100ms no está libre, vamos a modo silencioso
        ) if not _captcha_input_lock.locked() else False

        if got_input_lock:
            # Este hilo tiene el turno → puede pedir ENTER al usuario
            try:
                if timeout > 0:
                    logger.info(
                        "[%s] ══ CAPTCHA ══ Tienes %ds para resolverlo. "
                        "Presiona ENTER en esta consola cuando termines.",
                        label, timeout,
                    )
                    timed_out = False

                    async def _wait_input() -> None:
                        await loop.run_in_executor(
                            None, input,
                            f"[{label}] Presiona ENTER cuando hayas resuelto el CAPTCHA: ",
                        )

                    async def _wait_timeout() -> None:
                        nonlocal timed_out
                        await asyncio.sleep(timeout)
                        timed_out = True

                    input_task   = asyncio.create_task(_wait_input())
                    timeout_task = asyncio.create_task(_wait_timeout())

                    try:
                        _, pending = await asyncio.wait(
                            [input_task, timeout_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                    except Exception as exc:
                        logger.error("[%s] Error esperando input: %s", label, exc)

                    if timed_out:
                        logger.warning(
                            "[%s] Timeout (%ds) sin respuesta del usuario. Continuando…",
                            label, timeout,
                        )
                        GoogleCSEAutomator._play_alert_sound()
                else:
                    # timeout=0 → espera indefinida
                    logger.info(
                        "[%s] Esperando indefinidamente. Presiona ENTER para continuar.", label
                    )
                    await loop.run_in_executor(None, input, f"[{label}] ENTER para continuar: ")
            finally:
                _captcha_input_lock.release()

        else:
            # Otro hilo ya tiene el turno de input → espera silenciosa con timeout
            wait_secs = timeout if timeout > 0 else 300
            logger.warning(
                "[%s] Otro filtro ya está esperando input del usuario. "
                "Esperando %ds automáticamente antes de continuar…",
                label, wait_secs,
            )
            await asyncio.sleep(wait_secs)
            logger.info("[%s] Tiempo de espera automático agotado. Continuando…", label)

        logger.info("[%s] Continuando con el scraping…", label)

    # ── Engine loop ───────────────────────────────────────────────────────────

    async def _run_engine_keywords(
        self,
        engine_id: str,
        label: str,
        platform: str,
        keywords: list[str],
        total_pages: int = 3,
    ) -> None:
        """
        Procesa todas las keywords de un engine con soporte para cambio de
        visibilidad ante CAPTCHA.

        IMPROVE: captura page.url en el momento del CaptchaError y lo pasa
        a _switch_browser_visibility como captcha_url para que el browser
        visible navegue a la URL correcta.
        """
        semaphore_acquired = False

        automator = GoogleCSEAutomator(
            cse_id=engine_id,
            platform=platform,
            post_repo=self._post_repo,
            config=self._cfg,
            browser_type=settings.BROWSER_TYPE,
        )

        self._current_headless = getattr(settings, "BROWSER_HEADLESS_DEFAULT", True)
        fingerprint: BrowserFingerprint = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Fingerprint inicial | OS=%s | UA=%s… | Headless=%s",
            label, fingerprint.navigator_platform, fingerprint.user_agent[:50], self._current_headless,
        )

        browser: Browser | None = None
        context: BrowserContext | None = None
        page:    Page | None = None

        try:
            if self._browser_semaphore:
                await self._browser_semaphore.acquire()
                semaphore_acquired = True
                logger.debug("[%s] Semaphore adquirido.", label)

            async with async_playwright() as playwright:
                browser = await self._launch_browser(playwright, self._current_headless, label)
                context, page = await self._create_context_and_page(
                    browser=browser, automator=automator, fingerprint=fingerprint
                )

                for idx, raw_kw in enumerate(keywords, 1):
                    if not self._running:
                        logger.info("[%s] Stop signal. Saliendo.", label)
                        break

                    kw = raw_kw.strip()
                    if not kw:
                        continue

                    logger.info("[%s] [%d/%d] keyword='%s'", label, idx, len(keywords), kw)

                    try:
                        await automator.run_keyword(page, kw, total_pages)
                        if self._kw_repo:
                            await self._kw_repo.mark_scraped(kw)

                    except CaptchaError as captcha_exc:
                        logger.warning(
                            "[%s] CAPTCHA irresuelto (signal=%s) en '%s'.",
                            label, captcha_exc.signal, kw,
                        )
                        GoogleCSEAutomator._play_alert_sound()

                        # IMPROVE: capturar URL actual ANTES de cerrar el browser headless
                        # para navegar a ella en el nuevo browser visible.
                        captcha_url: str = ""
                        try:
                            captcha_url = page.url
                            logger.debug("[%s] URL del CAPTCHA capturada: %s", label, captcha_url)
                        except Exception:
                            pass

                        if getattr(settings, "BROWSER_VISIBLE_ON_CAPTCHA", True) and self._current_headless:
                            browser, context, page = await self._switch_browser_visibility(
                                playwright=playwright,
                                old_browser=browser,
                                old_context=context,
                                automator=automator,
                                fingerprint=fingerprint,
                                label=label,
                                new_headless=False,
                                captcha_url=captcha_url,   # ← URL del CAPTCHA
                            )
                            self._current_headless = False

                            await self._wait_for_captcha_resolution(label)

                            logger.info("[%s] Reintentando '%s' con navegador visible…", label, kw)
                            try:
                                await automator.run_keyword(page, kw, total_pages)
                                if self._kw_repo:
                                    await self._kw_repo.mark_scraped(kw)
                            except Exception as retry_exc:
                                logger.error(
                                    "[%s] Fallo en reintento post-CAPTCHA '%s': %s",
                                    label, kw, retry_exc, exc_info=True,
                                )

                            if getattr(settings, "HEADLESS_AFTER_CAPTCHA", False):
                                browser, context, page = await self._switch_browser_visibility(
                                    playwright=playwright,
                                    old_browser=browser,
                                    old_context=context,
                                    automator=automator,
                                    fingerprint=fingerprint,
                                    label=label,
                                    new_headless=True,
                                )
                                self._current_headless = True
                                logger.info("[%s] Volviendo a modo headless.", label)

                        else:
                            logger.info("[%s] Rotando identidad (sin cambio de visibilidad)…", label)
                            context, page, fingerprint = await self._rotate_identity(
                                browser=browser,
                                automator=automator,
                                old_context=context,
                                label=label,
                            )
                            logger.info("[%s] Reintentando '%s'…", label, kw)
                            try:
                                await automator.run_keyword(page, kw, total_pages)
                                if self._kw_repo:
                                    await self._kw_repo.mark_scraped(kw)
                            except Exception as retry_exc:
                                logger.error(
                                    "[%s] Fallo en reintento post-rotación '%s': %s",
                                    label, kw, retry_exc, exc_info=True,
                                )

                    except Exception as generic_exc:
                        logger.error(
                            "[%s] Error inesperado en '%s': %s",
                            label, kw, generic_exc, exc_info=True,
                        )
                        try:
                            await page.close()
                            page = await context.new_page()
                            await automator.setup_page(page, fingerprint)
                            logger.info("[%s] Página recreada. Continuando…", label)
                        except Exception as recovery_exc:
                            logger.error(
                                "[%s] Recuperación fallida. Abortando engine: %s",
                                label, recovery_exc,
                            )
                            break

                    pause = self._cfg.jitter_wait(*self._cfg.between_keywords_range)
                    logger.debug("[%s] Pausa entre keywords: %.1fs", label, pause)
                    await asyncio.sleep(pause)

        finally:
            if semaphore_acquired and self._browser_semaphore:
                self._browser_semaphore.release()
                logger.debug("[%s] Semaphore liberado.", label)

            if settings.SESSION_PERSIST and context:
                try:
                    if await self._session_store.save(context, "google.com"):
                        logger.info("[%s] Sesión guardada en disco.", label)
                except Exception as save_exc:
                    logger.warning("[%s] No se pudo guardar sesión: %s", label, save_exc)

            for resource in filter(None, [page, context, browser]):
                try:
                    await resource.close()
                except Exception:
                    pass

            mode_str = "HEADLESS" if self._current_headless else "VISIBLE"
            logger.info("[%s] Browser cerrado (modo: %s).", label, mode_str)

    # ── Ciclo principal ───────────────────────────────────────────────────────

    async def _execute_cycle(self) -> None:
        engines: list[dict] = await self._fetch_engines_config()
        if not engines:
            logger.warning(
                "Sin engines configurados para filtro '%s'. Saltando ciclo.",
                self._filter_label or "TODOS",
            )
            return

        for engine in engines:
            if not self._running:
                break

            engine_id: str | None = engine.get("engine_id")
            keywords:  list[str]  = engine.get("keywords", [])
            label:     str        = engine.get("label", engine_id or "?")
            platform:  str        = engine.get("platform", "")

            if self._filter_label and label != self._filter_label:
                continue

            valid_kws = [k for k in keywords if isinstance(k, str) and k.strip()]

            if not engine_id or not valid_kws:
                logger.warning(
                    "Config inválida engine='%s' (engine_id=%s, keywords=%d). Omitiendo.",
                    label, engine_id, len(valid_kws),
                )
                continue

            logger.info(
                "── Engine '%s' | %d keywords | platform='%s' | engine_id=%s",
                label, len(valid_kws), platform, engine_id,
            )
            try:
                await self._run_engine_keywords(
                    engine_id=engine_id,
                    label=label,
                    platform=platform,
                    keywords=valid_kws,
                    total_pages=settings.TOTAL_PAGES_PER_KEYWORD,
                )
            except Exception as critical_exc:
                logger.error(
                    "Fallo crítico en engine '%s': %s",
                    label, critical_exc, exc_info=True,
                )

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        await self._db_connect()
        logger.info(
            "Orquestador iniciado | FILTER='%s' | HEADLESS_DEFAULT=%s | OUTPUT_MODE=%s",
            self._filter_label or "TODOS",
            getattr(settings, "BROWSER_HEADLESS_DEFAULT", True),
            settings.OUTPUT_MODE,
        )

        try:
            while self._running:
                logger.info("═" * 60)
                logger.info("Iniciando ciclo de scraping para '%s'…", self._filter_label or "TODOS")
                await self._execute_cycle()

                if not self._running:
                    break

                delay = settings.CYCLE_DELAY_SECONDS
                logger.info("Ciclo completado. Próximo en %ds.", delay)
                await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("Bucle cancelado por señal para '%s'.", self._filter_label or "TODOS")
        finally:
            await self._db_disconnect()

        logger.info("Orquestador '%s' detenido.", self._filter_label or "TODOS")

    def stop(self) -> None:
        logger.info(
            "Señal de parada recibida para '%s'. Finalizando ciclo actual…",
            self._filter_label or "TODOS",
        )
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# ConcurrentFilterManager
# ─────────────────────────────────────────────────────────────────────────────

class ConcurrentFilterManager:
    """Gestiona la ejecución concurrente de múltiples FILTERs."""

    def __init__(self, filters: list[str] | None = None) -> None:
        self._filters            = filters
        self._tasks:         list[asyncio.Task]          = []
        self._orchestrators: list[ScraperOrchestrator]  = []
        self._browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

    async def _run_filter_task(self, orchestrator: ScraperOrchestrator, filter_label: str | None) -> None:
        try:
            await orchestrator.start()
        except Exception as exc:
            logger.error(
                "Error crítico en tarea para filter='%s': %s",
                filter_label or "TODOS", exc, exc_info=True,
            )

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def signal_handler() -> None:
            logger.info("Señal recibida. Deteniendo todas las tareas…")
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        filters_to_run = self._filters if self._filters else [None]

        logger.info(
            "ConcurrentFilterManager iniciado | Filtros: %s | Max browsers: %d | Headless: %s",
            [f or "TODOS" for f in filters_to_run],
            MAX_CONCURRENT_BROWSERS,
            getattr(settings, "BROWSER_HEADLESS_DEFAULT", True),
        )

        self._orchestrators = [
            ScraperOrchestrator(filter_label=flt, browser_semaphore=self._browser_semaphore)
            for flt in filters_to_run
        ]
        self._tasks = [
            asyncio.create_task(
                self._run_filter_task(orch, flt),
                name=f"filter-{flt or 'all'}",
            )
            for orch, flt in zip(self._orchestrators, filters_to_run)
        ]

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("Gather cancelado. Esperando cleanup de tareas…")
        finally:
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("ConcurrentFilterManager finalizado.")

    def stop(self) -> None:
        for orchestrator in self._orchestrators:
            orchestrator.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    await ConcurrentFilterManager(filters=FILTERS).start()


if __name__ == "__main__":
    import os
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
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    logger.info(
        "Python %s | PID %d | Filtros: %s | Headless: %s | Visible on CAPTCHA: %s",
        sys.version.split()[0],
        os.getpid(),
        f"{len(FILTERS)} activos" if FILTERS else "TODOS",
        getattr(settings, "BROWSER_HEADLESS_DEFAULT", True),
        getattr(settings, "BROWSER_VISIBLE_ON_CAPTCHA", True),
    )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrumpido manualmente.")
        sys.exit(0)