"""
run_scraper.py
Orquestador principal del scraper con integración completa de anti-detección.

Arquitectura de sesión:
  - UN browser por engine (se reutiliza entre keywords para compartir estado)
  - UN contexto por sesión (cookies y localStorage persistidos en disco)
  - Contexto nuevo SOLO ante CAPTCHA no resoluble (nuevo fingerprint, contexto limpio)
  - Fingerprint generado UNA vez por contexto (coherente durante toda la sesión)

Flujo ante CAPTCHA:
  1. Auto-solver intenta resolver el checkbox automáticamente
  2. Si falla → espera resolución manual (solo si captcha_wait_for_human=True)
  3. Si sigue fallando → cierra contexto, genera nuevo fingerprint y contexto limpio

Flujo de error genérico (sin CAPTCHA):
  - Recuperación suave: cerrar página → abrir página nueva en mismo contexto
  - El contexto (cookies, localStorage) se mantiene intacto
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from anti_detection import BrowserFingerprint, generate_fingerprint
from config.settings import settings
from google_cse_automator import BrowserConfig, GoogleCSEAutomator
from utils.captcha_guard import CaptchaError
from utils.session_store import SessionStore
from database.core_db import DatabaseManager
from database.google_result_db import GoogleResultRepository

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ScraperOrchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ScraperOrchestrator:
    """
    Orquestador que gestiona el ciclo de vida del browser, contextos y keywords.

    Responsabilidades:
      - Crear y destruir browsers (uno por engine)
      - Generar fingerprints coherentes para cada sesión
      - Detectar CAPTCHAs y coordinar recuperación con nuevo contexto
      - Persistir sesiones entre ejecuciones del ciclo
      - Gestionar señales de sistema (SIGINT, SIGTERM) para shutdown limpio

    Attributes:
        _running:      Flag de control del bucle principal.
        _cfg:          Parámetros de timing y comportamiento del browser.
        _session_store: Persistencia de cookies/localStorage en disco.
    """

    def __init__(self) -> None:
        self._running = False
        self._cfg = BrowserConfig()
        self._session_store = SessionStore(settings.SESSION_DIR)

        self._db_manager: DatabaseManager | None = None
        self._results_repo: GoogleResultRepository | None = None

    # ── Configuración de engines ─────────────────────────────────────────────

    async def _fetch_engines_config(self) -> list[dict]:
        """
        Placeholder: devuelve la configuración de engines y keywords.

        En producción, este método debería leer desde una base de datos,
        un archivo de configuración o una API externa.

        Returns:
            Lista de dicts con ``engine_id``, ``label`` y ``keywords``.
        """
        logger.info("[CONFIG] Cargando configuración de motores...")
        await asyncio.sleep(0.05)  # Simula latencia de red/DB

        return [
            {"label": "IG-KW-Engine", "engine_id": "c4b97eed1414fcb14", "keywords": 
                [
                    "#LaPatriaSeDefiende", 'Cuba',
                    "#CubaVive"
                ]
            },
            {
                "label": "general",
                "engine_id": "294a079ba2d4267d5",
                "keywords": [
                    "https://www.facebook.com/cubasatelite/posts/*",
                    "https://www.facebook.com/groups/3061231334163874/posts/*",
                ],
            },
            {"label": "KW MONITOR", "engine_id": "b3d8ab5d4c4a84c70", "keywords":
                [
                    "#LaPatriaSeDefiende",
                    "#CubaVive", "#DeZurdaTeam", "#YoSigoAMiPresidente", "#CubaPorLaSalud",
                    "#NoAlTerrorismo"
                ]
            },
            {"label": "general", "engine_id": "294a079ba2d4267d5", "keywords": [
                    "https://www.facebook.com/PresidenciaDeCuba/posts/*",
                    "https://www.facebook.com/gerardo.hernandez.nordelo/posts/*",
                    "https://www.facebook.com/groups/434004943672696/posts/*",
                    "https://www.facebook.com/groups/67706680225/posts/*",
                    "https://www.facebook.com/profile.php?id=61575946707396/posts/*",
                    "https://www.facebook.com/ranchueleros.por.la.libertad/posts/*",
   

            ]}
        ]

    # ── Helpers de contexto ──────────────────────────────────────────────────

    async def _create_context_and_page(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        session_domain: str = "google.com",
    ) -> tuple[BrowserContext, Page]:
        """
        Crea un contexto nuevo con fingerprint, stealth, warmup y sesión persistida.

        Integra las capas de anti-detección en el proceso de creación:
          1. Fingerprint coherente aplicado al contexto (UA, locale, timezone, viewport)
          2. Sesión persistida cargada si SESSION_PERSIST=true y session_domain no vacío
          3. Init script de stealth inyectado antes de la primera navegación
          4. Response interceptor para capturar 429/403
          5. Warmup session: navegar a Google y simular actividad antes del CSE

        Args:
            browser:        Browser activo (Firefox o Chromium).
            automator:      Instancia del automator (para setup_page y warmup).
            fingerprint:    Fingerprint coherente para esta sesión.
            session_domain: Dominio para buscar sesión persistida en disco.
                            Cadena vacía → siempre contexto limpio (sin sesión).

        Returns:
            Tupla ``(context, page)`` lista para scraping.
        """
        # 1. Cargar sesión persistida si está habilitada y existe en disco
        context_options = fingerprint.build_context_options()
        saved_state = (
            self._session_store.load_state_dict(session_domain)
            if settings.SESSION_PERSIST and session_domain
            else None
        )
        if saved_state:
            context_options["storage_state"] = saved_state

        # 2. Crear contexto con fingerprint completo
        context: BrowserContext = await browser.new_context(**context_options)
        page: Page = await context.new_page()

        # 3. Aplicar stealth + response interception
        await automator.setup_page(page, fingerprint)

        # 4. Warmup: navegar a Google y simular actividad humana
        await automator._warmup_session(page)

        logger.info(
            "Contexto creado: OS=%s | UA=%s... | session=%s",
            fingerprint.navigator_platform,
            fingerprint.user_agent[:40],
            "loaded" if saved_state else "fresh",
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
        Rota la identidad ante un CAPTCHA no resoluble: cierra el contexto actual
        y abre uno nuevo con un fingerprint completamente diferente.

        Sin rotación de IP: la misma IP pero con cookies, fingerprint y localStorage
        frescos. Suficiente para la mayoría de CAPTCHAs de checkpoint que se basan
        en el estado de sesión, no en la IP.

        Flujo:
          1. Cerrar el contexto bloqueado (descarta cookies contaminadas)
          2. Generar un fingerprint NUEVO (diferente OS/UA/WebGL al detectado)
          3. Abrir contexto limpio (sin sesión persistida) + warmup

        Args:
            browser:     Browser activo (se REUTILIZA, no se cierra).
            automator:   Instancia del automator.
            old_context: Contexto bloqueado por CAPTCHA.
            label:       Etiqueta de engine para logging.

        Returns:
            Tupla ``(nuevo_context, nueva_page)`` lista para reintentar.
        """
        # 1. Cerrar el contexto bloqueado (NO guardar sesión contaminada)
        try:
            await old_context.close()
            logger.debug("[%s] Contexto bloqueado cerrado.", label)
        except Exception as close_exc:
            logger.debug("[%s] Error cerrando contexto: %s", label, close_exc)

        # 2. Generar NUEVO fingerprint (diferente al que fue detectado)
        new_fingerprint = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Nuevo fingerprint: %s | %s",
            label,
            new_fingerprint.navigator_platform,
            new_fingerprint.user_agent[:50],
        )

        # 3. Crear contexto limpio sin sesión persistida (proxy=None = conexión directa)
        return await self._create_context_and_page(
            browser=browser,
            automator=automator,
            fingerprint=new_fingerprint,
            session_domain="",  # Cadena vacía → no buscar sesión en disco
        )

    # ── Engine loop ──────────────────────────────────────────────────────────

    async def _run_engine_keywords(
        self,
        engine_id: str,
        label: str,
        keywords: list[str],
        total_pages: int = 3,
    ) -> None:
        """
        Procesa todas las keywords de un engine con un único browser y contexto.

        Ciclo de vida de recursos:
          - Browser: creado una vez al inicio, cerrado en el ``finally``.
          - Contexto + página: creados antes del loop de keywords.
            Se recrean SOLO ante CAPTCHA no resoluble (nuevo fingerprint, contexto limpio).
          - Página: se reutiliza entre keywords; se recrea ante error genérico.
          - Fingerprint: uno por contexto, coherente durante toda la sesión.

        Args:
            engine_id:   ID del CSE de Google.
            label:       Etiqueta descriptiva para logs.
            keywords:    Lista de términos a buscar.
            total_pages: Páginas de resultados por keyword.
        """
        # Instanciar el automator para este engine
        automator = GoogleCSEAutomator(
            cse_id=engine_id,
            config=self._cfg,
            browser_type=settings.BROWSER_TYPE,
            db_manager=self._db_manager,
            results_repo=self._results_repo,
            sent_to_endpoint=settings.TO_ENDPOINT
        )

        # Generar fingerprint inicial coherente para toda la sesión
        fingerprint: BrowserFingerprint = generate_fingerprint(settings.BROWSER_TYPE)
        logger.info(
            "[%s] Fingerprint inicial: %s | %s",
            label,
            fingerprint.navigator_platform,
            fingerprint.user_agent[:50],
        )

        async with async_playwright() as playwright:
            # ── Lanzar browser ───────────────────────────────────────────────
            launch_opts = {"headless": settings.BROWSER_HEADLESS}
            if settings.BROWSER_TYPE == "firefox":
                browser: Browser = await playwright.firefox.launch(**launch_opts)
            else:
                browser: Browser = await playwright.chromium.launch(**launch_opts)

            logger.info("[%s] Browser '%s' iniciado.", label, settings.BROWSER_TYPE)

            # ── Contexto inicial ─────────────────────────────────────────────
            context, page = await self._create_context_and_page(
                browser=browser,
                automator=automator,
                fingerprint=fingerprint,
            )

            try:
                for idx, raw_kw in enumerate(keywords, 1):
                    if not self._running:
                        logger.info("[%s] Stop signal recibido. Saliendo del loop.", label)
                        break

                    kw = raw_kw.strip()
                    if not kw:
                        continue

                    logger.info(
                        "[%s] [%d/%d] Procesando keyword: '%s'",
                        label, idx, len(keywords), kw,
                    )

                    # ── Ejecutar keyword ─────────────────────────────────────
                    try:
                        await automator.run_keyword(page, kw, total_pages)

                    except CaptchaError as captcha_exc:
                        # ── CAPTCHA no resoluble: rotar identidad ────────────
                        # El auto-solver ya intentó resolverlo dentro de run_keyword.
                        # Aquí solo llegamos si todos los intentos fallaron.
                        logger.warning(
                            "[%s] CAPTCHA no resoluble (signal=%s) en '%s'. "
                            "Rotando identidad (nuevo fingerprint + contexto limpio)...",
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

                            logger.info("[%s] Reintentando '%s' con nueva identidad...", label, kw)
                            await automator.run_keyword(page, kw, total_pages)

                        except Exception as rotate_exc:
                            logger.error(
                                "[%s] Fallo en reintento con nueva identidad para '%s': %s",
                                label, kw, rotate_exc,
                                exc_info=True,
                            )

                    except Exception as generic_exc:
                        # ── Error genérico: recuperación suave ───────────────
                        # Cierra solo la página y abre una nueva en el mismo
                        # contexto para preservar cookies y localStorage.
                        logger.error(
                            "[%s] Error inesperado en '%s': %s",
                            label, kw, generic_exc,
                            exc_info=True,
                        )
                        try:
                            await page.close()
                            page = await context.new_page()
                            await automator.setup_page(page, fingerprint)
                            logger.info("[%s] Página recreada. Continuando...", label)
                        except Exception as recovery_exc:
                            logger.error(
                                "[%s] Recuperación fallida. Abortando engine: %s",
                                label, recovery_exc,
                            )
                            break

                    # ── Pausa entre keywords ─────────────────────────────────
                    pause_seconds = self._cfg.jitter_wait(*self._cfg.between_keywords_range)
                    logger.debug(
                        "[%s] Pausa entre keywords: %.1fs", label, pause_seconds
                    )

                    await asyncio.sleep(pause_seconds)

            finally:
                # ── Guardar sesión antes de cerrar (solo si está habilitado) ──
                if settings.SESSION_PERSIST:
                    try:
                        saved = await self._session_store.save(context, "google.com")
                        if saved:
                            logger.info("[%s] Sesión guardada en disco.", label)
                    except Exception as save_exc:
                        logger.warning("[%s] No se pudo guardar la sesión: %s", label, save_exc)
                else:
                    logger.debug("[%s] SESSION_PERSIST=false, sesión descartada.", label)

                # ── Cerrar contexto y browser ────────────────────────────────
                try:
                    await context.close()
                except Exception:
                    pass
                await browser.close()
                logger.info("[%s] Browser cerrado.", label)

    # ── Ciclo principal ──────────────────────────────────────────────────────

    async def _execute_cycle(self) -> None:
        """
        Ejecuta un ciclo completo: obtiene la config de engines y los procesa.

        Cada engine se ejecuta secuencialmente. Para paralelismo se podría
        usar ``asyncio.gather``, pero un solo browser concurrente es menos
        detectable que múltiples browsers simultáneos desde la misma IP.
        """
        engines: list[dict] = await self._fetch_engines_config()
        if not engines:
            logger.warning("Sin motores configurados. Saltando ciclo.")
            return

        for engine in engines:
            if not self._running:
                break

            engine_id: str | None = engine.get("engine_id")
            keywords: list[str] = engine.get("keywords", [])
            label: str = engine.get("label", engine_id or "?")

            # Filtrar keywords vacías o inválidas
            valid_kws = [k for k in keywords if isinstance(k, str) and k.strip()]

            if not engine_id or not valid_kws:
                logger.warning(
                    "Configuración inválida para engine '%s' (engine_id=%s, keywords=%d). "
                    "Omitiendo.",
                    label, engine_id, len(valid_kws),
                )
                continue

            logger.info(
                "── Engine '%s' | %d keywords | engine_id=%s",
                label, len(valid_kws), engine_id,
            )
            try:
                await self._run_engine_keywords(
                    engine_id=engine_id,
                    label=label,
                    keywords=valid_kws,
                    total_pages=settings.TOTAL_PAGES_PER_KEYWORD,
                )
            except Exception as critical_exc:
                logger.error(
                    "Fallo crítico en engine '%s': %s", label, critical_exc,
                    exc_info=True,
                )

    # ── Inicio y parada ──────────────────────────────────────────────────────

    async def _setup_database(self) -> None:
        """Inicializa conexión a MongoDB y cachea el repositorio."""
        self._db_manager = DatabaseManager(settings.MONGO_URL, settings.DB_NAME)
        await self._db_manager.connect()
        logger.info(f"MongoDB conectado: {settings.DB_NAME}")
        self._results_repo = GoogleResultRepository(self._db_manager)
        await self._results_repo.initialize()

    async def start(self) -> None:
        """
        Inicia el bucle principal del orquestador.

        Registra handlers de señal para SIGINT/SIGTERM que permiten un
        shutdown limpio: el ciclo actual termina antes de salir.
        """
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        await self._setup_database()
        logger.info("Orquestador iniciado. Ctrl+C para detener.")
        try:
            while self._running:
                logger.info("═" * 60)
                logger.info("Iniciando nuevo ciclo de scraping...")
                await self._execute_cycle()

                if not self._running:
                    break

                delay = settings.CYCLE_DELAY_SECONDS
                logger.info("Ciclo completado. Próximo ciclo en %ds.", delay)
                await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("Bucle cancelado por señal.")
        finally:
            if self._db_manager:
                await self._db_manager.disconnect()
                logger.info("MongoDB desconectado.")

        logger.info("Orquestador detenido.")

    def stop(self) -> None:
        """
        Señaliza al orquestador para que detenga el ciclo actual limpiamente.

        El ciclo en curso termina su keyword actual antes de salir.
        """
        logger.info("Señal de parada recibida. Finalizando ciclo actual...")
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Punto de entrada async del scraper."""
    orchestrator = ScraperOrchestrator()
    await orchestrator.start()


if __name__ == "__main__":
    import os
    from pathlib import Path

    # ── Configurar logging ────────────────────────────────────────────────────
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

    # Reducir verbosidad de librerías externas
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    logger.info("Python %s | PID %d", sys.version.split()[0], os.getpid())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Aplicación interrumpida manualmente.")
        sys.exit(0)
