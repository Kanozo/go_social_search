"""
anti_detection/stealth_scripts.py
Payloads JavaScript de evasión de fingerprinting inyectados como init scripts.

Cada función devuelve un string JS autoejecutable (IIFE) que parchea el
entorno del navegador *antes* de que cargue cualquier script de la página,
eliminando las señales que los sistemas anti-bot analizan.

Referencias técnicas:
  - https://github.com/nicholasgasior/playwright-stealth
  - CreepJS fingerprint test: https://abrahamjuliot.github.io/creepjs/
  - Bot detection breakdown: https://datadome.co/bot-management-protection/
"""
from __future__ import annotations

import json


# ─────────────────────────────────────────────────────────────────────────────
# Parches por navegador
# ─────────────────────────────────────────────────────────────────────────────

def _patch_navigator_core(
    platform: str,
    language: str,
    hardware_concurrency: int,
    device_memory: int,
) -> str:
    """
    Parches básicos de navigator: webdriver, platform, hw, memoria, conexión.

    Señales que elimina:
      - ``navigator.webdriver`` (la más conocida de todas)
      - ``navigator.platform`` (debe coincidir con el OS del UA)
      - ``navigator.hardwareConcurrency`` (0 delata headless/VM)
      - ``navigator.deviceMemory`` (solo Chrome/Chromium)
      - ``navigator.connection`` (ausente = automatización)
      - ``navigator.language`` / ``navigator.languages``
    """
    lang_parts = language.split(",")[0].strip()
    return f"""
(function patchNavigatorCore() {{
    // ── webdriver ─────────────────────────────────────────────────────────
    // La propiedad más reconocida para detectar automatización.
    // 'configurable: false' previene que scripts de la página la restauren.
    try {{
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined,
            configurable: false,
        }});
    }} catch (_) {{}}

    // ── platform ──────────────────────────────────────────────────────────
    // Debe coincidir con el OS en el User-Agent para evitar inconsistencias.
    try {{
        Object.defineProperty(navigator, 'platform', {{
            get: () => {json.dumps(platform)},
            configurable: true,
        }});
    }} catch (_) {{}}

    // ── hardwareConcurrency ───────────────────────────────────────────────
    // Un entorno headless real suele devolver 0 o 1.
    try {{
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {hardware_concurrency},
            configurable: true,
        }});
    }} catch (_) {{}}

    // ── deviceMemory (Chrome/Chromium only) ───────────────────────────────
    try {{
        if ('deviceMemory' in navigator) {{
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: () => {device_memory},
                configurable: true,
            }});
        }}
    }} catch (_) {{}}

    // ── connection API ────────────────────────────────────────────────────
    // Navegadores headless no simulan esta API; su ausencia delata bots.
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

    // ── languages ────────────────────────────────────────────────────────
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
    Simula una lista realista de plugins de Firefox.

    Firefox headless devuelve ``navigator.plugins.length === 0``, lo cual
    es una señal fuerte de automatización.
    """
    return """
(function patchFirefoxPlugins() {
    // Firefox tiene un solo plugin funcional (PDF viewer).
    // Simulamos la lista mínima creíble.
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
})();
"""


def _patch_plugins_chromium() -> str:
    return """
(function patchChromiumPlugins() {
    const makePlugin = (name, filename, desc, mimeType, suffix) => ({
        name, filename, description: desc, length: 1,
        item: (i) => i === 0 ? { type: mimeType, suffixes: suffix, description: desc, enabledPlugin: null } : null,
        namedItem: (n) => n === mimeType ? { type: mimeType, suffixes: suffix, description: desc, enabledPlugin: null } : null,
    });

    const fakePlugins = [
        makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/pdf', 'pdf'),
        makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', 'application/pdf', 'pdf'),
        makePlugin('Chromium PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', 'application/pdf', 'pdf'),
        makePlugin('Microsoft Edge PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', 'application/pdf', 'pdf'),
        makePlugin('WebKit built-in PDF', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', 'application/pdf', 'pdf'),
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

    // window.chrome más realista — Chrome 124+
    try {
        if (!window.chrome) {
            window.chrome = {
                runtime: {
                    OnInstalledReason: {},
                    OnRestartRequiredReason: {},
                    PlatformInfo: {},
                    RequestUpdateCheckStatus: {},
                    connect: function() { return { onDisconnect: { addListener: function() {} }, onMessage: { addListener: function() {} }, postMessage: function() {} }; },
                    sendMessage: function() { return Promise.resolve(); },
                },
                loadTimes: function() {
                    return {
                        commitLoadTime: Date.now() / 1000 - 0.5,
                        connectionInfo: 'h2',
                        finishDocumentLoadTime: Date.now() / 1000 - 0.2,
                        finishLoadTime: Date.now() / 1000 - 0.1,
                        firstPaintAfterLoadTime: 0,
                        firstPaintTime: Date.now() / 1000 - 0.3,
                        navigationType: 'Other',
                        npnNegotiatedProtocol: 'h2',
                        requestTime: Date.now() / 1000 - 0.8,
                        startLoadTime: Date.now() / 1000 - 0.9,
                        wasAlternateProtocolAvailable: false,
                        wasFetchedViaSpdy: true,
                        wasNpnNegotiated: true,
                    };
                },
                csi: function() {
                    return {
                        startE: Date.now() - 1000,
                        onloadT: Date.now(),
                        pageT: 1000 + Math.random() * 500,
                        tran: 15 + Math.floor(Math.random() * 5),
                    };
                },
                app: {
                    isInstalled: false,
                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                },
            };
        }
    } catch (_) {}
})();
"""


def _patch_canvas_noise() -> str:
    """
    Inyecta ruido mínimo en Canvas 2D y WebGL para romper el fingerprint exacto.

    Los sistemas de fingerprinting hacen un hash del resultado de ``toDataURL``
    o ``getImageData``. Variaciones de ±1-2 bits por pixel rompen ese hash
    sin que el ruido sea visible al ojo humano.

    Nota: El ruido se genera una vez por página (no varía entre llamadas) para
    que el fingerprint sea *consistente dentro de la sesión* pero diferente
    entre sesiones.
    """
    return """
(function patchCanvasNoise() {
    // Semilla de ruido: constante por sesión, diferente entre sesiones.
    const NOISE_SEED = Math.floor(Math.random() * 255);

    // ── Canvas 2D ────────────────────────────────────────────────────────
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
        const imageData = origGetImageData.call(this, x, y, w, h);
        // Modifica solo el canal rojo cada 100 píxeles para impacto mínimo.
        for (let i = 0; i < imageData.data.length; i += 400) {
            imageData.data[i] = Math.max(0, Math.min(255, imageData.data[i] ^ (NOISE_SEED & 3)));
        }
        return imageData;
    };

    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
        const ctx = this.getContext('2d');
        if (ctx) {
            // Dibuja un pixel invisible con la semilla para alterar el hash.
            const px = ctx.getImageData(0, 0, 1, 1);
            px.data[0] ^= (NOISE_SEED & 1);
            ctx.putImageData(px, 0, 0);
        }
        return origToDataURL.call(this, type, quality);
    };
})();
"""


def _patch_webgl(vendor: str, renderer: str) -> str:
    """
    Sustituye el vendor y renderer de WebGL por valores de hardware real.

    Las parámetros UNMASKED_VENDOR_WEBGL (37445) y UNMASKED_RENDERER_WEBGL
    (37446) son los más usados para identificar VMs y entornos headless.

    Args:
        vendor:   Cadena de vendor GPU (p.ej. "Intel Inc.").
        renderer: Cadena de renderer GPU (p.ej. "Intel Iris OpenGL Engine").
    """
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

        // Parchea también getSupportedExtensions para ocultar "WEBGL_debug_renderer_info"
        // en algunos fingerprinters avanzados que lo verifican por separado.
        const origGetExts = proto.getSupportedExtensions;
        if (origGetExts) {{
            proto.getSupportedExtensions = function() {{
                const exts = origGetExts.call(this) || [];
                // Mantener la extensión visible: algunos sites la usan legítimamente
                // para seleccionar shaders; filtrarla podría romper rendering.
                return exts;
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
    """
    Inyecta variación mínima en el AudioContext para romper el fingerprint.

    El audio fingerprint se obtiene procesando una señal a través de un
    OscillatorNode → AnalyserNode y haciendo hash del buffer resultante.
    Un delta de ±1e-10 en la señal altera el hash sin afectar el audio real.
    """
    return """
(function patchAudioContext() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;

    const AUDIO_NOISE = (Math.random() * 2e-10) - 1e-10;

    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
        const channelData = origGetChannelData.call(this, channel);
        // Solo altera el primer sample para no degradar la calidad.
        if (channelData.length > 0) {
            channelData[0] += AUDIO_NOISE;
        }
        return channelData;
    };

    // Parchea copyFromChannel también (usado por fingerprinters modernos).
    const origCopyFromChannel = AudioBuffer.prototype.copyFromChannel;
    if (origCopyFromChannel) {
        AudioBuffer.prototype.copyFromChannel = function(dest, channelNum, startInChannel) {
            origCopyFromChannel.call(this, dest, channelNum, startInChannel);
            if (dest.length > 0) dest[0] += AUDIO_NOISE;
        };
    }
})();
"""


def _patch_rtc_ip_leak() -> str:
    """
    Previene el leak de IP real a través de WebRTC (RTCPeerConnection).

    Incluso con proxy/Tor, WebRTC puede revelar la IP real del host mediante
    ICE candidates. Este parche elimina todos los ICE servers de las configs,
    forzando solo candidates de tipo 'host' (que usan la IP del proxy).
    """
    return """
(function patchRTCIPLeak() {
    if (!window.RTCPeerConnection) return;

    const OrigRTC = window.RTCPeerConnection;

    function PatchedRTC(config, constraints) {
        // Eliminar ICE servers previene STUN/TURN que revelarían la IP real.
        if (config && config.iceServers) {
            config = { ...config, iceServers: [] };
        }
        return new OrigRTC(config, constraints);
    }

    // Preservar el prototipo completo para que 'instanceof' siga funcionando.
    PatchedRTC.prototype = OrigRTC.prototype;
    Object.setPrototypeOf(PatchedRTC, OrigRTC);

    // Copiar propiedades estáticas.
    for (const key of Object.getOwnPropertyNames(OrigRTC)) {
        try {
            Object.defineProperty(PatchedRTC, key, Object.getOwnPropertyDescriptor(OrigRTC, key));
        } catch (_) {}
    }

    window.RTCPeerConnection = PatchedRTC;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = PatchedRTC;
})();
"""


def _patch_screen_metrics(width: int, height: int) -> str:
    """
    Hace que ``outerWidth/outerHeight`` sean coherentes con el viewport.

    En un navegador real, ``outerHeight = innerHeight + chrome_frame`` (~85px).
    En headless, outer == inner, señal obvia de bot.

    Args:
        width:  Ancho del viewport configurado.
        height: Alto del viewport configurado.
    """
    chrome_h = 85  # Alto aproximado del chrome frame (toolbar + tabs)
    chrome_w = 0   # Ancho del chrome frame (normalmente 0)
    return f"""
(function patchScreenMetrics() {{
    // outerWidth/outerHeight = viewport + frame del navegador
    try {{
        Object.defineProperty(window, 'outerWidth', {{
            get: () => {width + chrome_w},
            configurable: true,
        }});
        Object.defineProperty(window, 'outerHeight', {{
            get: () => {height + chrome_h},
            configurable: true,
        }});
    }} catch (_) {{}}

    // screen.width/height deben ser >= outerWidth/outerHeight
    try {{
        Object.defineProperty(screen, 'width',       {{ get: () => {width},  configurable: true }});
        Object.defineProperty(screen, 'height',      {{ get: () => {height}, configurable: true }});
        Object.defineProperty(screen, 'availWidth',  {{ get: () => {width},  configurable: true }});
        Object.defineProperty(screen, 'availHeight', {{ get: () => {height - 40}, configurable: true }});
        Object.defineProperty(screen, 'colorDepth',  {{ get: () => 24,       configurable: true }});
        Object.defineProperty(screen, 'pixelDepth',  {{ get: () => 24,       configurable: true }});
    }} catch (_) {{}}
}})();
"""


def _patch_permissions_api() -> str:
    return """
(function patchPermissionsAPI() {
    if (!navigator.permissions || !navigator.permissions.query) return;

    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = function(permDesc) {
        if (!permDesc || !permDesc.name) return origQuery(permDesc);
        
        // En headless real, estos devuelven 'denied' o 'prompt' inconsistente
        const headlessDefaults = {
            'notifications': 'default',
            'camera':        'prompt',
            'microphone':    'prompt',
            'geolocation':   'prompt',
        };
        
        if (permDesc.name in headlessDefaults) {
            return Promise.resolve({
                state: headlessDefaults[permDesc.name],
                onchange: null,
            });
        }
        return origQuery(permDesc);
    };
})();
"""

def _patch_media_devices() -> str:
    """
    Garantiza que ``navigator.mediaDevices`` existe en Chromium headless.

    Chromium headless a veces omite completamente ``navigator.mediaDevices``
    o lo expone con métodos no funcionales. Los fingerprinters avanzados
    (CreepJS, FingerprintJS Pro) usan su ausencia como señal de automatización.

    El parche crea un objeto ``MediaDevices`` con los métodos estándar:
      - ``enumerateDevices()``     → Promise que resuelve a lista vacía
                                     (sin micrófono/cámara en headless).
      - ``getSupportedConstraints()`` → Objeto vacío (ninguna constraint soportada).
      - ``getUserMedia()``         → Promise rechazada (no hay dispositivos reales).
      - ``addEventListener()`` / ``removeEventListener()`` → no-ops.

    Nota: Firefox ya expone ``navigator.mediaDevices`` correctamente en headless,
    por lo que este parche es principalmente para Chromium. Se aplica siempre
    porque ``if (!navigator.mediaDevices)`` lo hace no-op en Firefox.
    """
    return """
(function patchMediaDevices() {
    if (navigator.mediaDevices) return;

    try {
        const fakeMediaDevices = {
            enumerateDevices: function() {
                return Promise.resolve([]);
            },
            getSupportedConstraints: function() {
                return {
                    width: true, height: true, aspectRatio: true,
                    frameRate: true, facingMode: true,
                    echoCancellation: true, noiseSuppression: true,
                    autoGainControl: true, sampleRate: true,
                    sampleSize: true, channelCount: true,
                };
            },
            getUserMedia: function(constraints) {
                return Promise.reject(
                    new DOMException(
                        'Requested device not found',
                        'NotFoundError'
                    )
                );
            },
            addEventListener: function() {},
            removeEventListener: function() {},
            dispatchEvent: function() { return true; },
            ondevicechange: null,
        };

        Object.defineProperty(navigator, 'mediaDevices', {
            get: () => fakeMediaDevices,
            configurable: true,
        });
    } catch (_) {}
})();
"""

def _patch_performance_timing() -> str:
    """
    Reduce la precisión de ``performance.now()`` a 100µs.

    Los ataques de timing pueden identificar VMs por la granularidad exacta
    del reloj. Browsers reales (post-Spectre) ya reducen la precisión;
    headless a veces no.
    """
    return """
(function patchPerformanceTiming() {
    const origNow = performance.now.bind(performance);
    performance.now = function() {
        // Redondear a 100 microsegundos (0.1ms) = comportamiento de Chrome 68+
        return Math.round(origNow() * 10) / 10;
    };
})();
"""


def _patch_iframe_propagation() -> str:
    """
    Propaga los parches de navigator a iframes creados dinámicamente.

    Algunos fingerprinters avanzados crean un iframe y leen
    ``iframe.contentWindow.navigator.webdriver`` que no está parchado.
    Este parche intercepta la creación de iframes para aplicar los mismos
    overrides al contexto hijo.
    """
    return """
(function patchIframePropagation() {
    const origCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName, options) {
        const el = origCreateElement(tagName, options);
        if (tagName && tagName.toLowerCase() === 'iframe') {
            el.addEventListener('load', function() {
                try {
                    const iNav = el.contentWindow.navigator;
                    Object.defineProperty(iNav, 'webdriver', {
                        get: () => undefined,
                        configurable: false,
                    });
                } catch (_) {}
            });
        }
        return el;
    };
})();
"""


def _patch_notification_permission() -> str:
    """Fija ``Notification.permission`` a ``'default'`` en lugar de ``'denied'``."""
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

# ─────────────────────────────────────────────────────────────────────────────
# Compositor público
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
    Compone el script de stealth completo para un fingerprint dado.

    Combina todos los parches individuales en un único script que Playwright
    inyecta vía ``page.add_init_script`` antes de que cargue la página.

    Args:
        browser_type:         "firefox" | "chromium"
        platform:             String de OS (p.ej. "Win32", "MacIntel", "Linux x86_64")
        language:             Accept-Language primario (p.ej. "en-US")
        hardware_concurrency: Número de CPUs lógicas a reportar (4, 8, 12, 16)
        device_memory:        GB de RAM a reportar (4, 8, 16)
        webgl_vendor:         Vendor GPU para WebGL
        webgl_renderer:       Renderer GPU para WebGL
        viewport_width:       Ancho del viewport en píxeles
        viewport_height:      Alto del viewport en píxeles

    Returns:
        String JavaScript listo para inyectar como init script.
    """
    plugin_patch = (
        _patch_plugins_firefox()
        if browser_type == "firefox"
        else _patch_plugins_chromium()
    )

    parts = [
        "/* ═══ Stealth Init Script ═══ */",
        _patch_navigator_core(platform, language, hardware_concurrency, device_memory),
        plugin_patch,
        _patch_canvas_noise(),
        _patch_webgl(webgl_vendor, webgl_renderer),
        _patch_audio_context(),
        _patch_rtc_ip_leak(),
        _patch_screen_metrics(viewport_width, viewport_height),
        _patch_permissions_api(),
        _patch_performance_timing(),
        _patch_notification_permission(),
        _patch_iframe_propagation(),
        _patch_media_devices(),
    ]
    return "\n".join(parts)