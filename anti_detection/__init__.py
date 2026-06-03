"""
anti_detection/
Módulos de evasión de detección de scraping avanzados con soporte multiplataforma.

Exports públicos:
  - generate_fingerprint   → Genera un BrowserFingerprint coherente e inmutable
  - BrowserFingerprint     → Dataclass con la configuración del entorno y opciones de contexto
  - human_delay            → Pausa con distribución gaussiana y cola exponencial
  - micro_delay            → Micro-pausa atómica adaptativa
  - human_move_to          → Movimiento de ratón con curva de Bézier
  - human_click            → Click orgánico con movimiento cinemático previo
  - human_type             → Escritura con velocidad variable y corrección de errores
  - human_scroll           → Desplazamiento vertical simulado
  - simulate_idle          → Simulación de deriva de ratón en inactividad
  - simulate_reading_pause → Pausa reactiva proporcional a la densidad del texto
  - simulate_distraction   → Pérdida transitoria de foco o desvío de cursor
"""
from __future__ import annotations

from anti_detection.fingerprint import BrowserFingerprint, generate_fingerprint
from anti_detection.human_behavior import (
    human_click,
    human_delay,
    human_move_to,
    human_scroll,
    human_type,
    micro_delay,
    simulate_distraction,
    simulate_idle,
    simulate_reading_pause,
    simulate_page_focus_blur
)

__all__ = [
    "BrowserFingerprint",
    "generate_fingerprint",
    "human_click",
    "human_delay",
    "human_move_to",
    "human_scroll",
    "human_type",
    "micro_delay",
    "simulate_distraction",
    "simulate_idle",
    "simulate_reading_pause",
    "simulate_page_focus_blur"
]