"""
run_scraper.py
Orquestador principal del scraper con ejecución concurrente por FILTER.
Modo headless por defecto + navegador visible automático ante CAPTCHA.

MIGRACIÓN A CAMOUFOX
─────────────────────
  - Reemplaza async_playwright() por AsyncCamoufox
  - Camoufox maneja su propio lanzamiento de navegador (Firefox modificado)
  - Perfiles aislados por instancia para evitar conflictos de memoria
  - OS del fingerprint mapeado al parámetro ``os`` de Camoufox
  - Delay configurable entre lanzamientos para evitar picos de RAM
  - Mantiene toda la lógica de concurrencia, CAPTCHA, persistencia

MIGRACIÓN A SUPABASE
─────────────────────
  - Reemplaza SQLiteManager por SupabaseManager
  - Keywords se reclaman con bloqueo atómico (scraping=true)
  - URLs se insertan con UPSERT (ON CONFLICT DO NOTHING)
  - Al terminar cada keyword: mark_scraped() actualiza scraped_at
  - Si falla el engine: release_keywords() libera las no completadas

Correcciones aplicadas en esta versión
──────────────────────────────────────
  BUG FIX  _switch_browser_visibility  → tras abrir el browser visible, navega
           explícitamente a la URL del CAPTCHA (captcha_url). Sin esto la página
           quedaba en about:blank porque el browser headless anterior (que tenía
           la URL cargada) ya se había cerrado.

  BUG FIX  _wait_for_captcha_resolution → con múltiples hilos, input() en
           run_in_executor compite por la misma terminal. Ahora se usa un
           asyncio.Lock global serializado. Solo un hilo a la vez puede pedir
           input; los demás esperan su timeout automático sin colisionar.

  BUG FIX  _wait_for_captcha_resolution → tiempo MÍNIMO de espera de 120s.
           Si el usuario presiona ENTER antes de 120s, se ignora y sigue
           esperando. El navegador permanece visible todo el tiempo.

  IMPROVE  _switch_browser_visibility  → acepta captcha_url: str para navegar
           a la página correcta tras abrir el browser visible.

  IMPROVE  _run_engine_keywords        → captura la URL activa de la página en
           el momento del CaptchaError y la pasa a _switch_browser_visibility.

  IMPROVE  _wait_for_captcha_resolution → si otro hilo ya tiene el turno de
           input, espera timeout silenciosamente (sin bloquear la consola) y
           continúa. Esto evita que 2+ hilos simultáneos rompan la terminal.

  IMPROVE  ConcurrentFilterManager     → delay configurable entre lanzamientos
           de browsers para evitar picos de RAM al iniciar múltiples filtros.

OPTIMIZACIONES DE VELOCIDAD
────────────────────────────
  - Waits selectivos en lugar de delays ciegos
  - Movimientos de ratón reducidos para elementos de bajo riesgo
  - CAPTCHA con tiempo mínimo de espera garantizado
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
    "KW FB",
    "KW IG",
    "FB CR General Kano",
    "Kano Cluster CR",
    "Especial",
]
MAX_CONCURRENT_BROWSERS: Final[int] = getattr(settings, "MAX_CONCURRENT_BROWSERS", 2)

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
    Orquestador que gestiona browser, contextos, Supabase y el ciclo de keywords.
    Soporta cambio dinámico de visibilidad del browser ante CAPTCHAs.

    MIGRACIÓN A CAMOUFOX:
      - Usa AsyncCamoufox en lugar de async_playwright
      - Camoufox maneja su propio lanzamiento de navegador
      - Perfiles aislados por instancia (evita malloc corruption)
      - OS del fingerprint mapeado al parámetro ``os`` de Camoufox
      - Mantiene toda la lógica de fingerprint, stealth, CAPTCHA, persistencia

    MIGRACIÓN A SUPABASE:
      - Usa SupabaseManager en lugar de SQLiteManager
      - Keywords se reclaman con bloqueo atómico
      - URLs se insertan con UPSERT
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
        """Conecta a Supabase."""
        self._db = SupabaseManager()
        await self._db.connect()
        logger.info("Supabase conectado.")

    async def _db_disconnect(self) -> None:
        """Desconecta de Supabase."""
        if self._db:
            await self._db.disconnect()
            self._db = None
            logger.info("Supabase desconectado.")

    # ── Engines config ────────────────────────────────────────────────────────

    async def _fetch_engines_config(self) -> list[dict]:
        """
        Obtiene la configuración de engines desde Supabase.

        Si hay un FILTER específico, reclama keywords solo para ese label.
        Si no hay FILTER (modo TODOS), obtiene todos los labels distintos
        y reclama 10 keywords de cada uno.

        Returns:
            Lista de dicts con engine_id, label, platform, keywords y _keyword_ids.
        """
        if self._db is None or self._db.keyword_repo is None:
            logger.warning(
                "_fetch_engines_config: Supabase no inicializado. Usando fallback."
            )
            return _FALLBACK_ENGINES

        # ── Determinar qué labels procesar ──────────────────────────────────
        if self._filter_label:
            # Modo filtro específico: solo ese label
            labels_to_process = [self._filter_label]
        else:
            # Modo TODOS: obtener todos los labels distintos de Supabase
            try:
                all_labels = await self._db.keyword_repo.get_distinct_labels()
                if not all_labels:
                    logger.warning(
                        "No se encontraron labels en Supabase. Usando fallback."
                    )
                    return _FALLBACK_ENGINES
                labels_to_process = all_labels
                logger.info(
                    "Modo TODOS: %d labels encontrados en Supabase.",
                    len(labels_to_process),
                )
            except Exception as exc:
                logger.error(
                    "Error obteniendo labels de Supabase: %s. Usando fallback.",
                    exc,
                )
                return _FALLBACK_ENGINES

        # ── Reclamar keywords para cada label ───────────────────────────────
        engines_config = []

        for label in labels_to_process:
            claimed = await self._db.keyword_repo.claim_keywords(
                label=label,
                limit=10,
            )

            if not claimed:
                logger.info(
                    "No hay keywords disponibles para label='%s'.",
                    label,
                )
                continue

            # Agrupar por label → engine
            first = claimed[0]
            keywords = [c.keyword for c in claimed]

            engines_config.append({
                "label": label,
                "engine_id": first.engine,
                "platform": first.platform,
                "keywords": keywords,
                "_keyword_ids": [c.id for c in claimed],
            })

            logger.info(
                "[CONFIG] Label='%s' | %d keywords reclamadas | Engine=%s",
                label,
                len(keywords),
                first.engine,
            )

        if not engines_config:
            logger.warning(
                "No se pudo reclamar keywords para ningún label. Usando fallback."
            )
            return _FALLBACK_ENGINES

        return engines_config

    # ── Browser management ────────────────────────────────────────────────────

    @staticmethod
    def _map_platform_to_camoufox_os(navigator_platform: str) -> str:
        """
        Mapea ``navigator.platform`` del fingerprint al OS de Camoufox.

        Camoufox acepta: ``"windows"`` | ``"macos"`` | ``"linux"``
        El fingerprint genera ``navigator.platform`` como:
          - ``"Win32"``          → Windows
          - ``"MacIntel"``       → macOS
          - ``"Linux x86_64"``   → Linux

        Este mapeo garantiza coherencia entre el fingerprint (UA, platform,
        WebGL vendor/renderer) y el OS que Camoufox simula a nivel binario.
        Sin coherencia, los detectores de fingerprint encuentran inconsistencias
        entre ``navigator.platform`` y las APIs nativas del navegador.

        Args:
            navigator_platform: Valor de ``navigator.platform`` del fingerprint.

        Returns:
            String de OS compatible con el parámetro ``os`` de ``AsyncCamoufox``.
        """
        platform_lower = navigator_platform.lower()

        if "win" in platform_lower:
            return "windows"
        if "mac" in platform_lower:
            return "macos"
        if "linux" in platform_lower:
            return "linux"

        # Fallback: Windows es el OS más común (~75% del tráfico real)
        return "windows"

    async def _launch_camoufox(
        self,
        headless: bool,
        label: str,
        camoufox_os: str,
    ) -> tuple[AsyncCamoufox, Browser]:
        """
        Lanza Camoufox con configuración anti-detección.

        CAMOUFOX: El aislamiento de perfiles es automático. Cada instancia
        de AsyncCamoufox crea su propio perfil temporal internamente.

        Args:
            headless:     Si True, lanza sin interfaz gráfica.
            label:        Label del filtro (para logs).
            camoufox_os:  OS para fingerprint ("windows" | "macos" | "linux").

        Returns:
            Tupla ``(camoufox_instance, browser)``.
        """
        camoufox_params: dict = {
            "headless": headless,
            "humanize": settings.CAMOUFOX_HUMANIZE,
            "geoip":    settings.CAMOUFOX_GEOIP,
            "os":       camoufox_os,
        }

        camoufox_instance = AsyncCamoufox(**camoufox_params)
        browser = await camoufox_instance.__aenter__()

        mode_str = "HEADLESS" if headless else "VISIBLE"
        logger.info(
            "[%s] Camoufox iniciado en modo %s | OS=%s | humanize=%s | geoip=%s",
            label,
            mode_str,
            camoufox_os,
            settings.CAMOUFOX_HUMANIZE,
            settings.CAMOUFOX_GEOIP,
        )
        return camoufox_instance, browser

    async def _save_context_state(
        self,
        context: BrowserContext,
        domain: str = "google.com",
    ) -> dict | None:
        """Extrae el storage_state del contexto para persistencia."""
        if not settings.SESSION_PERSIST or not context:
            return None
        try:
            state = await context.storage_state()
            logger.debug(
                "[%s] storage_state extraído para '%s'.",
                self._filter_label or "MAIN",
                domain,
            )
            return state
        except Exception as exc:
            logger.warning(
                "[%s] No se pudo extraer storage_state: %s",
                self._filter_label or "MAIN",
                exc,
            )
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
        """
        Crea un contexto y página con fingerprint y sesión persistida.

        CAMOUFOX: Compatible con la API de Playwright para browser.new_context().

        Args:
            browser:        Instancia de Browser de Camoufox.
            automator:      Instancia de GoogleCSEAutomator.
            fingerprint:    Fingerprint de la sesión actual.
            session_domain: Dominio para cargar/guardar sesión.
            storage_state:  Estado de sesión explícito (None = cargar de disco).
            skip_warmup:    Si True, omite el warmup inicial.

        Returns:
            Tupla ``(context, page)``.
        """
        context_options = fingerprint.build_context_options()

        if storage_state:
            context_options["storage_state"] = storage_state
            logger.debug("Restaurando sesión desde storage_state explícito.")
        elif settings.SESSION_PERSIST and session_domain:
            saved_state = self._session_store.load_state_dict(session_domain)
            if saved_state:
                context_options["storage_state"] = saved_state
                logger.debug(
                    "Sesión persistida cargada desde disco para '%s'.",
                    session_domain,
                )

        context: BrowserContext = await browser.new_context(**context_options)
        await automator.block_images_async(
            context,
            url_pattern="https://encrypted-tbn0.gstatic.com/images",
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
        """
        Rota la identidad del navegador generando un nuevo fingerprint.

        CAMOUFOX: El nuevo fingerprint puede tener un OS diferente, pero
        el browser de Camoufox mantiene el OS con el que fue lanzado.
        Esto es aceptable porque el fingerprint JS sigue siendo coherente
        internamente (UA, platform, WebGL coinciden entre sí).

        Args:
            browser:          Instancia de Browser de Camoufox.
            automator:        Instancia de GoogleCSEAutomator.
            old_context:      Contexto a cerrar.
            label:            Label del filtro (para logs).
            preserve_session: Si True, guarda el storage_state antes de cerrar.

        Returns:
            Tupla ``(new_context, new_page, new_fingerprint)``.
        """
        storage_state = await self._save_context_state(old_context) if preserve_session else None
        try:
            await old_context.close()
            logger.debug("[%s] Contexto bloqueado cerrado.", label)
        except Exception as exc:
            logger.debug("[%s] Error cerrando contexto: %s", label, exc)

        # Generar nuevo fingerprint (puede tener OS diferente)
        new_fp = generate_fingerprint("firefox")  # Camoufox es Firefox modificado
        logger.info(
            "[%s] Nueva identidad | OS=%s | UA=%s…",
            label,
            new_fp.navigator_platform,
            new_fp.user_agent[:50],
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
        old_camoufox: AsyncCamoufox,
        old_browser: Browser,
        old_context: BrowserContext,
        automator: GoogleCSEAutomator,
        fingerprint: BrowserFingerprint,
        label: str,
        new_headless: bool,
        captcha_url: str = "",
    ) -> tuple[AsyncCamoufox, Browser, BrowserContext, Page]:
        """
        Cambia la visibilidad del browser preservando fingerprint y sesión.

        CAMOUFOX: Lanza un nuevo AsyncCamoufox con el modo cambiado.
        El aislamiento de perfiles es automático.

        Args:
            old_camoufox: Instancia anterior de AsyncCamoufox.
            old_browser:  Browser anterior.
            old_context:  Contexto anterior.
            automator:    Instancia de GoogleCSEAutomator.
            fingerprint:  Fingerprint a preservar.
            label:        Label del filtro (para logs).
            new_headless: True para volver a headless, False para visible.
            captcha_url:  URL del CAPTCHA para navegar tras abrir visible.

        Returns:
            Tupla ``(new_camoufox, new_browser, new_context, new_page)``.
        """
        from_mode = "HEADLESS" if not new_headless else "VISIBLE"
        to_mode   = "VISIBLE"  if not new_headless else "HEADLESS"
        logger.info(
            "[%s] Cambiando visibilidad del browser: %s → %s",
            label, from_mode, to_mode,
        )

        # 1. Guardar estado de sesión antes de cerrar
        storage_state = await self._save_context_state(old_context)

        # 2. Cerrar recursos del browser antiguo
        for resource in (old_context, old_browser):
            try:
                await resource.close()
            except Exception:
                pass

        try:
            await old_camoufox.__aexit__(None, None, None)
        except Exception:
            pass

        # 3. Determinar OS del fingerprint (mantener coherencia)
        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)

        # 4. Lanzar nuevo Camoufox con modo cambiado y mismo OS
        camoufox_params: dict = {
            "headless": new_headless,
            "humanize": settings.CAMOUFOX_HUMANIZE,
            "geoip":    settings.CAMOUFOX_GEOIP,
            "os":       camoufox_os,
        }

        new_camoufox = AsyncCamoufox(**camoufox_params)
        new_browser = await new_camoufox.__aenter__()

        # 5. Crear contexto con mismo fingerprint + sesión restaurada
        new_context, new_page = await self._create_context_and_page(
            browser=new_browser,
            automator=automator,
            fingerprint=fingerprint,
            session_domain="google.com",
            storage_state=storage_state,
            skip_warmup=True,
        )

        # 6. Navegar a la URL del CAPTCHA en el nuevo browser visible
        if not new_headless and captcha_url:
            try:
                logger.info(
                    "[%s] Navegando a URL del CAPTCHA: %s",
                    label, captcha_url,
                )
                await new_page.goto(
                    captcha_url,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
            except Exception as nav_exc:
                logger.warning(
                    "[%s] No se pudo navegar a '%s': %s. "
                    "El usuario verá la sesión restaurada.",
                    label, captcha_url, nav_exc,
                )

        # 7. Inyectar banner de notificación
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

        return new_camoufox, new_browser, new_context, new_page

    async def _wait_for_captcha_resolution(self, label: str) -> None:
        """
        Espera a que el usuario resuelva el CAPTCHA manualmente.

        CORRECCIÓN CRÍTICA:
          - Tiempo MÍNIMO de espera: 120 segundos (2 minutos)
          - Si el usuario presiona ENTER antes de 120s, se ignora y sigue esperando
          - Después de 120s, se acepta ENTER para continuar
          - El navegador permanece visible todo el tiempo (sin recargas)

        BUG FIX (multi-hilo): _captcha_input_lock es un asyncio.Lock() global.
          - El primer hilo en adquirirlo pide ENTER al usuario normalmente.
          - Los demás hilos NO esperan el lock bloqueados; comprueban si
            pueden adquirirlo en tiempo 0 (acquire con timeout=0).
          - Si no pueden, esperan su timeout silenciosamente sin tocar
            la consola y continúan solos cuando expira.

        Esto garantiza que nunca hay dos input() activos al mismo tiempo.

        Args:
            label: Label del filtro (para logs).
        """
        MINIMUM_WAIT_SECONDS: int = 120  # MÍNIMO DURO DE 2 MINUTOS
        timeout: int = getattr(settings, "CAPTCHA_MANUAL_TIMEOUT", 300)

        # Asegurar que el timeout sea al menos el mínimo
        effective_timeout = max(timeout, MINIMUM_WAIT_SECONDS)

        loop = asyncio.get_running_loop()
        got_input_lock = False

        # Intentar adquirir el lock de input sin bloquear
        if not _captcha_input_lock.locked():
            try:
                got_input_lock = await asyncio.wait_for(
                    asyncio.shield(_captcha_input_lock.acquire()),
                    timeout=0.1,
                )
            except asyncio.TimeoutError:
                pass

        if got_input_lock:
            try:
                logger.info(
                    "[%s] ══ CAPTCHA DETECTADO ══\n"
                    "    Tiempo mínimo de espera: %d segundos (%.1f minutos)\n"
                    "    El navegador está VISIBLE. Resuelve el CAPTCHA.\n"
                    "    NO presiones ENTER antes de que pasen %d segundos.",
                    label,
                    MINIMUM_WAIT_SECONDS,
                    MINIMUM_WAIT_SECONDS / 60,
                    MINIMUM_WAIT_SECONDS,
                )

                start_time = loop.time()

                # Fase 1: Esperar el tiempo MÍNIMO (120s) sin aceptar ENTER temprano
                while (loop.time() - start_time) < MINIMUM_WAIT_SECONDS:
                    remaining = MINIMUM_WAIT_SECONDS - int(loop.time() - start_time)

                    # Usar input con timeout corto para mostrar cuenta regresiva
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                input,
                                f"[{label}] ⏳ Faltan {remaining}s mínimos. "
                                f"Resuelve el CAPTCHA pero NO presiones ENTER aún: ",
                            ),
                            timeout=1.0,
                        )
                        # Si llegó aquí, el usuario presionó ENTER antes del mínimo
                        logger.warning(
                            "[%s] ENTER presionado antes del mínimo de %ds. Ignorado. "
                            "Sigue esperando...",
                            label,
                            MINIMUM_WAIT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        # Timeout normal, continuar esperando
                        pass
                    except Exception:
                        # Error de input, continuar
                        await asyncio.sleep(1.0)

                # Fase 2: Tiempo mínimo cumplido, ahora sí aceptar ENTER
                logger.info(
                    "[%s] ✅ Tiempo mínimo (%ds) cumplido. "
                    "Presiona ENTER cuando hayas resuelto el CAPTCHA.",
                    label,
                    MINIMUM_WAIT_SECONDS,
                )

                # Esperar ENTER real (con timeout máximo configurable)
                remaining_timeout = effective_timeout - MINIMUM_WAIT_SECONDS
                if remaining_timeout > 0:
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                input,
                                f"[{label}] Presiona ENTER para continuar "
                                f"(timeout en {remaining_timeout}s): ",
                            ),
                            timeout=remaining_timeout,
                        )
                        logger.info("[%s] ENTER recibido. Continuando...", label)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[%s] Timeout total (%ds) alcanzado. "
                            "Continuando automáticamente...",
                            label,
                            effective_timeout,
                        )
                        GoogleCSEAutomator._play_alert_sound()
                else:
                    # Sin timeout adicional (espera indefinida)
                    await loop.run_in_executor(
                        None,
                        input,
                        f"[{label}] Presiona ENTER para continuar: ",
                    )
                    logger.info("[%s] ENTER recibido. Continuando...", label)

            finally:
                _captcha_input_lock.release()

        else:
            # Otro hilo ya tiene el turno de input → espera silenciosa
            wait_secs = effective_timeout
            logger.warning(
                "[%s] Otro filtro ya está esperando input del usuario. "
                "Esperando %ds automáticamente antes de continuar...",
                label,
                wait_secs,
            )
            await asyncio.sleep(wait_secs)
            logger.info(
                "[%s] Tiempo de espera automático agotado. Continuando...",
                label,
            )

        logger.info("[%s] Continuando con el scraping...", label)

    # ── Engine loop ───────────────────────────────────────────────────────────

    async def _run_engine_keywords(
        self,
        engine_id: str,
        label: str,
        platform: str,
        keywords: list[str],
        keyword_ids: list[int],
        total_pages: int = 3,
    ) -> None:
        """
        Procesa todas las keywords de un engine con soporte para cambio de
        visibilidad ante CAPTCHA.

        Args:
            engine_id:   ID del Custom Search Engine de Google.
            label:       Label del engine (para logs y Supabase).
            platform:    Plataforma objetivo ("instagram" | "facebook").
            keywords:    Lista de keywords a procesar.
            keyword_ids: IDs en Supabase para marcar como scrapeadas.
            total_pages: Páginas de resultados por keyword.
        """
        semaphore_acquired = False

        automator = GoogleCSEAutomator(
            cse_id=engine_id,
            platform=platform,
            url_repo=self._db.url_repo if self._db else None,
            config=self._cfg,
        )

        self._current_headless = getattr(settings, "BROWSER_HEADLESS_DEFAULT", True)

        # ── Generar fingerprint primero ──
        fingerprint: BrowserFingerprint = generate_fingerprint("firefox")

        # ── Determinar OS para Camoufox basado en el fingerprint ──
        camoufox_os = self._map_platform_to_camoufox_os(fingerprint.navigator_platform)

        logger.info(
            "[%s] Fingerprint inicial | OS=%s | UA=%s… | Headless=%s | Camoufox OS=%s",
            label,
            fingerprint.navigator_platform,
            fingerprint.user_agent[:50],
            self._current_headless,
            camoufox_os,
        )

        camoufox_instance: AsyncCamoufox | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None

        try:
            if self._browser_semaphore:
                await self._browser_semaphore.acquire()
                semaphore_acquired = True
                logger.debug("[%s] Semaphore adquirido.", label)

            # ── Lanzar Camoufox (aislamiento automático) ──
            camoufox_instance, browser = await self._launch_camoufox(
                headless=self._current_headless,
                label=label,
                camoufox_os=camoufox_os,
            )

            async with camoufox_instance as browser:
                context, page = await self._create_context_and_page(
                    browser=browser,
                    automator=automator,
                    fingerprint=fingerprint,
                )

                for idx, (raw_kw, kw_id) in enumerate(zip(keywords, keyword_ids), 1):
                    if not self._running:
                        logger.info("[%s] Stop signal. Saliendo.", label)
                        break

                    kw = raw_kw.strip()
                    if not kw:
                        continue

                    logger.info(
                        "[%s] [%d/%d] keyword='%s' (id=%d)",
                        label, idx, len(keywords), kw, kw_id,
                    )

                    try:
                        await automator.run_keyword(page, kw, total_pages)

                        # Marcar como scrapeada en Supabase
                        if self._db and self._db.keyword_repo:
                            await self._db.keyword_repo.mark_scraped(kw_id)
                            logger.debug(
                                "[%s] Keyword id=%d marcada como scrapeada.",
                                label, kw_id,
                            )

                    except CaptchaError as captcha_exc:
                        logger.warning(
                            "[%s] CAPTCHA irresuelto (signal=%s) en '%s'.",
                            label, captcha_exc.signal, kw,
                        )
                        GoogleCSEAutomator._play_alert_sound()

                        # Capturar URL actual ANTES de cerrar el browser headless
                        captcha_url: str = ""
                        try:
                            captcha_url = page.url
                            logger.debug(
                                "[%s] URL del CAPTCHA capturada: %s",
                                label, captcha_url,
                            )
                        except Exception:
                            pass

                        if (
                            getattr(settings, "BROWSER_VISIBLE_ON_CAPTCHA", True)
                            and self._current_headless
                        ):
                            # Cambiar a visible
                            (
                                camoufox_instance,
                                browser,
                                context,
                                page,
                            ) = await self._switch_browser_visibility(
                                old_camoufox=camoufox_instance,
                                old_browser=browser,
                                old_context=context,
                                automator=automator,
                                fingerprint=fingerprint,
                                label=label,
                                new_headless=False,
                                captcha_url=captcha_url,
                            )
                            self._current_headless = False

                            # Esperar resolución manual (mínimo 120s)
                            await self._wait_for_captcha_resolution(label)

                            logger.info(
                                "[%s] Reintentando '%s' con navegador visible…",
                                label, kw,
                            )
                            try:
                                await automator.run_keyword(page, kw, total_pages)

                                # Marcar como scrapeada en Supabase
                                if self._db and self._db.keyword_repo:
                                    await self._db.keyword_repo.mark_scraped(kw_id)

                            except Exception as retry_exc:
                                logger.error(
                                    "[%s] Fallo en reintento post-CAPTCHA '%s': %s",
                                    label, kw, retry_exc, exc_info=True,
                                )

                            if getattr(settings, "HEADLESS_AFTER_CAPTCHA", False):
                                (
                                    camoufox_instance,
                                    browser,
                                    context,
                                    page,
                                ) = await self._switch_browser_visibility(
                                    old_camoufox=camoufox_instance,
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
                            # Rotar identidad sin cambio de visibilidad
                            logger.info(
                                "[%s] Rotando identidad (sin cambio de visibilidad)…",
                                label,
                            )
                            context, page, fingerprint = await self._rotate_identity(
                                browser=browser,
                                automator=automator,
                                old_context=context,
                                label=label,
                            )
                            logger.info("[%s] Reintentando '%s'…", label, kw)
                            try:
                                await automator.run_keyword(page, kw, total_pages)

                                # Marcar como scrapeada en Supabase
                                if self._db and self._db.keyword_repo:
                                    await self._db.keyword_repo.mark_scraped(kw_id)

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

                    # Pausa entre keywords
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
                    logger.warning(
                        "[%s] No se pudo guardar sesión: %s", label, save_exc,
                    )

            for resource in filter(None, [page, context]):
                try:
                    await resource.close()
                except Exception:
                    pass

            mode_str = "HEADLESS" if self._current_headless else "VISIBLE"
            logger.info("[%s] Camoufox cerrado (modo: %s).", label, mode_str)

    # ── Ciclo principal ───────────────────────────────────────────────────────

    async def _execute_cycle(self) -> None:
        """
        Ejecuta un ciclo completo de scraping para el FILTER asignado.

        Flujo:
          1. Reclama keywords desde Supabase (claim_keywords)
          2. Procesa cada engine con sus keywords
          3. Si falla, libera keywords no completadas (release_keywords)
        """
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
            keyword_ids: list[int] = engine.get("_keyword_ids", [])

            if not engine_id or not keywords:
                logger.warning(
                    "Config inválida engine='%s' (engine_id=%s, keywords=%d). Omitiendo.",
                    label, engine_id, len(keywords),
                )
                continue

            logger.info(
                "── Engine '%s' | %d keywords | platform='%s' | engine_id=%s",
                label, len(keywords), platform, engine_id,
            )
            try:
                await self._run_engine_keywords(
                    engine_id=engine_id,
                    label=label,
                    platform=platform,
                    keywords=keywords,
                    keyword_ids=keyword_ids,
                    total_pages=settings.TOTAL_PAGES_PER_KEYWORD,
                )
            except Exception as critical_exc:
                logger.error(
                    "Fallo crítico en engine '%s': %s",
                    label, critical_exc, exc_info=True,
                )
                # Liberar keywords no completadas
                if self._db and self._db.keyword_repo and keyword_ids:
                    await self._db.keyword_repo.release_keywords(keyword_ids)
                    logger.info(
                        "[%s] %d keywords liberadas tras fallo.",
                        label, len(keyword_ids),
                    )

    async def start(self) -> None:
        """Inicia el ciclo de scraping para este orquestador."""
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
                logger.info(
                    "Iniciando ciclo de scraping para '%s'…",
                    self._filter_label or "TODOS",
                )
                await self._execute_cycle()

                if not self._running:
                    break

                delay = settings.CYCLE_DELAY_SECONDS
                logger.info("Ciclo completado. Próximo en %ds.", delay)
                await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info(
                "Bucle cancelado por señal para '%s'.",
                self._filter_label or "TODOS",
            )
        finally:
            await self._db_disconnect()

        logger.info(
            "Orquestador '%s' detenido.",
            self._filter_label or "TODOS",
        )

    def stop(self) -> None:
        """Señal de parada graceful."""
        logger.info(
            "Señal de parada recibida para '%s'. Finalizando ciclo actual…",
            self._filter_label or "TODOS",
        )
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# ConcurrentFilterManager
# ─────────────────────────────────────────────────────────────────────────────

class ConcurrentFilterManager:
    """
    Gestiona la ejecución concurrente de múltiples FILTERs.

    CAMOUFOX: Añade delay configurable entre lanzamientos de browsers
    para evitar picos de RAM al iniciar múltiples instancias de Camoufox
    simultáneamente (cada una consume ~1.5-2GB RAM).

    SUPABASE: Cada filtro reclama sus propias keywords con bloqueo atómico,
    evitando que dos workers procesen la misma keyword.
    """

    def __init__(self, filters: list[str] | None = None) -> None:
        self._filters            = filters
        self._tasks:         list[asyncio.Task]          = []
        self._orchestrators: list[ScraperOrchestrator]  = []
        self._browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

    async def _run_filter_task(
        self,
        orchestrator: ScraperOrchestrator,
        filter_label: str | None,
    ) -> None:
        """Ejecuta un orquestador como tarea asíncrona."""
        try:
            await orchestrator.start()
        except Exception as exc:
            logger.error(
                "Error crítico en tarea para filter='%s': %s",
                filter_label or "TODOS", exc, exc_info=True,
            )

    async def start(self) -> None:
        """Inicia todas las tareas de filtros concurrentemente."""
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
            ScraperOrchestrator(
                filter_label=flt,
                browser_semaphore=self._browser_semaphore,
            )
            for flt in filters_to_run
        ]

        # ── CAMOUFOX: Lanzar tareas con delay entre ellas ──
        # Evita picos de RAM al iniciar múltiples instancias de Camoufox
        # simultáneamente. El delay es configurable en settings.CAMOUFOX_LAUNCH_DELAY
        launch_delay = settings.CAMOUFOX_LAUNCH_DELAY

        for idx, (orch, flt) in enumerate(zip(self._orchestrators, filters_to_run)):
            task = asyncio.create_task(
                self._run_filter_task(orch, flt),
                name=f"filter-{flt or 'all'}",
            )
            self._tasks.append(task)

            # Delay entre lanzamientos (excepto después del último)
            if idx < len(self._orchestrators) - 1 and launch_delay > 0:
                logger.debug(
                    "Delay de %.1fs antes de lanzar siguiente browser (%d/%d)...",
                    launch_delay, idx + 1, len(self._orchestrators),
                )
                await asyncio.sleep(launch_delay)

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
        """Detiene todos los orquestadores gracefulmente."""
        for orchestrator in self._orchestrators:
            orchestrator.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Punto de entrada principal."""
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
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("camoufox").setLevel(logging.WARNING)

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