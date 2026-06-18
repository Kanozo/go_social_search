"""
anti_detection/fingerprint.py
Generación de fingerprints de navegador coherentes y aleatorizados.

Un fingerprint coherente significa que TODOS sus atributos son consistentes
entre sí: el User-Agent, el OS del ``navigator.platform``, las fuentes de
WebGL y la configuración de viewport forman un perfil creíble de un usuario
real con ese hardware y SO.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from reaper.anti_detection.stealth_scripts import build_full_stealth_script


# ─────────────────────────────────────────────────────────────────────────────
# Perfiles de hardware por plataforma
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ViewportConfig:
    """Resolución de pantalla."""
    width: int
    height: int


@dataclass(frozen=True)
class PlatformProfile:
    """
    Agrupa todas las propiedades de sistema operativo para un perfil.

    Garantiza coherencia: el UA, el ``navigator.platform``, el WebGL
    renderer y las fuentes del sistema coinciden con el mismo SO.
    """
    os_name: str           # "windows" | "macos" | "linux"
    navigator_platform: str  # Valor de navigator.platform
    firefox_ua_templates: list[str]
    chromium_ua_templates: list[str]
    webgl_vendors: list[dict[str, str]]
    viewports: list[ViewportConfig]
    hardware_concurrency_pool: list[int]
    device_memory_pool: list[int]


# ─────────────────────────────────────────────────────────────────────────────
# Catálogo de perfiles por SO
# ─────────────────────────────────────────────────────────────────────────────

_WINDOWS_PROFILE = PlatformProfile(
    os_name="windows",
    navigator_platform="Win32",
    firefox_ua_templates=[
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    ],
    chromium_ua_templates=[
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    ],
    webgl_vendors=[
        {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ],
    viewports=[
        ViewportConfig(1920, 1080),
        ViewportConfig(1440, 900),
        ViewportConfig(1536, 864),
        ViewportConfig(1366, 768),
        ViewportConfig(1280, 720),
    ],
    hardware_concurrency_pool=[4, 8, 12, 16],
    device_memory_pool=[4, 8, 16],
)

_MACOS_PROFILE = PlatformProfile(
    os_name="macos",
    navigator_platform="MacIntel",
    firefox_ua_templates=[
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:127.0) Gecko/20100101 Firefox/127.0",
    ],
    chromium_ua_templates=[
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ],
    webgl_vendors=[
        {"vendor": "Apple Inc.", "renderer": "Apple M1"},
        {"vendor": "Apple Inc.", "renderer": "Apple M2"},
        {"vendor": "Intel Inc.", "renderer": "Intel Iris OpenGL Engine"},
        {"vendor": "AMD", "renderer": "AMD Radeon Pro 5500M OpenGL Engine"},
    ],
    viewports=[
        ViewportConfig(2560, 1600),
        ViewportConfig(1920, 1080),
        ViewportConfig(1440, 900),
        ViewportConfig(1280, 800),
    ],
    hardware_concurrency_pool=[8, 10, 12],
    device_memory_pool=[8, 16],
)

_LINUX_PROFILE = PlatformProfile(
    os_name="linux",
    navigator_platform="Linux x86_64",
    firefox_ua_templates=[
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    ],
    chromium_ua_templates=[
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ],
    webgl_vendors=[
        {"vendor": "Intel Open Source Technology Center", "renderer": "Mesa DRI Intel(R) HD Graphics 620 (KBL GT2)"},
        {"vendor": "X.Org", "renderer": "AMD RENOIR (LLVM 14.0.0, DRM 3.44, 5.15.0)"},
        {"vendor": "nouveau", "renderer": "NV136"},
    ],
    viewports=[
        ViewportConfig(1920, 1080),
        ViewportConfig(1366, 768),
        ViewportConfig(1280, 1024),
    ],
    hardware_concurrency_pool=[4, 8],
    device_memory_pool=[4, 8],
)

# Perfiles disponibles con pesos de probabilidad (Windows >> macOS >> Linux)
_PROFILES_WEIGHTED: list[tuple[PlatformProfile, float]] = [
    (_WINDOWS_PROFILE, 0.65),
    (_MACOS_PROFILE, 0.25),
    (_LINUX_PROFILE, 0.10),
]

# Pools de idiomas con Accept-Language header
_ACCEPT_LANGUAGES: list[str] = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.5",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-CA,en;q=0.9",
    "en-AU,en;q=0.9",
]

# Zonas horarias por SO
_TIMEZONES_BY_OS: dict[str, list[tuple[str, str]]] = {
    "windows": [
        ("en-US", "America/New_York"),
        ("en-US", "America/Chicago"),
        ("en-US", "America/Los_Angeles"),
        ("en-GB", "Europe/London"),
        ("en-CA", "America/Toronto"),
    ],
    "macos": [
        ("en-US", "America/New_York"),
        ("en-US", "America/Los_Angeles"),
        ("en-US", "America/Chicago"),
        ("en-GB", "Europe/London"),
    ],
    "linux": [
        ("en-US", "America/New_York"),
        ("en-GB", "Europe/London"),
        ("en-US", "America/Los_Angeles"),
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass de fingerprint
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BrowserFingerprint:
    """
    Fingerprint completo y coherente de un navegador.

    Todos los campos son consistentes entre sí: el UA, el platform, el WebGL
    renderer y el viewport corresponden al mismo perfil de hardware.

    Attributes:
        browser_type:         "firefox" o "chromium"
        user_agent:           User-Agent string completo
        accept_language:      Header Accept-Language
        locale:               Locale BCP-47 (p.ej. "en-US")
        timezone_id:          IANA timezone (p.ej. "America/New_York")
        viewport:             Resolución del viewport
        navigator_platform:   Valor de navigator.platform
        hardware_concurrency: CPUs lógicas a reportar
        device_memory:        GB de RAM a reportar
        webgl_vendor:         Vendor GPU para WebGL
        webgl_renderer:       Renderer GPU para WebGL
        stealth_js:           Script JS de evasión completo (no en repr)
        extra_headers:        Headers HTTP adicionales coherentes con el perfil
    """
    browser_type: str
    user_agent: str
    accept_language: str
    locale: str
    timezone_id: str
    viewport: ViewportConfig
    navigator_platform: str
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    stealth_js: str = field(repr=False)
    extra_headers: dict[str, str] = field(default_factory=dict)

    def build_context_options(
        self,
        proxy: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Genera el dict de opciones para ``browser.new_context()``.

        Integra UA, locale, timezone, viewport y headers en un único dict
        listo para pasarse a Playwright.

        Args:
            proxy: Dict de proxy Playwright (p.ej. ``{"server": "socks5://..."}``).
                   None = conexión directa.

        Returns:
            Dict compatible con ``Browser.new_context(**opts)``.
        """
        opts: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "viewport": {"width": self.viewport.width, "height": self.viewport.height},
            "extra_http_headers": {
                "Accept-Language": self.accept_language,
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
                "Connection": "keep-alive",
                **self.extra_headers,
            },
            "java_script_enabled": True,
            # Deshabilitar WebRTC en el contexto (doble protección junto al JS patch)
            "permissions": [],
        }
        if proxy:
            opts["proxy"] = proxy
        return opts


# ─────────────────────────────────────────────────────────────────────────────
# Factory pública
# ─────────────────────────────────────────────────────────────────────────────

def _weighted_choice(weighted_items: list[tuple[Any, float]]) -> Any:
    """Selección aleatoria ponderada."""
    items, weights = zip(*weighted_items)
    return random.choices(items, weights=weights, k=1)[0]


def generate_fingerprint(browser_type: str = "firefox") -> BrowserFingerprint:
    """
    Genera un fingerprint aleatorio pero internamente coherente.

    Elige un perfil de SO con probabilidades realistas (Windows 65%,
    macOS 25%, Linux 10%) y deriva todos los atributos del mismo perfil,
    garantizando que no haya contradicciones entre UA, platform, WebGL y
    viewport.

    Args:
        browser_type: "firefox" | "chromium". Determina el pool de UAs
                      y el parche de plugins a aplicar.

    Returns:
        ``BrowserFingerprint`` listo para usar en Playwright.

    Example:
        >>> fp = generate_fingerprint("firefox")
        >>> context = await browser.new_context(**fp.build_context_options())
        >>> await page.add_init_script(fp.stealth_js)
    """
    profile: PlatformProfile = _weighted_choice(_PROFILES_WEIGHTED)

    ua_pool = (
        profile.firefox_ua_templates
        if browser_type == "firefox"
        else profile.chromium_ua_templates
    )
    user_agent = random.choice(ua_pool)

    webgl = random.choice(profile.webgl_vendors)
    viewport = random.choice(profile.viewports)
    locale, timezone_id = random.choice(_TIMEZONES_BY_OS[profile.os_name])
    accept_language = random.choice(_ACCEPT_LANGUAGES)
    hw_concurrency = random.choice(profile.hardware_concurrency_pool)
    device_mem = random.choice(profile.device_memory_pool)

    stealth_js = build_full_stealth_script(
        browser_type=browser_type,
        platform=profile.navigator_platform,
        language=accept_language,
        hardware_concurrency=hw_concurrency,
        device_memory=device_mem,
        webgl_vendor=webgl["vendor"],
        webgl_renderer=webgl["renderer"],
        viewport_width=viewport.width,
        viewport_height=viewport.height,
    )

    return BrowserFingerprint(
        browser_type=browser_type,
        user_agent=user_agent,
        accept_language=accept_language,
        locale=locale,
        timezone_id=timezone_id,
        viewport=viewport,
        navigator_platform=profile.navigator_platform,
        hardware_concurrency=hw_concurrency,
        device_memory=device_mem,
        webgl_vendor=webgl["vendor"],
        webgl_renderer=webgl["renderer"],
        stealth_js=stealth_js,
    )