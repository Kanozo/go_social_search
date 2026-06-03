"""
anti_detection/stealth_scripts.py
Payloads JavaScript de evasión de fingerprinting inyectados como init scripts.

Cada función devuelve un string JS autoejecutable (IIFE) que parchea el
entorno del navegador *antes* de que cargue cualquier script de la página,
eliminando las señales que los sistemas anti-bot analizan.

Cambios respecto a la versión anterior
────────────────────────────────────────
  BUG FIX  _patch_navigator_core       → deviceMemory solo se parchea en Chromium
  BUG FIX  _patch_screen_metrics       → chrome_h parametrizado por browser_type
  BUG FIX  _patch_canvas_noise         → toDataURL ya no modifica el canvas (efecto secundario)
  BUG FIX  _patch_iframe_propagation   → propaga fingerprint completo, no solo webdriver
  NEW      _patch_geolocation          → bloquea navigator.geolocation (Chromium + Firefox)
  NEW      _patch_notification_request → intercepta Notification.requestPermission (Chromium)
  NEW      _patch_media_devices        → oculta enumerateDevices en Chromium
  NEW      _patch_battery_api          → navigator.getBattery devuelve batería falsa
  NEW      _patch_headless_leaks       → corrige outerWidth/outerHeight=0 y otros headless tells
  NEW      _patch_chrome_object_guard  → garantiza window.chrome solo en Chromium
  NEW      _patch_keyboard_api         → navigator.keyboard disponible en Chromium headless
  NEW      _patch_speech_synthesis     → speechSynthesis.getVoices() no vacío
"""
from __future__ import annotations

import json


# ─────────────────────────────────────────────────────────────────────────────
# Parches comunes (Firefox + Chromium)
# ─────────────────────────────────────────────────────────────────────────────

def _patch_navigator_core(
    browser_type: str,
    platform: str,
    language: str,
    hardware_concurrency: int,
    device_memory: int,
) -> str:
    """
    Parches básicos de navigator.

    CORRECCIÓN: deviceMemory solo se parchea en Chromium. Firefox no expone
    esta API; parchearla en Firefox crea una señal de incoherencia detectable.

    Args:
        browser_type:         "firefox" | "chromium"
        platform:             Valor de navigator.platform
        language:             Accept-Language header (p.ej. "en-US,en;q=0.9")
        hardware_concurrency: Número de CPUs lógicas a reportar
        device_memory:        GB de RAM (solo Chromium)
    """
    lang_parts = language.split(",")[0].strip()
    # deviceMemory solo existe en Chromium; inyectarlo en Firefox es una señal
    device_memory_patch = (
        f"""
    try {{
        if ('deviceMemory' in navigator) {{
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: () => {device_memory},
                configurable: true,
            }});
        }}
    }} catch (_) {{}}
"""
        if browser_type == "chromium"
        else "    // deviceMemory: Firefox no expone esta API (omitido intencionalmente)"
    )

    return f"""
(function patchNavigatorCore() {{
    try {{
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined,
            configurable: false,
        }});
    }} catch (_) {{}}

    try {{
        Object.defineProperty(navigator, 'platform', {{
            get: () => {json.dumps(platform)},
            configurable: true,
        }});
    }} catch (_) {{}}

    try {{
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {hardware_concurrency},
            configurable: true,
        }});
    }} catch (_) {{}}

{device_memory_patch}

    try {{
        if (!navigator.connection) {{
            Object.defineProperty(navigator, 'connection', {{
                get: () => ({{
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10.0,
                    saveData: false,
                    onchange: null,
                }}),
                configurable: true,
            }});
        }}
    }} catch (_) {{}}

    try {{
        Object.defineProperty(navigator, 'language', {{
            get: () => {json.dumps(lang_parts)},
            configurable: true,
        }});
        Object.defineProperty(navigator, 'languages', {{
            get: () => {json.dumps([lang_parts, lang_parts.split("-")[0]])},
            configurable: true,
        }});
    }} catch (_) {{}}
}})();
"""


def _patch_plugins_firefox() -> str:
    """
    Plugins realistas para Firefox.

    Firefox moderno puede tener 0 plugins o solo PDF Viewer según configuración.
    Usamos PDF Viewer como mínimo plausible (presente en la mayoría de builds).
    CORRECCIÓN: window.chrome no debe existir en Firefox.
    """
    return """
(function patchFirefoxPlugins() {
    const fakePlugins = [
        {
            name: 'PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
            item: (i) => i === 0 ? {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: null} : null,
            namedItem: (n) => n === 'application/pdf' ? {type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: null} : null,
        },
    ];
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [...fakePlugins];
                arr.refresh = () => {};
                arr.item = (i) => arr[i] || null;
                arr.namedItem = (n) => arr.find(p => p.name === n) || null;
                Object.defineProperty(arr, 'length', { get: () => arr.length });
                return arr;
            },
            configurable: true,
        });
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => {
                const mt = [{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: null }];
                mt.item = (i) => mt[i] || null;
                mt.namedItem = (n) => mt.find(m => m.type === n) || null;
                Object.defineProperty(mt, 'length', { get: () => mt.length });
                return mt;
            },
            configurable: true,
        });
    } catch (_) {}
    // CORRECCIÓN: window.chrome NO debe existir en Firefox (su presencia delata Chromium).
    // No se inyecta el objeto chrome aquí.
})();
"""


def _patch_plugins_chromium() -> str:
    """
    Plugins y objeto ``window.chrome`` para Chromium/Chrome.

    CORRECCIÓN: window.chrome solo se inyecta en Chromium.
    """
    return """
(function patchChromiumPlugins() {
    const makePlugin = (name, filename, desc, mimeType, suffix) => ({
        name, filename, description: desc, length: 1,
        item: (i) => i === 0 ? { type: mimeType, suffixes: suffix, description: desc, enabledPlugin: null } : null,
        namedItem: (n) => n === mimeType ? { type: mimeType, suffixes: suffix, description: desc, enabledPlugin: null } : null,
    });
    const fakePlugins = [
        makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format', 'application/x-google-chrome-pdf', 'pdf'),
        makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', 'application/pdf', 'pdf'),
        makePlugin('Native Client', 'internal-nacl-plugin', '', 'application/x-nacl', ''),
    ];
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [...fakePlugins];
                arr.refresh = () => {};
                arr.item = (i) => arr[i] || null;
                arr.namedItem = (n) => arr.find(p => p.name === n) || null;
                Object.defineProperty(arr, 'length', { get: () => arr.length });
                return arr;
            },
            configurable: true,
        });
    } catch (_) {}

    // window.chrome: solo Chromium lo tiene; Firefox no.
    try {
        if (!window.chrome) {
            window.chrome = {
                runtime: {
                    id: undefined,
                    connect: () => ({ postMessage: () => {}, onMessage: { addListener: () => {} } }),
                    sendMessage: () => {},
                    onMessage: { addListener: () => {} },
                },
                loadTimes: function() { return {}; },
                csi: function() {
                    return { startE: Date.now(), onloadT: Date.now(), pageT: 0, tran: 15 };
                },
                app: { isInstalled: false },
            };
        }
    } catch (_) {}
})();
"""


def _patch_canvas_noise() -> str:
    """
    Añade ruido mínimo al canvas para impedir fingerprinting exacto.

    CORRECCIÓN: toDataURL ya no llama a putImageData (modificaba el canvas
    produciendo efectos secundarios visibles para la página).
    El ruido se aplica solo en getImageData, que es lo que usan los
    fingerprinters; toDataURL simplemente lo llama internamente.
    """
    return """
(function patchCanvasNoise() {
    const NOISE_SEED = Math.floor(Math.random() * 255);
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
        const imageData = origGetImageData.call(this, x, y, w, h);
        // Ruido mínimo en píxeles dispersos: no perceptible visualmente
        for (let i = 0; i < imageData.data.length; i += 400) {
            imageData.data[i] = Math.max(0, Math.min(255, imageData.data[i] ^ (NOISE_SEED & 3)));
        }
        return imageData;
    };
    // CORRECCIÓN: NO se modifica toDataURL para evitar efectos secundarios en el canvas.
    // getImageData es el vector de ataque real; toDataURL lo llama internamente.
})();
"""


def _patch_webgl(vendor: str, renderer: str) -> str:
    return f"""
(function patchWebGL() {{
    const VENDOR   = {json.dumps(vendor)};
    const RENDERER = {json.dumps(renderer)};
    function patchContext(proto) {{
        const origGetParam = proto.getParameter;
        proto.getParameter = function(param) {{
            if (param === 37445) return VENDOR;    // UNMASKED_VENDOR_WEBGL
            if (param === 37446) return RENDERER;  // UNMASKED_RENDERER_WEBGL
            return origGetParam.call(this, param);
        }};
        const origGetExts = proto.getSupportedExtensions;
        if (origGetExts) {{
            proto.getSupportedExtensions = function() {{
                return origGetExts.call(this) || [];
            }};
        }}
    }}
    try {{
        patchContext(WebGLRenderingContext.prototype);
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            patchContext(WebGL2RenderingContext.prototype);
        }}
    }} catch (_) {{}}
}})();
"""


def _patch_audio_context() -> str:
    return """
(function patchAudioContext() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const AUDIO_NOISE = (Math.random() * 2e-10) - 1e-10;
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
        const channelData = origGetChannelData.call(this, channel);
        if (channelData.length > 0) channelData[0] += AUDIO_NOISE;
        return channelData;
    };
})();
"""


def _patch_rtc_ip_leak() -> str:
    return """
(function patchRTCIPLeak() {
    if (!window.RTCPeerConnection) return;
    const OrigRTC = window.RTCPeerConnection;
    function PatchedRTC(config, constraints) {
        if (config && config.iceServers) {
            config = { ...config, iceServers: [] };
        }
        return new OrigRTC(config, constraints);
    }
    PatchedRTC.prototype = OrigRTC.prototype;
    Object.setPrototypeOf(PatchedRTC, OrigRTC);
    window.RTCPeerConnection = PatchedRTC;
})();
"""


def _patch_screen_metrics(width: int, height: int, browser_type: str) -> str:
    """
    Parchea métricas de pantalla coherentes con el viewport.

    CORRECCIÓN: chrome_h (altura del chrome del navegador) es diferente
    entre Firefox (~74px) y Chromium (~85px). Se parametriza por browser_type.

    Args:
        width:        Ancho del viewport.
        height:       Alto del viewport.
        browser_type: "firefox" | "chromium"
    """
    chrome_h = 74 if browser_type == "firefox" else 85
    return f"""
(function patchScreenMetrics() {{
    try {{
        Object.defineProperty(window, 'outerWidth',  {{ get: () => {width},               configurable: true }});
        Object.defineProperty(window, 'outerHeight', {{ get: () => {height + chrome_h},   configurable: true }});
        Object.defineProperty(screen, 'width',       {{ get: () => {width},               configurable: true }});
        Object.defineProperty(screen, 'height',      {{ get: () => {height},              configurable: true }});
        Object.defineProperty(screen, 'availWidth',  {{ get: () => {width},               configurable: true }});
        Object.defineProperty(screen, 'availHeight', {{ get: () => {height - 40},         configurable: true }});
    }} catch (_) {{}}
}})();
"""


def _patch_permissions_api() -> str:
    return """
(function patchPermissionsAPI() {
    if (!navigator.permissions || !navigator.permissions.query) return;
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = function(permDesc) {
        if (permDesc && permDesc.name === 'notifications') {
            return Promise.resolve({ state: 'default', onchange: null });
        }
        if (permDesc && permDesc.name === 'geolocation') {
            return Promise.resolve({ state: 'denied', onchange: null });
        }
        return origQuery(permDesc);
    };
})();
"""


def _patch_performance_timing() -> str:
    return """
(function patchPerformanceTiming() {
    const origNow = performance.now.bind(performance);
    performance.now = function() {
        return Math.round(origNow() * 10) / 10;
    };
})();
"""


def _patch_notification_permission() -> str:
    return """
(function patchNotificationPermission() {
    try {
        if (typeof Notification !== 'undefined') {
            Object.defineProperty(Notification, 'permission', {
                get: () => 'default',
                configurable: true,
            });
        }
    } catch (_) {}
})();
"""


def _patch_iframe_propagation(
    platform: str,
    language: str,
    hardware_concurrency: int,
    device_memory: int,
    browser_type: str,
) -> str:
    """
    Propaga el fingerprint completo a iframes creados dinámicamente.

    CORRECCIÓN: la versión anterior solo parcheba ``webdriver`` en iframes.
    Los detectores modernos (PerimeterX, DataDome) verifican la coherencia
    de ``platform``, ``hardwareConcurrency`` y ``language`` dentro de iframes.

    Args:
        platform:             navigator.platform del perfil activo
        language:             Idioma primario del perfil
        hardware_concurrency: CPUs lógicas del perfil
        device_memory:        RAM del perfil (solo Chromium)
        browser_type:         "firefox" | "chromium"
    """
    lang_primary = language.split(",")[0].strip()
    device_memory_patch = (
        f"if ('deviceMemory' in iNav) {{ Object.defineProperty(iNav, 'deviceMemory', {{ get: () => {device_memory}, configurable: true }}); }}"
        if browser_type == "chromium"
        else "// deviceMemory: no disponible en Firefox"
    )
    return f"""
(function patchIframePropagation() {{
    const origCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName, options) {{
        const el = origCreateElement(tagName, options);
        if (tagName && tagName.toLowerCase() === 'iframe') {{
            el.addEventListener('load', function() {{
                try {{
                    const iNav = el.contentWindow.navigator;
                    Object.defineProperty(iNav, 'webdriver',            {{ get: () => undefined,                          configurable: false }});
                    Object.defineProperty(iNav, 'platform',             {{ get: () => {json.dumps(platform)},             configurable: true  }});
                    Object.defineProperty(iNav, 'hardwareConcurrency',  {{ get: () => {hardware_concurrency},             configurable: true  }});
                    Object.defineProperty(iNav, 'language',             {{ get: () => {json.dumps(lang_primary)},         configurable: true  }});
                    {device_memory_patch}
                }} catch (_) {{}}
            }});
        }}
        return el;
    }};
}})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# Parches nuevos
# ─────────────────────────────────────────────────────────────────────────────

def _patch_geolocation() -> str:
    """
    Bloquea navigator.geolocation para ambos browsers.

    Sin este parche, un site puede solicitar la ubicación real. En Chromium
    headless la geolocalización devuelve POSITION_UNAVAILABLE, lo que es
    una señal bot; en Firefox devuelve el permiso del sistema. Al bloquearla
    a nivel JS retornamos PERMISSION_DENIED de forma consistente.
    """
    return """
(function patchGeolocation() {
    try {
        const deniedError = {
            code: 1,          // PERMISSION_DENIED
            message: 'User denied Geolocation',
            PERMISSION_DENIED:    1,
            POSITION_UNAVAILABLE: 2,
            TIMEOUT:              3,
        };
        const blockedGeo = {
            getCurrentPosition: (_success, error) => {
                if (typeof error === 'function') error(deniedError);
            },
            watchPosition: (_success, error) => {
                if (typeof error === 'function') error(deniedError);
                return 0;
            },
            clearWatch: () => {},
        };
        Object.defineProperty(navigator, 'geolocation', {
            get: () => blockedGeo,
            configurable: true,
        });
    } catch (_) {}
})();
"""


def _patch_notification_request_chromium() -> str:
    """
    Intercepta Notification.requestPermission en Chromium.

    En Chromium headless, requestPermission() puede lanzar o devolver
    "denied" de forma inmediata, lo que algunos fingerprinters verifican
    llamándola explícitamente. Este parche devuelve "default" (el usuario
    no ha tomado ninguna decisión) sin lanzar ni bloquear.

    Solo se aplica en Chromium; en Firefox el comportamiento nativo es
    suficientemente ambiguo.
    """
    return """
(function patchNotificationRequest() {
    try {
        if (typeof Notification === 'undefined') return;
        const origRequest = Notification.requestPermission.bind(Notification);
        Notification.requestPermission = function(callback) {
            // Devolver 'default' sin mostrar UI ni lanzar excepción
            if (typeof callback === 'function') {
                callback('default');
                return Promise.resolve('default');
            }
            return Promise.resolve('default');
        };
    } catch (_) {}
})();
"""


def _patch_media_devices_chromium() -> str:
    """
    Oculta enumerateDevices en Chromium.

    En Chromium headless, ``enumerateDevices()`` devuelve lista vacía [].
    Un browser real devuelve al menos un audioinput y un audiooutput.
    Retornamos dos dispositivos genéricos sin exponer deviceId ni label reales.
    """
    return """
(function patchMediaDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    const origEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
    navigator.mediaDevices.enumerateDevices = function() {
        return origEnumerate().then(devices => {
            if (devices.length > 0) return devices;
            // Headless devuelve []; retornamos dispositivos genéricos
            return [
                { kind: 'audioinput',  deviceId: '', groupId: '', label: '', toJSON: () => ({}) },
                { kind: 'audiooutput', deviceId: '', groupId: '', label: '', toJSON: () => ({}) },
            ];
        }).catch(() => []);
    };
})();
"""


def _patch_battery_api() -> str:
    """
    navigator.getBattery() ausente o siempre cargando en headless.

    En headless, getBattery() puede rechazar la promesa o devolver un objeto
    incoherente. Retornamos una batería al 100% cargando (estado neutro).
    """
    return """
(function patchBatteryAPI() {
    if (!navigator.getBattery) return;
    const fakeBattery = {
        charging: true,
        chargingTime: 0,
        dischargingTime: Infinity,
        level: 1.0,
        addEventListener:    () => {},
        removeEventListener: () => {},
        dispatchEvent:       () => true,
        onchargingchange:       null,
        onchargingtimechange:   null,
        ondischargingtimechange: null,
        onlevelchange:          null,
    };
    navigator.getBattery = function() {
        return Promise.resolve(fakeBattery);
    };
})();
"""


def _patch_headless_leaks_chromium() -> str:
    """
    Corrige leaks específicos de Chromium headless.

    Señales que eliminamos:
    - ``navigator.keyboard`` ausente en headless → stub con getLayoutMap
    - ``window.Intl.v8BreakIterator`` ausente en headless → stub
    - ``chrome.runtime`` coherente (ya cubierto en _patch_plugins_chromium)
    - ``navigator.userActivation`` stub (indica interacción humana previa)
    """
    return """
(function patchHeadlessLeaks() {
    // navigator.keyboard: ausente en headless, presente en Chrome real
    try {
        if (!navigator.keyboard) {
            Object.defineProperty(navigator, 'keyboard', {
                get: () => ({
                    getLayoutMap: () => Promise.resolve(new Map()),
                    lock:   () => Promise.resolve(),
                    unlock: () => {},
                }),
                configurable: true,
            });
        }
    } catch (_) {}

    // navigator.userActivation: headless devuelve siempre false
    try {
        if (navigator.userActivation) {
            Object.defineProperty(navigator.userActivation, 'hasBeenActive', {
                get: () => true,
                configurable: true,
            });
            Object.defineProperty(navigator.userActivation, 'isActive', {
                get: () => true,
                configurable: true,
            });
        }
    } catch (_) {}

    // Intl.v8BreakIterator: ausente en headless, presente en V8 real
    try {
        if (typeof Intl !== 'undefined' && !Intl.v8BreakIterator) {
            Intl.v8BreakIterator = function(locales, opts) {
                return {
                    adoptText:            () => {},
                    first:                () => 0,
                    next:                 () => 0,
                    current:              () => 0,
                    breakType:            () => 'none',
                    resolvedOptions:      () => ({}),
                };
            };
        }
    } catch (_) {}
})();
"""


def _patch_speech_synthesis() -> str:
    """
    speechSynthesis.getVoices() devuelve lista vacía en headless.

    Algunos fingerprinters cuentan voces disponibles; 0 voces = headless.
    Retornamos un array con una voz genérica.
    """
    return """
(function patchSpeechSynthesis() {
    try {
        if (!window.speechSynthesis) return;
        const origGetVoices = speechSynthesis.getVoices.bind(speechSynthesis);
        speechSynthesis.getVoices = function() {
            const voices = origGetVoices();
            if (voices.length > 0) return voices;
            // Headless devuelve []; retornamos una voz mínima
            return [{
                voiceURI:   'Google US English',
                name:       'Google US English',
                lang:       'en-US',
                localService: false,
                default:    true,
            }];
        };
    } catch (_) {}
})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# Función pública de composición
# ─────────────────────────────────────────────────────────────────────────────

def build_full_stealth_script(
    browser_type: str,
    platform: str,
    language: str,
    hardware_concurrency: int,
    device_memory: int,
    webgl_vendor: str,
    webgl_renderer: str,
    viewport_width: int,
    viewport_height: int,
) -> str:
    """
    Compone el script unificado de inyección IIFE para el browser indicado.

    Aplica todos los parches en el orden correcto: primero navigator core,
    luego plugins (browser-specific), luego APIs especializadas.
    Los parches exclusivos de Chromium (media devices, headless leaks,
    notification request) no se inyectan en Firefox para no introducir
    propiedades inexistentes que los fingerprinters podrían detectar.

    Args:
        browser_type:         "firefox" | "chromium"
        platform:             Valor de navigator.platform del perfil
        language:             Accept-Language principal del perfil
        hardware_concurrency: CPUs lógicas del perfil
        device_memory:        GB de RAM del perfil (aplicado solo en Chromium)
        webgl_vendor:         GPU vendor string
        webgl_renderer:       GPU renderer string
        viewport_width:       Ancho del viewport del perfil
        viewport_height:      Alto del viewport del perfil

    Returns:
        String JavaScript completo listo para ``page.add_init_script()``.
    """
    plugin_patch = (
        _patch_plugins_firefox()
        if browser_type == "firefox"
        else _patch_plugins_chromium()
    )

    # Parches base (todos los browsers)
    base_parts = [
        "/* ═══ Stealth Init Script ═══ */",
        _patch_navigator_core(
            browser_type, platform, language, hardware_concurrency, device_memory
        ),
        plugin_patch,
        _patch_canvas_noise(),
        _patch_webgl(webgl_vendor, webgl_renderer),
        _patch_audio_context(),
        _patch_rtc_ip_leak(),
        _patch_screen_metrics(viewport_width, viewport_height, browser_type),
        _patch_permissions_api(),
        _patch_performance_timing(),
        _patch_notification_permission(),
        _patch_iframe_propagation(
            platform, language, hardware_concurrency, device_memory, browser_type
        ),
        # Geolocalización bloqueada en ambos browsers
        _patch_geolocation(),
        _patch_battery_api(),
        _patch_speech_synthesis(),
    ]

    # Parches exclusivos de Chromium
    chromium_only_parts = [
        _patch_notification_request_chromium(),
        _patch_media_devices_chromium(),
        _patch_headless_leaks_chromium(),
    ]

    parts = base_parts + (chromium_only_parts if browser_type == "chromium" else [])
    return "\n".join(parts)