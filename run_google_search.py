"""
run_google_search.py
Orquestador de scraping de Google Search directo.

El browser arranca siempre en modo VISIBLE (headless=False).
No hay lógica de cambio de visibilidad: la ventana está disponible
desde el inicio para que el usuario pueda resolver CAPTCHAs directamente.

Ciclo ante CAPTCHA
───────────────────
  1. CaptchaError se propaga desde GoogleSearchAutomator.
  2. El orquestador loguea un aviso y espera ENTER del usuario
     (con timeout configurable en CAPTCHA_MANUAL_TIMEOUT).
  3. Al recibir ENTER reintenta la keyword fallida.
  4. Con múltiples hilos activos, el Lock global de run_scraper.py
     garantiza que solo un hilo a la vez pide ENTER por consola.

Uso standalone
───────────────
    python run_google_search.py

Uso integrado
─────────────
    from run_google_search import GoogleSearchOrchestrator
    asyncio.run(GoogleSearchOrchestrator().start())
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from anti_detection import BrowserFingerprint, generate_fingerprint
from config.settings import settings
from database import KeywordRepository, PostRepository, SQLiteManager
from google_search_automator import GoogleSearchAutomator, SearchConfig
from google_search_url import SearchLanguage, SearchTab, TimeFilter
from utils.captcha_guard import CaptchaError
from utils.session_store import SessionStore

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Lock compartido con run_scraper.py para serializar input() multi-hilo
# ─────────────────────────────────────────────────────────────────────────────
# Si ambos orquestadores corren juntos (CSE + Google Search) y detectan
# CAPTCHA al mismo tiempo, este lock garantiza que solo uno pide ENTER.
# Se importa desde run_scraper para compartir la misma instancia.
try:
    from run_scraper import _captcha_input_lock
except ImportError:
    # Standalone: crear lock propio
    _captcha_input_lock = asyncio.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Configuración por defecto
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_SEARCH_CONFIG = SearchConfig(
    domains      = ["facebook.com", "instagram.com"],
    languages    = [SearchLanguage.SPANISH, SearchLanguage.ENGLISH],
    tabs         = [SearchTab.WEB, SearchTab.NEWS, SearchTab.VIDEOS, SearchTab.SHORTS],
    time_filter  = TimeFilter.DAY,
    country      = "cu",
    max_pages    = 3,
    num_results  = 10,
    browser_type = getattr(settings, "BROWSER_TYPE", "firefox"),
)

_FALLBACK_KEYWORDS: list[str] = [
    "Cuba", "#Cuba", "#CubaVive", "habana", "havana",
    "#NoMasBloqueo", "#CubaNoEstaSola",
]


# ─────────────────────────────────────────────────────────────────────────────
# Orquestador
# ─────────────────────────────────────────────────────────────────────────────

class GoogleSearchOrchestrator:
    """
    Orquestador de Google Search directo. Browser siempre visible.

    Gestiona: DB → browser visible → contexto → búsqueda → CAPTCHA manual
              → persistencia → DB disconnect → ciclo.
    """

    def __init__(
        self,
        config: SearchConfig | None = None,
        browser_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._config            = config or DEFAULT_SEARCH_CONFIG
        self._browser_semaphore = browser_semaphore
        self._session_store     = SessionStore(settings.SESSION_DIR)
        self._running           = False
        self._db:        SQLiteManager     | None = None
        self._kw_repo:   KeywordRepository | None = None
        self._post_repo: PostRepository   | None = None

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _db_connect(self) -> None:
        db_path = settings.SESSION_DIR.parent / "url_scraper.db"
        self._db       = SQLiteManager(db_path)
        await self._db.connect()
        self._kw_repo   = KeywordRepository(self._db)
        self._post_repo = PostRepository(self._db)
        logger.info("SQLite conectado | path=%s", db_path)

    async def _db_disconnect(self) -> None:
        if self._db:
            await self._db.disconnect()
            self._db = None

    # ── Keywords ──────────────────────────────────────────────────────────────

    async def _load_keywords(self) -> list[str]:
        if not self._kw_repo:
            return _FALLBACK_KEYWORDS
        try:
            keywords = await self._kw_repo.get_google_search_keywords()
        except AttributeError:
            try:
                keywords = await self._kw_repo.get_all()
            except Exception:
                keywords = []
        if not keywords:
            logger.warning("Sin keywords en DB. Usando fallback.")
            return _FALLBACK_KEYWORDS
        logger.info("%d keywords cargadas.", len(keywords))
        return keywords

    # ── Browser ───────────────────────────────────────────────────────────────

    async def _launch_browser(self, playwright) -> Browser:
        """
        Lanza el browser siempre en modo VISIBLE (headless=False).

        --start-maximized maximiza la ventana para que el usuario vea
        claramente los resultados y cualquier CAPTCHA que aparezca.
        """
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--start-maximized",
        ]
        browser = (
            await playwright.firefox.launch(headless=False, args=args)
            if self._config.browser_type == "firefox"
            else await playwright.chromium.launch(headless=False, args=args)
        )
        logger.info("Browser '%s' iniciado | modo=VISIBLE", self._config.browser_type)
        return browser

    async def _create_context(
        self,
        browser: Browser,
        automator: GoogleSearchAutomator,
        fingerprint: BrowserFingerprint,
        storage_state: dict | None = None,
    ) -> tuple[BrowserContext, Page]:
        opts = fingerprint.build_context_options()

        # Restaurar sesión: primero storage_state explícito, luego disco
        if storage_state:
            opts["storage_state"] = storage_state
        elif settings.SESSION_PERSIST:
            saved = self._session_store.load_state_dict("google.com")
            if saved:
                opts["storage_state"] = saved
                logger.debug("Sesión restaurada desde disco.")

        context: BrowserContext = await browser.new_context(**opts)
        page: Page = await context.new_page()
        await automator.setup_page(page, fingerprint)

        # Warmup: navegar a Google para establecer contexto de sesión
        try:
            await page.goto(
                "https://www.google.com",
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            await asyncio.sleep(2.0)
            logger.debug("Warmup completado.")
        except Exception as exc:
            logger.debug("Warmup falló (no crítico): %s", exc)

        return context, page

    # ── CAPTCHA manual ────────────────────────────────────────────────────────

    async def _wait_for_captcha(self) -> None:
        """
        Espera input manual del usuario tras detectar un CAPTCHA.

        El browser ya está visible; el usuario ve el CAPTCHA directamente.
        Usa el lock global para serializar el input() cuando hay múltiples
        hilos activos (evita colisión de prompts en la terminal).

        Si otro hilo ya tiene el turno de input, espera el timeout
        silenciosamente sin tocar la consola.
        """
        timeout: int = getattr(settings, "CAPTCHA_MANUAL_TIMEOUT", 300)
        loop = asyncio.get_running_loop()

        # Intentar adquirir el lock de forma no bloqueante
        got_lock = False
        if not _captcha_input_lock.locked():
            try:
                await asyncio.wait_for(
                    asyncio.shield(_captcha_input_lock.acquire()),
                    timeout=0.1,
                )
                got_lock = True
            except (asyncio.TimeoutError, Exception):
                pass

        if got_lock:
            try:
                if timeout > 0:
                    logger.info(
                        "══ CAPTCHA ══ Resuélvelo en la ventana del navegador. "
                        "Tienes %ds. Presiona ENTER aquí cuando termines.",
                        timeout,
                    )
                    timed_out = False

                    async def _wait_input() -> None:
                        await loop.run_in_executor(
                            None, input, "[GoogleSearch] ENTER cuando hayas resuelto el CAPTCHA: "
                        )

                    async def _wait_timeout() -> None:
                        nonlocal timed_out
                        await asyncio.sleep(timeout)
                        timed_out = True

                    input_task   = asyncio.create_task(_wait_input())
                    timeout_task = asyncio.create_task(_wait_timeout())
                    _, pending = await asyncio.wait(
                        [input_task, timeout_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass

                    if timed_out:
                        logger.warning("Timeout (%ds) sin respuesta. Continuando…", timeout)
                else:
                    logger.info("Esperando ENTER (sin timeout)…")
                    await loop.run_in_executor(
                        None, input, "[GoogleSearch] ENTER para continuar: "
                    )
            finally:
                _captcha_input_lock.release()
        else:
            wait = timeout if timeout > 0 else 300
            logger.warning(
                "Otro hilo gestiona el CAPTCHA. Esperando %ds automáticamente…", wait
            )
            await asyncio.sleep(wait)

        logger.info("Continuando tras CAPTCHA…")

    # ── Ciclo principal ───────────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        keywords  = await self._load_keywords()
        if not keywords:
            logger.warning("Sin keywords. Saltando ciclo.")
            return

        semaphore_acquired = False
        fingerprint = generate_fingerprint(self._config.browser_type)
        automator   = GoogleSearchAutomator(
            config=self._config,
            post_repo=self._post_repo,
        )

        browser: Browser | None = None
        context: BrowserContext | None = None
        page:    Page | None = None

        try:
            if self._browser_semaphore:
                await self._browser_semaphore.acquire()
                semaphore_acquired = True

            async with async_playwright() as playwright:
                browser = await self._launch_browser(playwright)
                context, page = await self._create_context(
                    browser=browser,
                    automator=automator,
                    fingerprint=fingerprint,
                )

                # Ejecutar búsqueda completa con manejo de CAPTCHA
                pending_keywords = list(keywords)
                while pending_keywords and self._running:
                    try:
                        await automator.run_search(page, fingerprint, pending_keywords)
                        pending_keywords = []   # Completado sin CAPTCHA irresuelto

                    except CaptchaError as captcha_exc:
                        logger.warning(
                            "CAPTCHA detectado (signal=%s). "
                            "Resuélvelo en la ventana del navegador.",
                            captcha_exc.signal,
                        )
                        # Esperar resolución manual — la ventana ya está visible
                        await self._wait_for_captcha()

                        # Identificar desde qué keyword reanudar
                        processed = automator._results  # resultados ya acumulados
                        # Reintentar: automator.run_search limpia _results en cada kw,
                        # así que basta con reinvocar con las keywords restantes.
                        # La keyword que falló estará al inicio de pending_keywords
                        # porque run_search la propagó antes de avanzar a la siguiente.
                        logger.info(
                            "Reintentando desde keyword actual con %d keywords pendientes…",
                            len(pending_keywords),
                        )
                        # pending_keywords no cambia: run_search propaga en la kw fallida

        finally:
            if semaphore_acquired and self._browser_semaphore:
                self._browser_semaphore.release()

            if settings.SESSION_PERSIST and context:
                try:
                    await self._session_store.save(context, "google.com")
                    logger.info("Sesión guardada.")
                except Exception as exc:
                    logger.warning("No se pudo guardar sesión: %s", exc)

            for resource in filter(None, [page, context, browser]):
                try:
                    await resource.close()
                except Exception:
                    pass

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        await self._db_connect()
        logger.info(
            "GoogleSearchOrchestrator iniciado | tabs=%s | langs=%s | "
            "time_filter=%s | browser=VISIBLE",
            [t.label for t in self._config.tabs],
            [la.code  for la in self._config.languages],
            self._config.time_filter.value,
        )

        try:
            while self._running:
                logger.info("═" * 60)
                await self._run_cycle()
                if not self._running:
                    break
                delay = settings.CYCLE_DELAY_SECONDS
                logger.info("Ciclo completado. Próximo en %ds.", delay)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            await self._db_disconnect()

        logger.info("GoogleSearchOrchestrator detenido.")

    def stop(self) -> None:
        logger.info("Stop signal recibido.")
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    await GoogleSearchOrchestrator().start()


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
    logging.getLogger("playwright").setLevel(logging.WARNING)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrumpido manualmente.")
        sys.exit(0)