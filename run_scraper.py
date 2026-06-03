"""
run_scraper.py
Orquestador principal del scraper.

Ciclo de vida de recursos
──────────────────────────
  SQLite DB   → abierta una vez en ``start()``, cerrada en el ``finally``.
  Browser     → uno por engine, creado y cerrado en ``_run_engine_keywords``.
  Contexto    → uno por sesión de engine; se recrea SOLO ante CAPTCHA irresuelto.
  Fingerprint → uno por contexto (coherente durante toda la sesión del engine).
  PostRepo    → la misma instancia se pasa a todos los automators (sin re-conectar).

_fetch_engines_config
──────────────────────
  Fuente primaria:  tabla ``keywords`` de SQLite (agrupada por engine_id + label).
  Fuente fallback:  lista hardcodeada ``_FALLBACK_ENGINES`` si la tabla está vacía.

Actualización de last_scrap
─────────────────────────────
  ``kw_repo.mark_scraped(keyword)`` se llama después de cada keyword procesada
  exitosamente, actualizando el campo ``last_scrap`` en la tabla ``keywords``.
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

FILTER = None


# FILTER = "Maritza"
# FILTER = "FB CR General Kano"
# FILTER = "Kano Cluster CR"

# FILTER = "KW IG"
# FILTER = "KW FB"


# ─────────────────────────────────────────────────────────────────────────────
# Configuración de fallback (primer arranque sin datos en DB)
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

    Attributes:
        _running:       Flag del bucle principal.
        _cfg:           Parámetros de timing del browser.
        _session_store: Persistencia de cookies/localStorage en disco.
        _db:            Gestor de SQLite (una conexión compartida por ciclo).
        _kw_repo:       CRUD de keywords (leer config + mark_scraped).
        _post_repo:     CRUD de posts (escritura de resultados scrapeados).
    """

    def __init__(self) -> None:
        self._running       = False
        self._cfg           = BrowserConfig()
        self._session_store = SessionStore(settings.SESSION_DIR)
        self._db:        SQLiteManager      | None = None
        self._kw_repo:   KeywordRepository  | None = None
        self._post_repo: PostRepository     | None = None

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _db_connect(self) -> None:
        """
        Abre la conexión SQLite, aplica pragmas WAL y crea los repositorios.

        La ruta del archivo es ``<BASE_DIR>/scraper.db``.
        """
        db_path = settings.SESSION_DIR.parent / "url_scraper.db"
        self._db = SQLiteManager(db_path)
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
        """Cierra la conexión SQLite con CHECKPOINT del WAL."""
        if self._db:
            await self._db.disconnect()
            self._db = None
            logger.info("SQLite desconectado.")

    # ── Configuración de engines ──────────────────────────────────────────────

    async def _fetch_engines_config(self) -> list[dict]:
        """
        Devuelve la configuración de engines y keywords.

        Fuente primaria: tabla ``keywords`` de SQLite, agrupada por
        ``(engine_id, label)`` mediante ``KeywordRepository.get_engine_groups()``.

        Si la tabla está vacía (primer arranque), devuelve ``_FALLBACK_ENGINES``
        y registra un aviso. El fallback permite operar sin datos en DB hasta
        que se carguen keywords mediante la CLI de administración.

        Returns:
            Lista de dicts con claves: engine_id, label, platform, keywords.
        """
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

        logger.info("[CONFIG] %d grupos de engine cargados desde SQLite.", len(groups))
        return groups

    # ── Context helpers ───────────────────────────────────────────────────────

    async def _create_context_and_page(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        session_domain: str = "google.com",
    ) -> tuple[BrowserContext, Page]:
        """
        Crea un contexto con fingerprint completo, sesión persistida y warmup.

        Capas aplicadas en orden:
          1. ``fingerprint.build_context_options()`` → UA, locale, timezone, viewport
          2. ``SessionStore.load_state_dict()``      → cookies + localStorage previos
          3. ``automator.setup_page()``              → 12 parches JS + interceptor HTTP
          4. ``automator._warmup_session()``         → cookies Google + historial

        Args:
            browser:        Browser activo.
            automator:      Automator del engine.
            fingerprint:    Fingerprint coherente para esta sesión.
            session_domain: Dominio para sesión en disco.
                            Cadena vacía → siempre contexto limpio.

        Returns:
            Tupla ``(context, page)`` lista para scraping.
        """
        context_options = fingerprint.build_context_options()

        # Cargar sesión persistida si está habilitada y el dominio es válido
        if settings.SESSION_PERSIST and session_domain:
            saved_state = self._session_store.load_state_dict(session_domain)
            if saved_state:
                context_options["storage_state"] = saved_state
                logger.debug("Sesión persistida cargada para '%s'.", session_domain)

        context: BrowserContext = await browser.new_context(**context_options)
        
        # ✅ Bloquear imágenes a nivel de contexto (ANTES de crear la página)
        await automator.block_images_async(context, url_pattern="https://encrypted-tbn0.gstatic.com/images")
        
        page: Page = await context.new_page()
        await automator.setup_page(page, fingerprint)
        await automator._warmup_session(page)

        logger.info(
            "Contexto listo | OS=%s | UA=%s… | session=%s",
            fingerprint.navigator_platform,
            fingerprint.user_agent[:40],
            "cargada" if context_options.get("storage_state") else "nueva",
        )
        return context, page

    async def _rotate_identity(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        old_context: BrowserContext,
        label: str,
    ) -> tuple[BrowserContext, Page]:
        """
        Rota identidad ante CAPTCHA no resoluble.

        Cierra el contexto bloqueado (descarta cookies contaminadas) y abre
        uno nuevo con un fingerprint completamente diferente (otro OS, otro UA,
        otro WebGL renderer). No rota IP: la misma dirección pero con identidad
        de browser diferente es suficiente para CAPTCHAs basados en sesión.

        Args:
            browser:     Browser activo (se reutiliza; solo cambia el contexto).
            automator:   Automator del engine.
            old_context: Contexto bloqueado por CAPTCHA.
            label:       Etiqueta del engine para logging.

        Returns:
            Tupla ``(nuevo_context, nueva_page)`` con identidad fresca.
        """
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
        # session_domain="" → no cargar sesión contaminada del dominio anterior
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

        Después de cada keyword exitosa llama ``kw_repo.mark_scraped(keyword)``
        para actualizar ``last_scrap`` en la tabla ``keywords``.

        Flujo de recuperación:
          · Error genérico → cierra la página, abre una nueva en el mismo
            contexto (preserva cookies y localStorage).
          · CaptchaError  → ``_rotate_identity`` (nuevo contexto + fingerprint).

        Args:
            engine_id:   ID del CSE de Google.
            label:       Etiqueta descriptiva.
            platform:    Plataforma objetivo ("instagram", "facebook", …).
            keywords:    Lista de términos a buscar.
            total_pages: Páginas de resultados por keyword.
        """
        automator = GoogleCSEAutomator(
            cse_id=engine_id,
            platform=platform,
            post_repo=self._post_repo,
            config=self._cfg,
            browser_type=settings.BROWSER_TYPE,
        )

        fingerprint: BrowserFingerprint = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Fingerprint inicial | OS=%s | UA=%s…",
            label, fingerprint.navigator_platform, fingerprint.user_agent[:50],
        )

        async with async_playwright() as playwright:
            launch_opts = {"headless": settings.BROWSER_HEADLESS_DEFAULT}
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
                        logger.info("[%s] Stop signal. Saliendo.", label)
                        break

                    kw = raw_kw.strip()
                    if not kw:
                        continue

                    logger.info(
                        "[%s] [%d/%d] keyword='%s'", label, idx, len(keywords), kw
                    )

                    try:
                        await automator.run_keyword(page, kw, total_pages)
                        # ── Actualizar last_scrap en tabla keywords ────────────
                        if self._kw_repo:
                            await self._kw_repo.mark_scraped(kw)

                    except CaptchaError as captcha_exc:
                        logger.warning(
                            "[%s] CAPTCHA irresuelto (signal=%s) en '%s'. "
                            "Rotando identidad…",
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

                            logger.info("[%s] Reintentando '%s'…", label, kw)
                            await automator.run_keyword(page, kw, total_pages)
                            if self._kw_repo:
                                await self._kw_repo.mark_scraped(kw)

                        except Exception as rotate_exc:
                            logger.error(
                                "[%s] Fallo en reintento con nueva identidad '%s': %s",
                                label, kw, rotate_exc, exc_info=True,
                            )

                    except Exception as generic_exc:
                        # Recuperación suave: nueva página, mismo contexto
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

                    # Pausa entre keywords con distribución jitter
                    pause = self._cfg.jitter_wait(*self._cfg.between_keywords_range)
                    logger.debug("[%s] Pausa entre keywords: %.1fs", label, pause)
                    await asyncio.sleep(pause)

            finally:
                # Guardar sesión si está habilitado
                if settings.SESSION_PERSIST:
                    try:
                        if await self._session_store.save(context, "google.com"):
                            logger.info("[%s] Sesión guardada en disco.", label)
                    except Exception as save_exc:
                        logger.warning("[%s] No se pudo guardar sesión: %s", label, save_exc)
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
        """
        Ejecuta un ciclo completo: carga engines desde DB y los procesa.

        Los engines se procesan secuencialmente (no en paralelo) porque un único
        browser activo por IP es menos detectable que varios simultáneos.
        """
        engines: list[dict] = await self._fetch_engines_config()
        if not engines:
            logger.warning("Sin engines configurados. Saltando ciclo.")
            return

        for engine in engines:
            if not self._running:
                break

            engine_id: str | None = engine.get("engine_id")
            keywords:  list[str]  = engine.get("keywords", [])
            label:     str        = engine.get("label", engine_id or "?")
            platform:  str        = engine.get("platform", "")

            if not FILTER or label == FILTER:
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

        Abre SQLite al inicio y lo cierra en el ``finally``, independientemente
        de cómo termine el proceso (señal, excepción, fin normal).
        """
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        await self._db_connect()
        logger.info(
            "Orquestador iniciado | OUTPUT_MODE=%s | BROWSER=%s",
            settings.OUTPUT_MODE, settings.BROWSER_TYPE,
        )

        try:
            while self._running:
                logger.info("═" * 60)
                logger.info("Iniciando ciclo de scraping…")
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
        """Señaliza parada limpia: el ciclo actual termina su keyword antes de salir."""
        logger.info("Señal de parada recibida. Finalizando ciclo actual…")
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Punto de entrada async."""
    await ScraperOrchestrator().start()


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

    logger.info("Python %s | PID %d", sys.version.split()[0], os.getpid())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrumpido manualmente.")
        sys.exit(0)