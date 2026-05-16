"""
anti_detection/
Módulos de evasión de detección de scraping.

Exports públicos:
  - generate_fingerprint   → Genera un BrowserFingerprint coherente
  - BrowserFingerprint     → Dataclass con todas las propiedades del fingerprint
  - human_delay            → Pausa con distribución gaussiana
  - micro_delay            → Micro-pausa entre acciones atómicas
  - human_move_to          → Movimiento de ratón con curva de Bézier
  - human_click            → Click con movimiento previo
  - human_type             → Escritura con velocidad y errores realistas
  - human_scroll           → Scroll con velocidad variable
  - simulate_idle          → Deriva de ratón mientras el usuario lee
  - simulate_reading_pause → Pausa proporcional al texto visible
  - simulate_distraction   → Simula distracción momentánea del usuario
"""
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
]