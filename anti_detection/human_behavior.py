"""
anti_detection/human_behavior.py
Emulación de comportamiento humano: delays, curvas de ratón, escritura,
scroll natural, distracción y comportamiento idle.

Cada función es ``async`` para integrarse directamente con Playwright.
Los parámetros de timing siguen distribuciones estadísticas (Gauss, exponencial)
en lugar de ``random.uniform`` puro, que produce patrones demasiado uniformes.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Locator, Page

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Delays
# ─────────────────────────────────────────────────────────────────────────────

async def human_delay(min_seconds: float = 2.0, max_seconds: float = 7.0) -> None:
    """
    Pausa con distribución gaussiana + cola exponencial.

    La distribución gaussiana centrada en el midpoint más una pequeña cola
    exponencial replica el comportamiento real de lectura: mayoría del tiempo
    en el centro del rango, con picos ocasionales de distracción.

    Args:
        min_seconds: Límite inferior del rango de pausa.
        max_seconds: Límite superior del rango de pausa.
    """
    mid = (min_seconds + max_seconds) / 2.0
    sigma = (max_seconds - min_seconds) / 6.0
    base = random.gauss(mid, sigma)
    # Cola exponencial: simula momentos en que el usuario se distrae.
    distraction = random.expovariate(5.0) * 0.5
    delay = max(min_seconds, min(max_seconds * 1.5, base + distraction))
    logger.debug("Human delay: %.2fs", delay)
    await asyncio.sleep(delay)


async def micro_delay(min_ms: int = 50, max_ms: int = 350) -> None:
    """
    Micro-pausa entre acciones atómicas (teclas, movimientos de ratón).

    Usa ``random.triangular`` para favorecer valores medios, que son
    más naturales que los extremos.

    Args:
        min_ms: Mínimo en milisegundos.
        max_ms: Máximo en milisegundos.
    """
    delay_ms = random.triangular(min_ms, max_ms, (min_ms + max_ms) // 2)
    await asyncio.sleep(delay_ms / 1000.0)


# ─────────────────────────────────────────────────────────────────────────────
# Movimiento de ratón
# ─────────────────────────────────────────────────────────────────────────────

def _bezier_points(
    start: tuple[float, float],
    end: tuple[float, float],
    num_points: int = 20,
) -> list[tuple[float, float]]:
    """
    Calcula una curva de Bézier cúbica entre dos puntos.

    Los puntos de control aleatorios producen trayectorias orgánicas que
    difieren de las líneas rectas generadas por bots simples.

    Args:
        start:      Coordenada (x, y) de inicio.
        end:        Coordenada (x, y) de destino.
        num_points: Número de puntos intermedios en la curva.

    Returns:
        Lista de coordenadas (x, y) a lo largo de la curva.
    """
    sx, sy = start
    ex, ey = end
    # Puntos de control con variación aleatoria (±40px de la línea recta)
    ctrl1 = (
        sx + (ex - sx) * random.uniform(0.2, 0.5) + random.uniform(-40, 40),
        sy + (ey - sy) * random.uniform(0.1, 0.4) + random.uniform(-40, 40),
    )
    ctrl2 = (
        sx + (ex - sx) * random.uniform(0.5, 0.8) + random.uniform(-40, 40),
        sy + (ey - sy) * random.uniform(0.6, 0.9) + random.uniform(-40, 40),
    )
    points: list[tuple[float, float]] = []
    for i in range(num_points + 1):
        t = i / num_points
        inv = 1 - t
        x = inv**3 * sx + 3 * inv**2 * t * ctrl1[0] + 3 * inv * t**2 * ctrl2[0] + t**3 * ex
        y = inv**3 * sy + 3 * inv**2 * t * ctrl1[1] + 3 * inv * t**2 * ctrl2[1] + t**3 * ey
        points.append((x, y))
    return points


async def human_move_to(page: "Page", target_x: float, target_y: float) -> None:
    """
    Mueve el ratón hasta ``(target_x, target_y)`` describiendo una curva de Bézier.

    La velocidad varía a lo largo de la curva: más lenta al inicio y al final
    (aceleración/desaceleración), más rápida en el tramo central.

    Args:
        page:     Página de Playwright activa.
        target_x: Coordenada X de destino.
        target_y: Coordenada Y de destino.
    """
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    # Punto de partida: centro del viewport con jitter realista
    current_x = viewport["width"] / 2 + random.uniform(-100, 100)
    current_y = viewport["height"] / 2 + random.uniform(-80, 80)

    distance = math.hypot(target_x - current_x, target_y - current_y)
    num_steps = max(15, int(distance / 12))

    curve = _bezier_points((current_x, current_y), (target_x, target_y), num_steps)
    for idx, (px, py) in enumerate(curve):
        await page.mouse.move(px, py)
        # Velocidad variable: más lenta en extremos (easing), más rápida en centro
        progress = idx / num_steps
        easing = math.sin(math.pi * progress)  # 0→1→0 a lo largo de la curva
        step_delay = random.uniform(0.004, 0.018) * (1.5 - easing)
        await asyncio.sleep(step_delay)


# ─────────────────────────────────────────────────────────────────────────────
# Click y escritura
# ─────────────────────────────────────────────────────────────────────────────

async def human_click(
    page: "Page",
    selector: str,
    timeout: int = 10_000,
    move_first: bool = True,
) -> None:
    """
    Hace click en un elemento con movimiento de ratón previo y micro-delay post-click.

    Args:
        page:       Página de Playwright activa.
        selector:   Selector CSS del elemento destino.
        timeout:    Timeout de visibilidad en ms.
        move_first: Si True, mueve el ratón hasta el elemento antes de clickar.
    """
    element = page.locator(selector).first
    await element.wait_for(state="visible", timeout=timeout)
    box = await element.bounding_box()

    if box and move_first:
        # Click en un punto aleatorio dentro del elemento (no siempre el centro)
        target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
        target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
        await human_move_to(page, target_x, target_y)

    # Micro-pausa antes del click (el humano "apunta" antes de presionar)
    await micro_delay(80, 200)
    await element.click()
    # Micro-pausa post-click (reacción natural)
    await micro_delay(50, 150)


async def human_type(
    page: "Page",
    selector: str,
    text: str,
    clear_first: bool = False,
    wpm: int | None = None,
) -> None:
    """
    Escribe texto caracter a caracter con velocidad y errores aleatorios.

    Simula:
    - Velocidad variable (distribución gaussiana alrededor del WPM objetivo)
    - Pausas más largas en espacios y puntuación (el humano "piensa")
    - Errores tipográficos ocasionales (2% de chars) seguidos de Backspace

    Args:
        page:        Página de Playwright activa.
        selector:    Selector CSS del campo de texto.
        text:        Texto a escribir.
        clear_first: Si True, limpia el campo antes de escribir.
        wpm:         Palabras por minuto objetivo. Si None, elige entre 60-120.
    """
    element = page.locator(selector).first
    await element.wait_for(state="visible", timeout=8_000)
    await human_click(page, selector)

    if clear_first:
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.15)
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.1)

    target_wpm = wpm or random.randint(65, 115)
    # Delay base por caracter (asumiendo 5 chars/palabra)
    base_delay = 60.0 / (target_wpm * 5)

    for char in text:
        # Simular error tipográfico (2% de probabilidad)
        if random.random() < 0.02:
            typo_char = random.choice("qwertyuiopasdfghjklzxcvbnm")
            await page.keyboard.type(typo_char)
            # Pausa de "me di cuenta del error"
            await asyncio.sleep(random.uniform(0.12, 0.35))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.15))

        await page.keyboard.type(char)

        # Los espacios y puntuación tienen pausas más largas (pensamiento)
        if char in " .,;:!?\n\t":
            multiplier = random.uniform(1.3, 2.2)
        # Teclas de desplazamiento (mayúsculas) son más lentas
        elif char.isupper():
            multiplier = random.uniform(1.1, 1.6)
        else:
            multiplier = random.uniform(0.5, 1.4)

        await asyncio.sleep(max(0.03, base_delay * multiplier))


# ─────────────────────────────────────────────────────────────────────────────
# Scroll
# ─────────────────────────────────────────────────────────────────────────────

async def human_scroll(
    page: "Page",
    direction: str = "down",
    amount: int | None = None,
) -> None:
    """
    Scroll con velocidad variable: ráfagas rápidas separadas por micro-pausas.

    Un bot suele hacer scroll en pasos fijos y uniformes. Un humano alterna
    ráfagas rápidas con pausas mientras lee.

    Args:
        page:      Página de Playwright activa.
        direction: "down" | "up"
        amount:    Píxeles totales a desplazar. Si None, entre 200 y 800px.
    """
    total = amount or random.randint(200, 800)
    sign = 1 if direction == "down" else -1
    scrolled = 0

    while scrolled < total:
        # Ráfaga de scroll (el usuario arrastra la rueda)
        burst_pixels = random.randint(60, 180)
        step_size = random.randint(20, 60)

        burst_scrolled = 0
        while burst_scrolled < burst_pixels and scrolled < total:
            delta = min(step_size, total - scrolled)
            await page.mouse.wheel(0, sign * delta)
            scrolled += delta
            burst_scrolled += delta
            await asyncio.sleep(random.uniform(0.03, 0.10))

        # Pausa entre ráfagas (el usuario está leyendo)
        await asyncio.sleep(random.uniform(0.15, 0.60))


async def human_scroll_to_element(page: "Page", selector: str) -> None:
    """
    Hace scroll hasta un elemento usando ``scrollIntoView`` + movimiento de ratón.

    Args:
        page:     Página de Playwright activa.
        selector: Selector CSS del elemento destino.
    """
    element = page.locator(selector).first
    await element.scroll_into_view_if_needed()
    await micro_delay(200, 500)
    box = await element.bounding_box()
    if box:
        await human_move_to(
            page,
            box["x"] + box["width"] * random.uniform(0.3, 0.7),
            box["y"] + box["height"] * random.uniform(0.3, 0.7),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Comportamiento idle y distracción
# ─────────────────────────────────────────────────────────────────────────────

async def simulate_idle(page: "Page", duration_seconds: float = 2.0) -> None:
    """
    Simula a un usuario idle: pequeños movimientos del ratón mientras espera.

    Un ratón perfectamente inmóvil durante segundos es una señal de bot.
    Este comportamiento replica la deriva natural del ratón cuando el usuario
    está leyendo la página.

    Args:
        page:             Página de Playwright activa.
        duration_seconds: Cuántos segundos de comportamiento idle simular.
    """
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    # Posición base aleatoria en la mitad superior de la pantalla
    base_x = random.uniform(viewport["width"] * 0.2, viewport["width"] * 0.8)
    base_y = random.uniform(viewport["height"] * 0.1, viewport["height"] * 0.6)

    elapsed = 0.0
    while elapsed < duration_seconds:
        # Deriva pequeña alrededor de la posición base
        jitter_x = base_x + random.gauss(0, 15)
        jitter_y = base_y + random.gauss(0, 10)
        jitter_x = max(0, min(viewport["width"], jitter_x))
        jitter_y = max(0, min(viewport["height"], jitter_y))

        await page.mouse.move(jitter_x, jitter_y)
        pause = random.uniform(0.4, 1.2)
        await asyncio.sleep(pause)
        elapsed += pause


async def simulate_reading_pause(page: "Page", words_estimate: int = 50) -> None:
    """
    Pausa proporcional al tiempo que tardaría un humano en leer el contenido.

    Usa una velocidad de lectura de 200-300 WPM con variación gaussiana,
    y añade comportamiento idle durante la espera.

    Args:
        page:           Página de Playwright activa.
        words_estimate: Estimación de palabras en el contenido visible.
    """
    reading_wpm = random.gauss(250, 40)  # 250 WPM ± 40 (humano promedio)
    reading_seconds = max(1.0, (words_estimate / reading_wpm) * 60)
    logger.debug("Simulating reading pause: %.1fs for ~%d words", reading_seconds, words_estimate)
    await simulate_idle(page, reading_seconds)


async def simulate_distraction(page: "Page") -> None:
    """
    Simula que el usuario se distrajo brevemente (cambió de tab mentalmente).

    Mueve el ratón lejos del contenido principal, pausa, y vuelve.
    Esta señal es muy difícil de replicar para bots y es característica
    del comportamiento humano real.

    Args:
        page: Página de Playwright activa.
    """
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    # Ir a la esquina o al borde de la pantalla
    distraction_spots = [
        (random.uniform(10, 50), random.uniform(10, 50)),          # esquina superior
        (random.uniform(viewport["width"] - 50, viewport["width"] - 10),
         random.uniform(10, 50)),                                    # esquina superior derecha
        (viewport["width"] // 2, random.uniform(5, 20)),           # borde superior
    ]
    spot_x, spot_y = random.choice(distraction_spots)
    await page.mouse.move(spot_x, spot_y)
    # "Olvida" qué estaba haciendo por 0.5-3 segundos
    await asyncio.sleep(random.uniform(0.5, 3.0))


async def simulate_page_focus_blur(page: "Page") -> None:
    """
    Simula eventos de foco/desenfoque de página (el usuario cambia de pestaña).

    Algunos sistemas de detección monitorizan si la página recibe eventos
    de visibilidad (``visibilitychange``). Un bot nunca pierde el foco;
    un humano lo pierde regularmente.

    Args:
        page: Página de Playwright activa.
    """
    try:
        # Simula que el usuario cambia a otra pestaña brevemente
        await page.evaluate("""
            document.dispatchEvent(new Event('visibilitychange'));
            Object.defineProperty(document, 'hidden', { get: () => true, configurable: true });
        """)
        await asyncio.sleep(random.uniform(0.5, 2.0))
        # Vuelve a la pestaña
        await page.evaluate("""
            Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
            document.dispatchEvent(new Event('visibilitychange'));
            window.dispatchEvent(new Event('focus'));
        """)
    except Exception as exc:
        logger.debug("simulate_page_focus_blur: %s", exc)