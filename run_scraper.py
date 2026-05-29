"""
run_scraper.py
Orquestador principal: gestiona browser, contextos, SQLite y el ciclo de keywords.
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
from google_cse_automator import BrowserConfig, GoogleCSEAutomator
from utils.captcha_guard import CaptchaError
from utils.session_store import SessionStore

logger = logging.getLogger(__name__)
FILTER = "FB CR General Kano"
FILTER = "KW IG"

# ─────────────────────────────────────────────────────────────────────────────
# Keywords de fallback (solo se usan si la tabla keywords está vacía)
# ─────────────────────────────────────────────────────────────────────────────

_FALLBACK_ENGINE_CONFIG: list[dict] = [
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
    Orquestador que gestiona el ciclo de vida del browser, contextos y keywords.

    Attributes:
        _running:       Flag de control del bucle principal.
        _cfg:           Parámetros de timing del browser.
        _session_store: Persistencia de cookies/localStorage en disco.
        _db:            Gestor SQLite (una conexión compartida).
        _kw_repo:       Repositorio de keywords (lectura de config + mark_scraped).
        _post_repo:     Repositorio de posts (escritura de resultados).
    """

    def __init__(self) -> None:
        self._running = False
        self._cfg = BrowserConfig()
        self._session_store = SessionStore(settings.SESSION_DIR)
        # La DB se inicializa en connect() dentro de start()
        self._db: SQLiteManager | None = None
        self._kw_repo: KeywordRepository | None = None
        self._post_repo: PostRepository | None = None

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _db_connect(self) -> None:
        """Abre la conexión SQLite y crea los repositorios."""
        db_path = settings.SESSION_DIR.parent / "url_scraper.db"
        self._db = SQLiteManager(db_path)
        await self._db.connect()
        self._kw_repo = KeywordRepository(self._db)
        self._post_repo = PostRepository(self._db)
        kw_count  = await self._kw_repo.count()
        post_count = await self._post_repo.count()
        logger.info(
            "SQLite listo | keywords=%d | posts=%d", kw_count, post_count
        )

    async def _db_disconnect(self) -> None:
        """Cierra la conexión SQLite limpiamente."""
        if self._db:
            await self._db.disconnect()
            self._db = None

    # ── Configuración de engines ─────────────────────────────────────────────

    async def _fetch_engines_config(self) -> list[dict]:
        """
        Devuelve la configuración de engines y keywords.

        Fuente de datos: tabla ``keywords`` de SQLite, agrupada por
        ``(engine_id, label)``. Si la tabla está vacía, carga el
        fallback hardcodeado para el primer arranque.

        Returns:
            Lista de dicts con ``engine_id``, ``label``, ``platform``
            y ``keywords`` lista de strings.
        """
        if self._kw_repo is None:
            logger.warning("_fetch_engines_config: kw_repo no inicializado.")
            return _FALLBACK_ENGINE_CONFIG

        groups = await self._kw_repo.get_engine_groups()

        if not groups:
            logger.info(
                "Tabla keywords vacía. Usando configuración de fallback hardcodeada."
            )
            return _FALLBACK_ENGINE_CONFIG

        logger.info(
            "[CONFIG] %d grupos de engine cargados desde SQLite.", len(groups)
        )
        return groups

    # ── Context helpers ───────────────────────────────────────────────────────

    async def _create_context_and_page(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        session_domain: str = "google.com",
    ) -> tuple[BrowserContext, Page]:
        context_options = fingerprint.build_context_options()

        if settings.SESSION_PERSIST and session_domain and self._session_store:
            saved_state = self._session_store.load_state_dict(session_domain)
            if saved_state:
                context_options["storage_state"] = saved_state

        context: BrowserContext = await browser.new_context(**context_options)
        
        # ✅ Bloquear imágenes a nivel de contexto (ANTES de crear la página)
        await automator.block_images_async(context, url_pattern="https://encrypted-tbn0.gstatic.com/images")
        
        page: Page = await context.new_page()
        await automator.setup_page(page, fingerprint)
        await automator._warmup_session(page)

        logger.info("Contexto listo: OS=%s | UA=%s…", fingerprint.navigator_platform, fingerprint.user_agent[:40])
        return context, page

    async def _rotate_identity(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        old_context: BrowserContext,
        label: str,
    ) -> tuple[BrowserContext, Page]:
        """
        Rota identidad ante CAPTCHA: cierra contexto bloqueado y abre uno limpio
        con un fingerprint completamente diferente.

        Args:
            browser:     Browser activo (se reutiliza).
            automator:   Automator del engine.
            old_context: Contexto bloqueado (se cierra aquí).
            label:       Etiqueta del engine para logging.

        Returns:
            Tupla ``(nuevo_context, nueva_page)``.
        """
        try:
            await old_context.close()
            logger.debug("[%s] Contexto bloqueado cerrado.", label)
        except Exception as exc:
            logger.debug("[%s] Error cerrando contexto: %s", label, exc)

        new_fp = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Nueva identidad: %s | %s",
            label, new_fp.navigator_platform, new_fp.user_agent[:50],
        )
        # session_domain="" → no cargar sesión contaminada
        return await self._create_context_and_page(
            browser=browser,
            automator=automator,
            fingerprint=new_fp,
            session_domain="",
        )

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
        Procesa todas las keywords de un engine con un único browser.

        Después de procesar cada keyword exitosamente, llama
        ``kw_repo.mark_scraped(keyword)`` para actualizar ``last_scrap`` en DB.

        Args:
            engine_id:   ID del Google CSE.
            label:       Etiqueta descriptiva del engine.
            platform:    Plataforma objetivo ("instagram", "facebook", …).
            keywords:    Lista de términos a buscar.
            total_pages: Páginas de resultados por keyword.
        """
        # El PostRepository se pasa al automator para que persista sin re-conectar
        automator = GoogleCSEAutomator(
            cse_id=engine_id,
            platform=platform,
            post_repo=self._post_repo,
            config=self._cfg,
            browser_type=settings.BROWSER_TYPE,
        )

        fingerprint: BrowserFingerprint = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Fingerprint inicial: %s | %s",
            label, fingerprint.navigator_platform, fingerprint.user_agent[:50],
        )

        async with async_playwright() as playwright:
            # Lanzar browser
            launch_opts = {"headless": settings.BROWSER_HEADLESS}
            browser: Browser = (
                await playwright.firefox.launch(**launch_opts)
                if settings.BROWSER_TYPE == "firefox"
                else await playwright.chromium.launch(**launch_opts)
            )
            logger.info("[%s] Browser '%s' iniciado.", label, settings.BROWSER_TYPE)

            context, page = await self._create_context_and_page(
                browser=browser, automator=automator, fingerprint=fingerprint
            )

            try:
                for idx, raw_kw in enumerate(keywords, 1):
                    if not self._running:
                        logger.info("[%s] Stop signal. Saliendo del loop.", label)
                        break

                    kw = raw_kw.strip()
                    if not kw:
                        continue

                    logger.info(
                        "[%s] [%d/%d] keyword='%s'", label, idx, len(keywords), kw
                    )

                    try:
                        await automator.run_keyword(page, kw, total_pages)
                        # ── Actualizar last_scrap en DB ───────────────────────
                        if self._kw_repo:
                            await self._kw_repo.mark_scraped(kw)

                    except CaptchaError as captcha_exc:
                        # CAPTCHA no resoluble → rotar identidad
                        logger.warning(
                            "[%s] CAPTCHA no resoluble (signal=%s) en '%s'. "
                            "Rotando identidad...",
                            label, captcha_exc.signal, kw,
                        )
                        GoogleCSEAutomator._play_alert_sound()

                        try:
                            context, page = await self._rotate_identity(
                                browser=browser,
                                automator=automator,
                                old_context=context,
                                label=label,
                            )
                            fingerprint = generate_fingerprint(settings.BROWSER_TYPE)
                            await automator.setup_page(page, fingerprint)

                            logger.info("[%s] Reintentando '%s'...", label, kw)
                            await automator.run_keyword(page, kw, total_pages)
                            if self._kw_repo:
                                await self._kw_repo.mark_scraped(kw)

                        except Exception as rotate_exc:
                            logger.error(
                                "[%s] Fallo en reintento con nueva identidad '%s': %s",
                                label, kw, rotate_exc, exc_info=True,
                            )

                    except Exception as generic_exc:
                        # Error genérico: recuperación suave (nueva página, mismo contexto)
                        logger.error(
                            "[%s] Error en '%s': %s",
                            label, kw, generic_exc, exc_info=True,
                        )
                        try:
                            await page.close()
                            page = await context.new_page()
                            await automator.setup_page(page, fingerprint)
                            logger.info("[%s] Página recreada.", label)
                        except Exception as recovery_exc:
                            logger.error(
                                "[%s] Recuperación fallida. Abortando engine: %s",
                                label, recovery_exc,
                            )
                            break

                    # Pausa entre keywords
                    pause = self._cfg.jitter_wait(*self._cfg.between_keywords_range)
                    logger.debug("[%s] Pausa entre keywords: %.1fs", label, pause)
                    await asyncio.sleep(pause)

            finally:
                # Guardar sesión si está habilitado
                if settings.SESSION_PERSIST and self._session_store:
                    try:
                        saved = await self._session_store.save(context, "google.com")
                        if saved:
                            logger.info("[%s] Sesión guardada en disco.", label)
                    except Exception as save_exc:
                        logger.warning(
                            "[%s] No se pudo guardar la sesión: %s", label, save_exc
                        )
                else:
                    logger.debug("[%s] SESSION_PERSIST=false. Sesión descartada.", label)

                try:
                    await context.close()
                except Exception:
                    pass
                await browser.close()
                logger.info("[%s] Browser cerrado.", label)

    # ── Ciclo principal ───────────────────────────────────────────────────────

    async def _execute_cycle(self) -> None:
        """Ejecuta un ciclo: carga engines desde DB y los procesa secuencialmente."""
        engines: list[dict] = await self._fetch_engines_config()
        if not engines:
            logger.warning("Sin motores configurados. Saltando ciclo.")
            return

        for engine in engines:
            if not self._running:
                break

            engine_id: str | None = engine.get("engine_id")
            keywords:  list[str]  = engine.get("keywords", [])
            label:     str        = engine.get("label", engine_id or "?")
            platform:  str        = engine.get("platform", "")

            if FILTER and label == FILTER:
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

    # ── Inicio y parada ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Inicia el bucle principal del orquestador.

        Abre la conexión SQLite al inicio y la cierra al terminar,
        independientemente de cómo salga el proceso.
        """
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        await self._db_connect()

        logger.info("Orquestador iniciado | OUTPUT_MODE=%s", settings.OUTPUT_MODE)
        try:
            while self._running:
                logger.info("═" * 60)
                logger.info("Iniciando nuevo ciclo de scraping...")
                await self._execute_cycle()

                if not self._running:
                    break

                delay = settings.CYCLE_DELAY_SECONDS
                logger.info("Ciclo completado. Próximo en %ds.", delay)
                await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("Bucle cancelado por señal.")
        finally:
            await self._db_disconnect()

        logger.info("Orquestador detenido.")

    def stop(self) -> None:
        """Señaliza al orquestador para detenerse limpiamente tras el ciclo actual."""
        logger.info("Señal de parada recibida. Finalizando ciclo actual...")
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Punto de entrada async del scraper."""
    await ScraperOrchestrator().start()


if __name__ == "__main__":
    import os
    from pathlib import Path

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "scraper.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    logger.info("Python %s | PID %d", sys.version.split()[0], os.getpid())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrumpido manualmente.")
        sys.exit(0)