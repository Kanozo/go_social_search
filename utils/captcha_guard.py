"""
utils/captcha_guard.py

Detector de CAPTCHA/robot-check de Google con alerta auditiva multi-plataforma.

DISEÑO:
    CaptchaDetector   →  inspecciona la Page de Playwright y determina si Google
                         ha interceptado la navegación con un challenge.
    CaptchaAlerter    →  emite señal auditiva y loguea el evento con contexto.
    CaptchaError      →  excepción semántica que propaga el fallo hacia arriba.

ESTRATEGIA DE AUDIO (fallback en cascada, sin dependencias obligatorias):
    1. winsound.Beep()           — Windows nativo, sin deps.
    2. afplay (macOS)            — reproductor nativo de macOS.
    3. paplay / aplay (Linux)    — PulseAudio / ALSA.
    4. beep (Linux, opcional)    — comando del paquete `beep`.
    5. print('\\a')              — bell de terminal, funciona siempre.
    6. playsound (cross-platform)— solo si está instalado: pip install playsound.

INTEGRACIÓN EN google_cse_automator.py:
    Llamar ``await CaptchaDetector.check(page, keyword)`` en los puntos críticos.
    Si detecta CAPTCHA lanza ``CaptchaError``, el caller la captura y decide si
    pausar, reintentar, o escalar.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
import sys
from typing import Final

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selectores y patrones de detección
# ---------------------------------------------------------------------------

# URLs que Google usa para sus páginas de bloqueo / CAPTCHA
_CAPTCHA_URL_PATTERNS: Final[tuple[str, ...]] = (
    "/sorry/index",
    "google.com/sorry",
    "recaptcha",
    "sorry.google.com",
    "/sorry?",
)

# Títulos de página que indican bloqueo
_CAPTCHA_TITLE_PATTERNS: Final[tuple[str, ...]] = (
    "before you continue",
    "unusual traffic",
    "our systems have detected",
    "robot",
    "captcha",
    "verify you're not a robot",
    "sorry...",
)

# Selectores DOM que aparecen en páginas de CAPTCHA de Google
_CAPTCHA_SELECTORS: Final[tuple[str, ...]] = (
    "#captcha",
    "#recaptcha",
    ".g-recaptcha",
    "iframe[src*='recaptcha']",
    "iframe[src*='google.com/recaptcha']",
    "#captcha-form",
    "form[action*='/sorry/']",
    "[data-sitekey]",                        # reCAPTCHA v2/v3
)

# Selector que DEBE existir si todo va bien (ausencia = sospecha)
_EXPECTED_CSE_SELECTOR: Final[str] = ".gsc-results-wrapper-visible, .gsc-webResult"


# ---------------------------------------------------------------------------
# Excepción semántica
# ---------------------------------------------------------------------------

class CaptchaError(Exception):
    """
    Se lanza cuando se detecta un CAPTCHA o robot-check de Google.

    Attributes:
        keyword:    Término de búsqueda que disparó la detección.
        reason:     Descripción técnica de por qué se detectó (URL, selector, título).
        page_url:   URL actual de la página al momento de la detección.
        screenshot: Bytes del screenshot (PNG) o None si falló.
    """

    def __init__(
        self,
        keyword: str,
        reason: str,
        page_url: str = "",
        screenshot: bytes | None = None,
    ) -> None:
        self.keyword = keyword
        self.reason = reason
        self.page_url = page_url
        self.screenshot = screenshot
        super().__init__(
            f"CAPTCHA detectado [keyword='{keyword}'] → {reason} | URL: {page_url}"
        )


# ---------------------------------------------------------------------------
# Alertador auditivo
# ---------------------------------------------------------------------------

class CaptchaAlerter:
    """
    Emite una señal auditiva repetida para alertar al operador.

    Usa una cascada de métodos según el SO, sin dependencias obligatorias.
    Todos los métodos son síncronos internamente; se llaman desde un
    ``asyncio.get_event_loop().run_in_executor`` para no bloquear el loop.
    """

    # Parámetros por defecto de la señal
    DEFAULT_FREQUENCY_HZ: int = 880       # La4 — frecuencia audible clara
    DEFAULT_DURATION_MS: int = 400        # Duración de cada pitido
    DEFAULT_REPETITIONS: int = 5          # Pitidos consecutivos
    DEFAULT_PAUSE_MS: int = 200           # Pausa entre pitidos

    @classmethod
    def _beep_windows(cls) -> bool:
        """Pitido nativo en Windows con winsound."""
        try:
            import winsound  # type: ignore[import]
            for _ in range(cls.DEFAULT_REPETITIONS):
                winsound.Beep(cls.DEFAULT_FREQUENCY_HZ, cls.DEFAULT_DURATION_MS)
                import time
                time.sleep(cls.DEFAULT_PAUSE_MS / 1000)
            return True
        except (ImportError, RuntimeError):
            return False

    @classmethod
    def _beep_macos(cls) -> bool:
        """Pitido en macOS usando afplay con un archivo de sonido del sistema."""
        sounds = [
            "/System/Library/Sounds/Sosumi.aiff",
            "/System/Library/Sounds/Ping.aiff",
            "/System/Library/Sounds/Basso.aiff",
            "/System/Library/Sounds/Hero.aiff",
        ]
        for sound_path in sounds:
            try:
                for _ in range(cls.DEFAULT_REPETITIONS):
                    result = subprocess.run(
                        ["afplay", sound_path],
                        capture_output=True,
                        timeout=3,
                    )
                    if result.returncode == 0:
                        import time
                        time.sleep(cls.DEFAULT_PAUSE_MS / 1000)
                return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return False

    @classmethod
    def _beep_linux_paplay(cls) -> bool:
        """Pitido en Linux via PulseAudio (paplay)."""
        sounds = [
            "/usr/share/sounds/freedesktop/stereo/bell.oga",
            "/usr/share/sounds/ubuntu/stereo/bell.ogg",
            "/usr/share/sounds/alsa/Front_Center.wav",
        ]
        for sound_path in sounds:
            try:
                for _ in range(cls.DEFAULT_REPETITIONS):
                    result = subprocess.run(
                        ["paplay", "--volume=65536", sound_path],
                        capture_output=True,
                        timeout=3,
                    )
                    if result.returncode == 0:
                        import time
                        time.sleep(cls.DEFAULT_PAUSE_MS / 1000)
                return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return False

    @classmethod
    def _beep_linux_aplay(cls) -> bool:
        """Pitido en Linux via ALSA (aplay)."""
        try:
            for _ in range(cls.DEFAULT_REPETITIONS):
                subprocess.run(
                    ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"],
                    capture_output=True,
                    timeout=3,
                )
                import time
                time.sleep(cls.DEFAULT_PAUSE_MS / 1000)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @classmethod
    def _beep_linux_beep_cmd(cls) -> bool:
        """Pitido en Linux via comando `beep` (requiere: apt install beep)."""
        try:
            subprocess.run(
                [
                    "beep",
                    "-f", str(cls.DEFAULT_FREQUENCY_HZ),
                    "-l", str(cls.DEFAULT_DURATION_MS),
                    "-r", str(cls.DEFAULT_REPETITIONS),
                    "-D", str(cls.DEFAULT_PAUSE_MS),
                ],
                capture_output=True,
                timeout=10,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @classmethod
    def _beep_terminal_bell(cls) -> bool:
        """
        Fallback universal: carácter BEL (\\a) repetido.

        Funciona en cualquier terminal que tenga el bell de audio habilitado.
        En terminales modernas puede ser visual en lugar de auditivo.
        """
        for _ in range(cls.DEFAULT_REPETITIONS):
            sys.stdout.write("\a")
            sys.stdout.flush()
            import time
            time.sleep((cls.DEFAULT_DURATION_MS + cls.DEFAULT_PAUSE_MS) / 1000)
        return True

    @classmethod
    def _beep_playsound(cls) -> bool:
        """
        Alternativa cross-platform con la librería `playsound`.
        Requiere: pip install playsound==1.2.2
        """
        try:
            from playsound import playsound  # type: ignore[import]
            # Genera un WAV mínimo en memoria con tono de 880Hz
            import struct
            import wave
            import tempfile
            import os
            import math

            sample_rate = 44100
            duration_s = cls.DEFAULT_DURATION_MS / 1000
            num_samples = int(sample_rate * duration_s)
            freq = cls.DEFAULT_FREQUENCY_HZ

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            with wave.open(tmp_path, "w") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                frames = struct.pack(
                    f"<{num_samples}h",
                    *[
                        int(32767 * math.sin(2 * math.pi * freq * i / sample_rate))
                        for i in range(num_samples)
                    ],
                )
                wav_file.writeframes(frames)

            for _ in range(cls.DEFAULT_REPETITIONS):
                playsound(tmp_path)
                import time
                time.sleep(cls.DEFAULT_PAUSE_MS / 1000)

            os.unlink(tmp_path)
            return True
        except Exception:
            return False

    @classmethod
    def emit(cls) -> None:
        """
        Intenta emitir la alerta auditiva usando la cascada de métodos.

        El primer método que tenga éxito termina la cascada.
        Siempre termina con el bell de terminal como garantía mínima.
        """
        os_name = platform.system()
        logger.warning("🔔 Emitiendo alerta auditiva de CAPTCHA...")

        strategies = []
        if os_name == "Windows":
            strategies = [cls._beep_windows, cls._beep_playsound, cls._beep_terminal_bell]
        elif os_name == "Darwin":
            strategies = [cls._beep_macos, cls._beep_playsound, cls._beep_terminal_bell]
        else:  # Linux y otros
            strategies = [
                cls._beep_linux_beep_cmd,
                cls._beep_linux_paplay,
                cls._beep_linux_aplay,
                cls._beep_playsound,
                cls._beep_terminal_bell,
            ]

        for strategy in strategies:
            try:
                if strategy():
                    logger.debug(f"Alerta auditiva emitida con: {strategy.__name__}")
                    return
            except Exception as exc:
                logger.debug(f"Estrategia {strategy.__name__} falló: {exc}")

        # Garantía absoluta: el bell de terminal siempre se ejecuta
        cls._beep_terminal_bell()


# ---------------------------------------------------------------------------
# Detector de CAPTCHA
# ---------------------------------------------------------------------------

class CaptchaDetector:
    """
    Detecta si la página actual de Playwright es un robot-check de Google.

    Uso::

        try:
            await CaptchaDetector.check(page, keyword="cuba")
        except CaptchaError as e:
            logger.critical(str(e))
            # manejar pausa/reintento
    """

    @classmethod
    async def check(
        cls,
        page: Page,
        keyword: str,
        take_screenshot: bool = True,
    ) -> None:
        """
        Inspecciona la página y lanza ``CaptchaError`` si detecta un challenge.

        Estrategia de detección (orden de menor a mayor coste):
            1. URL de la página actual (string match, O(n)).
            2. Título de la página (string match, O(n)).
            3. Presencia de selectores DOM de CAPTCHA (query al DOM).
            4. Ausencia de selectores CSE esperados (query al DOM).

        Args:
            page:            Página de Playwright a inspeccionar.
            keyword:         Keyword activa para contexto en el error.
            take_screenshot: Si True, adjunta screenshot a la excepción.

        Raises:
            CaptchaError: Si se detecta cualquier indicador de CAPTCHA.
        """
        current_url = page.url
        reason: str | None = None

        # ── 1. Detección por URL ─────────────────────────────────────────
        url_lower = current_url.lower()
        for pattern in _CAPTCHA_URL_PATTERNS:
            if pattern in url_lower:
                reason = f"URL contiene patrón de bloqueo: '{pattern}'"
                break

        # ── 2. Detección por título ──────────────────────────────────────
        if not reason:
            try:
                title = (await page.title()).lower()
                for pattern in _CAPTCHA_TITLE_PATTERNS:
                    if pattern in title:
                        reason = f"Título de página indica bloqueo: '{title[:60]}'"
                        break
            except Exception:
                pass

        # ── 3. Detección por selectores de CAPTCHA ───────────────────────
        if not reason:
            for selector in _CAPTCHA_SELECTORS:
                try:
                    element = page.locator(selector).first
                    if await element.is_visible(timeout=500):
                        reason = f"Selector de CAPTCHA encontrado: '{selector}'"
                        break
                except Exception:
                    continue

        # ── 4. Ausencia de elementos CSE esperados ───────────────────────
        if not reason:
            try:
                cse_visible = await page.locator(_EXPECTED_CSE_SELECTOR).count()
                if cse_visible == 0:
                    # Doble check: si tampoco hay CAPTCHA conocido, no alertar
                    # (podría ser una búsqueda sin resultados legítima)
                    input_exists = await page.locator("input.gsc-input").count()
                    if input_exists == 0:
                        reason = "Ni elementos CSE ni input de búsqueda encontrados"
            except Exception:
                pass

        if not reason:
            return  # Todo bien, no hay CAPTCHA

        # ── CAPTCHA DETECTADO ────────────────────────────────────────────
        screenshot_bytes: bytes | None = None
        if take_screenshot:
            try:
                screenshot_bytes = await page.screenshot(full_page=False)
                logger.info("Screenshot del CAPTCHA capturado en memoria.")
            except Exception as exc:
                logger.debug(f"No se pudo capturar screenshot: {exc}")

        # Log prominente en consola para que el operador lo vea
        separator = "=" * 70
        logger.critical(separator)
        logger.critical("🚨  CAPTCHA / ROBOT-CHECK DETECTADO  🚨")
        logger.critical(f"   Keyword  : {keyword!r}")
        logger.critical(f"   Razón    : {reason}")
        logger.critical(f"   URL      : {current_url}")
        logger.critical("   El operador debe intervenir manualmente.")
        logger.critical(separator)

        # Alerta auditiva en hilo separado (no bloquea el event loop)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, CaptchaAlerter.emit)

        raise CaptchaError(
            keyword=keyword,
            reason=reason,
            page_url=current_url,
            screenshot=screenshot_bytes,
        )

    @classmethod
    async def wait_for_human_resolution(
        cls,
        page: Page,
        keyword: str,
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
    ) -> bool:
        """
        Espera activamente a que el operador resuelva el CAPTCHA manualmente.

        Hace polling del estado de la página hasta que los elementos CSE
        vuelvan a estar presentes (operador resolvió) o se agote el timeout.

        Args:
            page:          Página en espera.
            keyword:       Keyword activa (para logging).
            poll_interval: Segundos entre checks de estado.
            max_wait:      Segundos máximos a esperar antes de abortar.

        Returns:
            True si el operador resolvió el CAPTCHA.
            False si se agotó el tiempo de espera.
        """
        logger.warning(
            f"Esperando resolución manual del CAPTCHA (máx {max_wait:.0f}s)... "
            "Resuelve el desafío en el navegador."
        )
        elapsed = 0.0
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                # Si los elementos CSE reaparecen, el CAPTCHA fue resuelto
                cse_count = await page.locator(_EXPECTED_CSE_SELECTOR).count()
                if cse_count > 0:
                    logger.info(
                        f"✅ CAPTCHA resuelto por el operador "
                        f"(keyword='{keyword}', elapsed={elapsed:.0f}s)."
                    )
                    return True
            except Exception:
                pass
            remaining = max_wait - elapsed
            logger.info(
                f"Aún esperando resolución manual... "
                f"{remaining:.0f}s restantes."
            )
        logger.error(
            f"Timeout esperando resolución de CAPTCHA para keyword='{keyword}'."
        )
        return False