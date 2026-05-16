"""
utils/captcha_guard.py
Detección y resolución automática de CAPTCHAs en navegadores automatizados.

Implementa un sistema multi-señal que detecta:
  - reCAPTCHA v2/v3 (Google)
  - hCaptcha (Cloudflare y otros)
  - Cloudflare Challenge (Turnstile y legacy JS challenge)
  - Páginas "unusual traffic" de Google
  - Checkpoints de Facebook / Instagram
  - Rate limiting genérico (429, 403 con mensaje específico)

Y un solver de checkbox que intenta resolver automáticamente la variante
más simple de cada CAPTCHA (el tick "I'm not a robot" / "I am human"):

  ┌──────────────────────────────────────────────────────────────┐
  │  Flujo de resolución automática                              │
  │                                                              │
  │  check() detecta CAPTCHA                                     │
  │       ↓                                                      │
  │  CaptchaAutosolver.try_solve_checkbox()                      │
  │       ├─ reCAPTCHA v2  → iframe anchor → #recaptcha-anchor  │
  │       ├─ hCaptcha       → iframe main   → #checkbox          │
  │       └─ CF Turnstile  → iframe cf      → input[type=check] │
  │       ↓                                                      │
  │  Verificar si se resolvió (check() ya no lanza)              │
  │       ├─ Sí → continuar scraping                             │
  │       └─ No → lanzar CaptchaError (nueva identidad / humano) │
  └──────────────────────────────────────────────────────────────┘

Limitación conocida: si tras el checkbox aparece un reto visual
(selección de imágenes), el solver no puede resolverlo y escala
al flujo de rotación de identidad normal.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from enum import Enum, auto
from typing import TYPE_CHECKING

from playwright.async_api import FrameLocator, Page, TimeoutError as PlaywrightTimeoutError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class CaptchaError(Exception):
    """
    Se lanza cuando se detecta un CAPTCHA o página de bloqueo.

    Attributes:
        page_url:    URL de la página donde se detectó el bloqueo.
        signal:      Descripción de la señal que disparó la detección.
    """
    def __init__(self, page_url: str = "", signal: str = "") -> None:
        self.page_url = page_url
        self.signal = signal
        super().__init__(f"CAPTCHA/block detected [{signal}] at: {page_url}")


# ─────────────────────────────────────────────────────────────────────────────
# Patrones de detección
# ─────────────────────────────────────────────────────────────────────────────

# Fragmentos de URL que indican bloqueo o challenge
_BLOCKED_URL_PATTERNS: tuple[str, ...] = (
    "/sorry/",          # Google "unusual traffic"
    "sorry.google",
    "/checkpoint/",     # Facebook checkpoint
    "/login/checkpoint",
    "challenge",        # Cloudflare challenge genérico
    "captcha",
    "bot_check",
    "security_check",
    "verify",
    "antibot",
)

# Títulos de página que indican bloqueo
_BLOCKED_TITLE_PATTERNS: tuple[str, ...] = (
    "captcha",
    "unusual traffic",
    "verify",
    "security check",
    "bot check",
    "access denied",
    "blocked",
    "challenge",
    "attention required",  # Cloudflare
    "just a moment",       # Cloudflare Turnstile
    "error 429",
    "too many requests",
)

# Selectores CSS que indican presencia de widgets de CAPTCHA
_CAPTCHA_SELECTORS: tuple[str, ...] = (
    # reCAPTCHA
    ".g-recaptcha",
    "#recaptcha",
    "iframe[src*='recaptcha']",
    "iframe[src*='google.com/recaptcha']",
    # hCaptcha
    ".h-captcha",
    "iframe[src*='hcaptcha.com']",
    "[data-sitekey]",
    # Cloudflare
    "#challenge-form",
    "#cf-challenge-running",
    ".cf-browser-verification",
    "#cf-wrapper",
    "iframe[src*='challenges.cloudflare.com']",
    # Facebook / Instagram checkpoint
    "#checkpoint",
    ".uiInterstitial",
    "form[action*='checkpoint']",
    # Genéricos
    "[id*='captcha']",
    "[class*='captcha']",
    "[name*='captcha']",
)

# Texto en el body que indica bloqueo (buscar en las primeras 2000 chars)
_BLOCKED_BODY_PATTERNS: tuple[str, ...] = (
    "unusual traffic",
    "automated queries",
    "our systems have detected",
    "too many requests",
    "access to this page has been denied",
    "checking your browser",
    "please verify you are a human",
    "ddos protection by cloudflare",
    "ray id",  # Cloudflare ray ID al final de páginas de error
)


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────

class CaptchaDetector:
    """
    Clase estática con métodos de detección multi-señal.

    Diseñada para llamarse en múltiples puntos del flujo de scraping:
    después de cada ``page.goto()``, después de enviar formularios
    y después de esperar carga de resultados.
    """

    @staticmethod
    async def check(page: Page, keyword: str = "") -> None:
        """
        Inspecciona la página actual con múltiples señales de detección.

        Orden de checks (de más a menos costoso):
          1. URL path (instantáneo)
          2. Título de página (instantáneo)
          3. Selectores CSS (leve overhead de DOM query)
          4. Texto del body (moderado, busca en los primeros 3000 chars)

        Args:
            page:    Página de Playwright activa.
            keyword: Keyword actual (solo para logging).

        Raises:
            CaptchaError: Si cualquier señal indica bloqueo o CAPTCHA.
        """
        current_url = page.url

        # 1. Verificar URL ────────────────────────────────────────────────────
        url_lower = current_url.lower()
        for pattern in _BLOCKED_URL_PATTERNS:
            if pattern in url_lower:
                logger.warning(
                    "[CAPTCHA] URL pattern '%s' detected | url=%s | kw='%s'",
                    pattern, current_url, keyword,
                )
                raise CaptchaError(page_url=current_url, signal=f"url:{pattern}")

        # 2. Verificar título ─────────────────────────────────────────────────
        try:
            title = (await page.title()).lower()
            for pattern in _BLOCKED_TITLE_PATTERNS:
                if pattern in title:
                    logger.warning(
                        "[CAPTCHA] Title pattern '%s' detected | title='%s' | kw='%s'",
                        pattern, title, keyword,
                    )
                    raise CaptchaError(page_url=current_url, signal=f"title:{pattern}")
        except CaptchaError:
            raise
        except Exception as exc:
            logger.debug("Could not read page title: %s", exc)

        # 3. Verificar selectores CSS ─────────────────────────────────────────
        for selector in _CAPTCHA_SELECTORS:
            try:
                element = page.locator(selector).first
                # is_visible() con timeout=0 = no esperar, solo consultar
                if await element.is_visible():
                    logger.warning(
                        "[CAPTCHA] Selector '%s' visible | url=%s | kw='%s'",
                        selector, current_url, keyword,
                    )
                    raise CaptchaError(page_url=current_url, signal=f"selector:{selector}")
            except CaptchaError:
                raise
            except Exception:
                pass  # Selector no existe = sin bloqueo

        # 4. Verificar texto del body ─────────────────────────────────────────
        try:
            body_text = await page.evaluate(
                "document.body ? document.body.innerText.substring(0, 3000).toLowerCase() : ''"
            )
            for pattern in _BLOCKED_BODY_PATTERNS:
                if pattern in body_text:
                    logger.warning(
                        "[CAPTCHA] Body pattern '%s' detected | url=%s | kw='%s'",
                        pattern, current_url, keyword,
                    )
                    raise CaptchaError(page_url=current_url, signal=f"body:{pattern}")
        except CaptchaError:
            raise
        except Exception as exc:
            logger.debug("Could not read page body: %s", exc)

    @staticmethod
    async def wait_for_human_resolution(
        page: Page,
        keyword: str,
        max_wait: float = 300.0,
    ) -> bool:
        """
        Espera a que un humano resuelva el CAPTCHA manualmente.

        Útil en modo ``headless=False`` cuando se prefiere intervención
        humana en lugar de rotar identidad automáticamente.

        Polling: verifica cada 3 segundos si el CAPTCHA desapareció.

        Args:
            page:     Página de Playwright activa.
            keyword:  Keyword actual (para logging).
            max_wait: Segundos máximos a esperar. Default 300s (5 min).

        Returns:
            True si el CAPTCHA se resolvió, False si se agotó el tiempo.
        """
        logger.info(
            "[CAPTCHA] Waiting up to %.0fs for manual resolution | kw='%s'",
            max_wait, keyword,
        )
        elapsed = 0.0
        poll_interval = 3.0

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                # Si check() no lanza, el CAPTCHA desapareció
                await CaptchaDetector.check(page, keyword)
                logger.info(
                    "[CAPTCHA] Manually resolved after %.0fs | kw='%s'",
                    elapsed, keyword,
                )
                return True
            except CaptchaError:
                remaining = max_wait - elapsed
                logger.debug(
                    "[CAPTCHA] Still waiting... %.0fs remaining | kw='%s'",
                    remaining, keyword,
                )

        logger.error(
            "[CAPTCHA] Timeout: CAPTCHA not resolved after %.0fs | kw='%s'",
            max_wait, keyword,
        )
        return False

    @staticmethod
    async def intercept_response_errors(page: Page) -> None:
        """
        Registra un listener de respuestas HTTP para detectar 429/403.

        Debe llamarse una vez por página, idealmente justo después de crearla.
        Los errores HTTP se loggean pero no lanzan excepción directamente
        (se delega la detección al siguiente ``check()``).

        Args:
            page: Página de Playwright activa.
        """
        def _on_response(response: object) -> None:
            # Importación inline para evitar circular import
            from playwright.async_api import Response as PlaywrightResponse
            resp: PlaywrightResponse = response  # type: ignore[assignment]
            status = resp.status
            if status in (429, 403):
                logger.warning(
                    "[HTTP] Status %d received | url=%s",
                    status, resp.url[:100],
                )
            elif status >= 500:
                logger.debug(
                    "[HTTP] Server error %d | url=%s",
                    status, resp.url[:100],
                )

        page.on("response", _on_response)


# ─────────────────────────────────────────────────────────────────────────────
# Tipos de CAPTCHA
# ─────────────────────────────────────────────────────────────────────────────

class CaptchaType(Enum):
    """Variantes de CAPTCHA con checkbox resoluble automáticamente."""
    RECAPTCHA_V2   = auto()   # Google "I'm not a robot"
    HCAPTCHA       = auto()   # hCaptcha "I am human"
    CF_TURNSTILE   = auto()   # Cloudflare Turnstile
    UNKNOWN        = auto()   # No identificado / no resoluble


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de movimiento humano (inline para no crear dependencia circular)
# ─────────────────────────────────────────────────────────────────────────────

async def _arc_move(page: Page, target_x: float, target_y: float) -> None:
    """
    Mueve el ratón hasta (target_x, target_y) con una curva de Bézier cúbica.

    Réplica local de ``human_move_to`` para evitar importación circular entre
    utils/ y anti_detection/.

    Args:
        page:     Página de Playwright activa.
        target_x: Coordenada X de destino en viewport.
        target_y: Coordenada Y de destino en viewport.
    """
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    start_x = random.uniform(viewport["width"]  * 0.1, viewport["width"]  * 0.9)
    start_y = random.uniform(viewport["height"] * 0.2, viewport["height"] * 0.7)

    # Puntos de control con jitter ±35px para curva orgánica
    ctrl1 = (
        start_x + (target_x - start_x) * random.uniform(0.2, 0.45) + random.uniform(-35, 35),
        start_y + (target_y - start_y) * random.uniform(0.1, 0.40) + random.uniform(-35, 35),
    )
    ctrl2 = (
        start_x + (target_x - start_x) * random.uniform(0.55, 0.80) + random.uniform(-35, 35),
        start_y + (target_y - start_y) * random.uniform(0.60, 0.90) + random.uniform(-35, 35),
    )

    num_steps = random.randint(16, 30)
    for i in range(num_steps + 1):
        t = i / num_steps
        inv = 1 - t
        # Cúbica de Bézier con easing senoidal para velocidad variable
        t_ease = (1 - math.cos(math.pi * t)) / 2
        x = inv**3 * start_x + 3*inv**2*t * ctrl1[0] + 3*inv*t**2 * ctrl2[0] + t**3 * target_x
        y = inv**3 * start_y + 3*inv**2*t * ctrl1[1] + 3*inv*t**2 * ctrl2[1] + t**3 * target_y
        await page.mouse.move(x, y)
        # Velocidad: más lenta al inicio y al final (aceleración natural)
        speed_factor = 1.6 - math.sin(math.pi * t_ease)
        await asyncio.sleep(random.uniform(0.004, 0.018) * speed_factor)


async def _human_click_in_frame(
    page: Page,
    frame: FrameLocator,
    selector: str,
    timeout_ms: int = 5_000,
) -> None:
    """
    Localiza un elemento dentro de un iframe y lo clicka con comportamiento humano.

    Proceso:
      1. Esperar a que el elemento sea visible dentro del frame
      2. Obtener su bounding box relativa al viewport principal
      3. Mover el ratón con curva de Bézier hasta el centro del checkbox
      4. Pausa de "apunte" antes del click
      5. Click con posición ligeramente aleatoria dentro del elemento
      6. Micro-pausa post-click (reacción natural)

    Args:
        page:       Página principal de Playwright (necesaria para mouse global).
        frame:      FrameLocator del iframe que contiene el elemento.
        selector:   Selector CSS del elemento dentro del iframe.
        timeout_ms: Milisegundos máximos esperando visibilidad. Default 5s.

    Raises:
        PlaywrightTimeoutError: Si el elemento no aparece en el timeout.
    """
    element = frame.locator(selector).first
    await element.wait_for(state="visible", timeout=timeout_ms)

    # Obtener posición absoluta en el viewport para mover el ratón desde la página principal
    box = await element.bounding_box()
    if box:
        # Punto de click aleatorio dentro del elemento (evitar siempre el centro exacto)
        click_x = box["x"] + box["width"]  * random.uniform(0.30, 0.70)
        click_y = box["y"] + box["height"] * random.uniform(0.30, 0.70)

        # Movimiento humano con arco hasta el checkbox
        await _arc_move(page, click_x, click_y)

        # Pausa de "apunte": el humano centra el cursor antes de hacer click
        await asyncio.sleep(random.uniform(0.12, 0.38))

        # Click en la posición calculada
        await page.mouse.click(click_x, click_y)
    else:
        # Fallback: click directo por Playwright si no hay bounding box
        await element.click()

    # Micro-pausa post-click (reacción natural del usuario)
    await asyncio.sleep(random.uniform(0.08, 0.22))


# ─────────────────────────────────────────────────────────────────────────────
# CaptchaAutosolver
# ─────────────────────────────────────────────────────────────────────────────

class CaptchaAutosolver:
    """
    Intenta resolver automáticamente CAPTCHAs de tipo checkbox.

    Cubre la variante más simple (y la más frecuente en flujos normales):
    el tick "I'm not a robot" / "Verify you are human". No resuelve retos
    de imágenes, audio, ni puzzles deslizantes.

    Estrategia de detección de iframes:
      Los widgets de CAPTCHA se renderizan dentro de iframes cruzados (cross-origin),
      por lo que Playwright requiere ``frame_locator()`` para acceder a su DOM.
      Cada proveedor usa URLs de iframe distintas:

      - reCAPTCHA v2 anchor: ``recaptcha/api2/anchor`` o ``recaptcha/enterprise/anchor``
      - hCaptcha:            ``assets.hcaptcha.com`` o ``newassets.hcaptcha.com``
      - Cloudflare Turnstile: ``challenges.cloudflare.com``

    Estrategia post-click:
      Después de clickar el checkbox, esperamos 2-6 segundos y volvemos a
      llamar a ``CaptchaDetector.check()``. Si ya no lanza, el CAPTCHA se
      resolvió. Si lanza de nuevo, puede ser porque:
        a) Apareció un reto visual (imágenes) → no resoluble automáticamente
        b) El click no registró → reintentamos una vez más
        c) El sistema de detección rechazó la interacción → escalar a nueva identidad
    """

    # ── reCAPTCHA v2 ──────────────────────────────────────────────────────────

    # Selectores del iframe anchor (el que contiene el checkbox inicial)
    _RECAPTCHA_ANCHOR_IFRAME_SELECTORS: tuple[str, ...] = (
        "iframe[src*='recaptcha/api2/anchor']",
        "iframe[src*='recaptcha/enterprise/anchor']",
        "iframe[title='reCAPTCHA']",
    )
    # Selector del checkbox dentro del iframe anchor
    _RECAPTCHA_CHECKBOX_SELECTOR: str = "#recaptcha-anchor"
    # Selector del estado "resuelto" dentro del iframe anchor
    _RECAPTCHA_SOLVED_SELECTOR:   str = ".recaptcha-checkbox-checked"
    # Iframe del reto visual (si aparece, el checkbox no bastó)
    _RECAPTCHA_CHALLENGE_IFRAME:  str = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"

    # ── hCaptcha ──────────────────────────────────────────────────────────────

    _HCAPTCHA_IFRAME_SELECTORS: tuple[str, ...] = (
        "iframe[src*='assets.hcaptcha.com']",
        "iframe[src*='newassets.hcaptcha.com']",
        "iframe[data-hcaptcha-widget-id]",
        "iframe[title='Main content of the hCaptcha challenge']",
    )
    _HCAPTCHA_CHECKBOX_SELECTOR: str = "#checkbox"
    # hCaptcha marca el checkbox con aria-checked="true" cuando se resuelve
    _HCAPTCHA_SOLVED_SELECTOR:   str = "#checkbox[aria-checked='true']"

    # ── Cloudflare Turnstile ──────────────────────────────────────────────────

    _CF_TURNSTILE_IFRAME_SELECTORS: tuple[str, ...] = (
        "iframe[src*='challenges.cloudflare.com']",
        "iframe[src*='cf-chl-widget']",
    )
    # Turnstile moderno usa un input checkbox oculto que se activa al click
    _CF_TURNSTILE_CHECKBOX_SELECTORS: tuple[str, ...] = (
        "input[type='checkbox']",         # Turnstile estándar
        ".cf-turnstile-part-checkbox",    # Variante legacy
        "[id*='cf-chl-widget']",
    )
    _CF_TURNSTILE_SOLVED_SELECTOR: str = "input[type='checkbox'][checked]"

    # ── Helpers privados ──────────────────────────────────────────────────────

    @staticmethod
    async def _find_iframe_frame_locator(
        page: Page,
        selectors: tuple[str, ...],
        timeout_ms: int = 4_000,
    ) -> FrameLocator | None:
        """
        Busca el primer iframe que coincida con alguno de los selectores.

        Args:
            page:       Página principal.
            selectors:  Selectores CSS a probar en orden.
            timeout_ms: Timeout por selector. Total máximo = len(selectors) * timeout_ms.

        Returns:
            ``FrameLocator`` del primer iframe encontrado, o None si ninguno existe.
        """
        for selector in selectors:
            try:
                iframe_el = page.locator(selector).first
                await iframe_el.wait_for(state="attached", timeout=timeout_ms)
                # Verificar que el iframe está presente (attached) aunque no visible
                if await iframe_el.count() > 0:
                    return page.frame_locator(selector)
            except PlaywrightTimeoutError:
                continue
            except Exception as exc:
                logger.debug("_find_iframe_frame_locator [%s]: %s", selector, exc)
        return None

    @staticmethod
    async def _is_solved_in_frame(
        frame: FrameLocator,
        solved_selector: str,
        timeout_ms: int = 500,
    ) -> bool:
        """
        Verifica si el estado "resuelto" está presente en el iframe.

        Args:
            frame:           FrameLocator del iframe del CAPTCHA.
            solved_selector: Selector CSS del elemento de estado resuelto.
            timeout_ms:      Timeout máximo de verificación.

        Returns:
            True si el selector de "resuelto" es visible.
        """
        try:
            el = frame.locator(solved_selector).first
            return await el.is_visible(timeout=timeout_ms)
        except Exception:
            return False

    @staticmethod
    async def _visual_challenge_appeared(page: Page) -> bool:
        """
        Detecta si apareció el iframe del reto visual de reCAPTCHA.

        Si el bframe está presente y visible, el checkbox no bastó y se
        requiere seleccionar imágenes — no resoluble automáticamente.

        Args:
            page: Página principal.

        Returns:
            True si hay un reto visual activo.
        """
        try:
            bframe = page.locator(CaptchaAutosolver._RECAPTCHA_CHALLENGE_IFRAME).first
            return await bframe.is_visible(timeout=500)
        except Exception:
            return False

    # ── Solvers por proveedor ─────────────────────────────────────────────────

    @staticmethod
    async def _solve_recaptcha_v2(page: Page) -> bool:
        """
        Intenta resolver reCAPTCHA v2 haciendo click en el checkbox "I'm not a robot".

        Flujo interno:
          1. Localizar el iframe ``anchor`` (contiene el checkbox inicial)
          2. Esperar a que el checkbox esté visible e interactuable
          3. Pausa pre-click: simula que el usuario lee brevemente el CAPTCHA
          4. Click humano con arco de Bézier + posición aleatoria
          5. Esperar 2-5s para que el sistema de reCAPTCHA evalúe el click
          6. Verificar si apareció el iframe ``bframe`` (reto visual)
             → Si sí: no resoluble, devolver False
          7. Verificar clase ``.recaptcha-checkbox-checked`` en el iframe
             → Si presente: resuelto, devolver True

        Args:
            page: Página principal de Playwright.

        Returns:
            True si el checkbox se marcó y no apareció reto visual.
            False en cualquier otro caso.
        """
        logger.info("[CAPTCHA] Intentando resolver reCAPTCHA v2 (checkbox)...")

        frame = await CaptchaAutosolver._find_iframe_frame_locator(
            page,
            CaptchaAutosolver._RECAPTCHA_ANCHOR_IFRAME_SELECTORS,
        )
        if not frame:
            logger.debug("[reCAPTCHA] iframe anchor no encontrado.")
            return False

        try:
            # Pausa pre-click: el humano "lee" el widget antes de interactuar
            await asyncio.sleep(random.uniform(0.8, 2.2))

            await _human_click_in_frame(
                page,
                frame,
                CaptchaAutosolver._RECAPTCHA_CHECKBOX_SELECTOR,
                timeout_ms=6_000,
            )
            logger.debug("[reCAPTCHA] Checkbox clickado. Esperando evaluación...")

            # reCAPTCHA evalúa señales de comportamiento durante ~2-4 segundos
            await asyncio.sleep(random.uniform(2.5, 5.0))

            # Verificar si saltó el reto visual (bframe)
            if await CaptchaAutosolver._visual_challenge_appeared(page):
                logger.warning(
                    "[reCAPTCHA] Reto visual activo (imágenes). "
                    "Resolución automática no disponible."
                )
                return False

            # Verificar estado "checked" dentro del iframe anchor
            solved = await CaptchaAutosolver._is_solved_in_frame(
                frame,
                CaptchaAutosolver._RECAPTCHA_SOLVED_SELECTOR,
                timeout_ms=3_000,
            )
            if solved:
                logger.info("[reCAPTCHA] ✓ Checkbox resuelto exitosamente.")
            else:
                logger.warning("[reCAPTCHA] Checkbox no marcado tras el click.")
            return solved

        except PlaywrightTimeoutError:
            logger.warning("[reCAPTCHA] Timeout esperando el checkbox.")
            return False
        except Exception as exc:
            logger.warning("[reCAPTCHA] Error inesperado: %s", exc)
            return False

    @staticmethod
    async def _solve_hcaptcha(page: Page) -> bool:
        """
        Intenta resolver hCaptcha haciendo click en el checkbox "I am human".

        hCaptcha sigue el mismo patrón que reCAPTCHA v2: un iframe con un
        checkbox inicial que puede resolver el CAPTCHA directamente si el
        perfil del navegador tiene suficiente "confianza". Si no la tiene,
        aparece un reto visual que no podemos resolver automáticamente.

        Args:
            page: Página principal de Playwright.

        Returns:
            True si el checkbox quedó en aria-checked="true".
            False en cualquier otro caso.
        """
        logger.info("[CAPTCHA] Intentando resolver hCaptcha (checkbox)...")

        frame = await CaptchaAutosolver._find_iframe_frame_locator(
            page,
            CaptchaAutosolver._HCAPTCHA_IFRAME_SELECTORS,
        )
        if not frame:
            logger.debug("[hCaptcha] iframe no encontrado.")
            return False

        try:
            await asyncio.sleep(random.uniform(0.6, 1.8))

            await _human_click_in_frame(
                page,
                frame,
                CaptchaAutosolver._HCAPTCHA_CHECKBOX_SELECTOR,
                timeout_ms=6_000,
            )
            logger.debug("[hCaptcha] Checkbox clickado. Esperando evaluación...")

            # hCaptcha evalúa señales durante ~2-4 segundos
            await asyncio.sleep(random.uniform(2.0, 4.5))

            solved = await CaptchaAutosolver._is_solved_in_frame(
                frame,
                CaptchaAutosolver._HCAPTCHA_SOLVED_SELECTOR,
                timeout_ms=3_000,
            )
            if solved:
                logger.info("[hCaptcha] ✓ Checkbox resuelto exitosamente.")
            else:
                logger.warning(
                    "[hCaptcha] aria-checked sigue en false. "
                    "Posible reto visual o sesión no confiable."
                )
            return solved

        except PlaywrightTimeoutError:
            logger.warning("[hCaptcha] Timeout esperando el checkbox.")
            return False
        except Exception as exc:
            logger.warning("[hCaptcha] Error inesperado: %s", exc)
            return False

    @staticmethod
    async def _solve_cloudflare_turnstile(page: Page) -> bool:
        """
        Intenta resolver Cloudflare Turnstile haciendo click en su checkbox.

        Turnstile moderno hace el "challenge" de manera invisible (analiza
        señales de TLS, canvas, mouse, etc.) y luego muestra un checkbox
        final para confirmación. Si el perfil del navegador pasa las
        verificaciones previas, el click en el checkbox es suficiente.

        En algunos deployments de Cloudflare (JS challenge legacy) no hay
        iframe visible: la página simplemente redirige tras unos segundos
        automáticamente. Este solver solo actúa cuando hay un iframe visible.

        Args:
            page: Página principal de Playwright.

        Returns:
            True si el checkbox se marcó o si la página redirigió.
            False si el iframe no existe o el click no funcionó.
        """
        logger.info("[CAPTCHA] Intentando resolver Cloudflare Turnstile (checkbox)...")

        frame = await CaptchaAutosolver._find_iframe_frame_locator(
            page,
            CaptchaAutosolver._CF_TURNSTILE_IFRAME_SELECTORS,
        )
        if not frame:
            logger.debug("[CF Turnstile] iframe no encontrado.")
            return False

        try:
            # Turnstile analiza comportamiento antes de mostrar el checkbox
            # Esperar más tiempo para que el widget inicialice completamente
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # Probar cada selector del checkbox en orden
            clicked = False
            for checkbox_selector in CaptchaAutosolver._CF_TURNSTILE_CHECKBOX_SELECTORS:
                try:
                    el = frame.locator(checkbox_selector).first
                    if await el.is_visible(timeout=2_000):
                        await _human_click_in_frame(
                            page, frame, checkbox_selector, timeout_ms=3_000
                        )
                        clicked = True
                        logger.debug("[CF Turnstile] Click en '%s'.", checkbox_selector)
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning("[CF Turnstile] No se encontró checkbox clickable.")
                return False

            # Cloudflare procesa la respuesta durante 3-8 segundos
            await asyncio.sleep(random.uniform(3.0, 6.0))

            # La señal de éxito en Turnstile es que la página redirige o que
            # el token de respuesta aparece en el DOM
            token_present = await page.evaluate("""
                () => {
                    // Turnstile inyecta el token en un input hidden de la página padre
                    const tokenInput = document.querySelector(
                        'input[name="cf-turnstile-response"], ' +
                        'input[name="g-recaptcha-response"]'
                    );
                    return tokenInput ? tokenInput.value.length > 0 : false;
                }
            """)

            if token_present:
                logger.info("[CF Turnstile] ✓ Token de respuesta detectado. Resuelto.")
                return True

            # Alternativa: verificar si el iframe desapareció (se cerró el challenge)
            try:
                cf_iframe = page.locator(CaptchaAutosolver._CF_TURNSTILE_IFRAME_SELECTORS[0]).first
                still_visible = await cf_iframe.is_visible(timeout=500)
                if not still_visible:
                    logger.info("[CF Turnstile] ✓ iframe cerrado. Challenge superado.")
                    return True
            except Exception:
                pass

            logger.warning("[CF Turnstile] No se pudo confirmar resolución.")
            return False

        except PlaywrightTimeoutError:
            logger.warning("[CF Turnstile] Timeout durante la resolución.")
            return False
        except Exception as exc:
            logger.warning("[CF Turnstile] Error inesperado: %s", exc)
            return False

    # ── API pública ──────────────────────────────────────────────────────────

    @staticmethod
    async def detect_type(page: Page) -> CaptchaType:
        """
        Identifica el tipo de CAPTCHA presente en la página actual.

        Busca iframes característicos de cada proveedor en el orden:
        reCAPTCHA → hCaptcha → Cloudflare. Devuelve el primero que encuentre.

        Args:
            page: Página de Playwright activa.

        Returns:
            ``CaptchaType`` identificado, o ``CaptchaType.UNKNOWN`` si no
            se reconoce ningún proveedor.
        """
        checks: list[tuple[CaptchaType, tuple[str, ...]]] = [
            (
                CaptchaType.RECAPTCHA_V2,
                CaptchaAutosolver._RECAPTCHA_ANCHOR_IFRAME_SELECTORS,
            ),
            (
                CaptchaType.HCAPTCHA,
                CaptchaAutosolver._HCAPTCHA_IFRAME_SELECTORS,
            ),
            (
                CaptchaType.CF_TURNSTILE,
                CaptchaAutosolver._CF_TURNSTILE_IFRAME_SELECTORS,
            ),
        ]
        for captcha_type, selectors in checks:
            for selector in selectors:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        logger.debug(
                            "[CAPTCHA] Tipo identificado: %s (via '%s')",
                            captcha_type.name, selector,
                        )
                        return captcha_type
                except Exception:
                    continue

        return CaptchaType.UNKNOWN

    @staticmethod
    async def try_solve_checkbox(
        page: Page,
        keyword: str = "",
        max_attempts: int = 2,
    ) -> bool:
        """
        Intenta resolver automáticamente el CAPTCHA de checkbox presente en la página.

        Flujo completo:
          1. Identificar el tipo de CAPTCHA (reCAPTCHA / hCaptcha / CF Turnstile)
          2. Llamar al solver específico del proveedor
          3. Esperar a que la página procese el resultado
          4. Verificar con ``CaptchaDetector.check()`` si el CAPTCHA desapareció
          5. Si no se resolvió y quedan intentos: reintentar (un click puede fallar
             por timing; el segundo suele tener mejor probabilidad)

        Args:
            page:         Página de Playwright activa.
            keyword:      Keyword actual (para logging y verificación).
            max_attempts: Número máximo de intentos antes de rendirse.
                          Default 2 (el segundo intento suele tener mejor timing).

        Returns:
            True  → CAPTCHA resuelto, la página está limpia.
            False → No se pudo resolver (requiere escalado: nueva identidad / resolución manual).

        Example::

            try:
                await CaptchaDetector.check(page, keyword)
            except CaptchaError:
                solved = await CaptchaAutosolver.try_solve_checkbox(page, keyword)
                if not solved:
                    await rotate_identity(...)
        """
        captcha_type = await CaptchaAutosolver.detect_type(page)
        logger.info(
            "[CAPTCHA] Intentando resolver automáticamente: %s | kw='%s'",
            captcha_type.name, keyword,
        )

        if captcha_type == CaptchaType.UNKNOWN:
            logger.warning(
                "[CAPTCHA] Tipo no identificado. No se puede resolver automáticamente."
            )
            return False

        # Mapa de tipo → función solver
        solvers = {
            CaptchaType.RECAPTCHA_V2: CaptchaAutosolver._solve_recaptcha_v2,
            CaptchaType.HCAPTCHA:     CaptchaAutosolver._solve_hcaptcha,
            CaptchaType.CF_TURNSTILE: CaptchaAutosolver._solve_cloudflare_turnstile,
        }
        solver_fn = solvers[captcha_type]

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                # Espera exponencial entre intentos
                backoff = random.uniform(3.0, 6.0) * (attempt - 1)
                logger.info(
                    "[CAPTCHA] Reintento %d/%d en %.1fs...", attempt, max_attempts, backoff
                )
                await asyncio.sleep(backoff)

            click_ok = await solver_fn(page)
            if not click_ok:
                logger.debug("[CAPTCHA] Solver reportó fallo en intento %d.", attempt)
                continue

            # Verificar que la página ya no muestra el CAPTCHA
            await asyncio.sleep(random.uniform(1.5, 3.0))
            try:
                await CaptchaDetector.check(page, keyword)
                # Si check() no lanza → página limpia → CAPTCHA resuelto
                logger.info(
                    "[CAPTCHA] ✓ Resolución confirmada por CaptchaDetector en intento %d.",
                    attempt,
                )
                return True
            except CaptchaError:
                logger.warning(
                    "[CAPTCHA] CaptchaDetector sigue detectando bloqueo "
                    "tras intento %d.",
                    attempt,
                )

        logger.error(
            "[CAPTCHA] No se pudo resolver %s tras %d intentos | kw='%s'",
            captcha_type.name, max_attempts, keyword,
        )
        return False