"""
utils/proxy_manager.py
Gestión de pool de proxies HTTP/SOCKS5 con rotación, health-checking y backoff.

Diseñado para usarse con proxies externos (listas de IPs de pago o residenciales).
La selección por defecto es weighted-random: los proxies con menos fallos recientes
tienen mayor probabilidad de ser seleccionados.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Proxy entry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProxyEntry:
    """
    Representa un proxy individual con métricas de salud.

    Attributes:
        server:          URL del proxy (p.ej. ``"socks5://127.0.0.1:9050"``).
        failures:        Número de fallos consecutivos desde el último éxito.
        last_failure_ts: Timestamp del último fallo (epoch seconds).
        last_success_ts: Timestamp del último éxito (epoch seconds).
        total_requests:  Total de peticiones enviadas a través de este proxy.
        total_failures:  Total histórico de fallos.
    """
    server: str
    failures: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = 0.0
    total_requests: int = 0
    total_failures: int = 0

    # Umbral: si hay >= N fallos consecutivos, el proxy va a cooldown.
    FAILURE_THRESHOLD: int = field(default=3, init=False, repr=False)
    # Segundos de cooldown antes de reintentar un proxy problemático.
    COOLDOWN_SECONDS: float = field(default=300.0, init=False, repr=False)

    @property
    def is_healthy(self) -> bool:
        """True si el proxy no está en cooldown."""
        if self.failures < self.FAILURE_THRESHOLD:
            return True
        elapsed = time.monotonic() - self.last_failure_ts
        return elapsed > self.COOLDOWN_SECONDS

    @property
    def weight(self) -> float:
        """
        Peso de selección: inversamente proporcional a los fallos recientes.

        Un proxy sin fallos tiene peso 1.0; cada fallo reduce el peso a la mitad.
        """
        return max(0.05, 1.0 / (2 ** self.failures))

    def as_playwright_proxy(self) -> dict[str, str]:
        """Devuelve el formato de proxy que espera Playwright."""
        return {"server": self.server}


# ─────────────────────────────────────────────────────────────────────────────
# ProxyManager
# ─────────────────────────────────────────────────────────────────────────────

class ProxyManager:
    """
    Pool de proxies con selección ponderada, health-checking y backoff.

    Flujo de uso típico::

        pm = ProxyManager(proxy_urls=["socks5://...", "http://..."])
        proxy_entry = await pm.get_proxy()
        try:
            # ... usar proxy_entry.as_playwright_proxy() en new_context()
            await pm.mark_success(proxy_entry)
        except SomeNetworkError:
            await pm.mark_failure(proxy_entry)

    Args:
        proxy_urls:   Lista de URLs de proxy. Lista vacía = solo conexión directa.
        include_direct: Si True, incluye la conexión directa en el pool.
    """

    def __init__(
        self,
        proxy_urls: list[str] | None = None,
        include_direct: bool = False,
    ) -> None:
        self._pool: list[ProxyEntry] = []
        self._lock = asyncio.Lock()

        if include_direct:
            self._pool.append(ProxyEntry(server="direct"))

        for url in (proxy_urls or []):
            url = url.strip()
            if url:
                self._pool.append(ProxyEntry(server=url))

        logger.info(
            "ProxyManager initialized: %d proxies (direct=%s)",
            len(self._pool),
            include_direct,
        )

    # ── Selección ─────────────────────────────────────────────────────────────

    async def get_proxy(self) -> ProxyEntry | None:
        """
        Selecciona un proxy saludable usando weighted-random.

        Returns:
            ``ProxyEntry`` seleccionado, o None si el pool está vacío.
            Si el proxy seleccionado es "direct", devuelve None para indicar
            que se debe usar conexión directa.

        Raises:
            RuntimeError: Si todos los proxies están en cooldown.
        """
        async with self._lock:
            healthy = [p for p in self._pool if p.is_healthy]
            if not healthy:
                if self._pool:
                    # Todos en cooldown: esperar al que tenga cooldown más corto
                    soonest = min(
                        self._pool,
                        key=lambda p: p.last_failure_ts + p.COOLDOWN_SECONDS,
                    )
                    wait_time = max(0.0, soonest.last_failure_ts + soonest.COOLDOWN_SECONDS - time.monotonic())
                    logger.warning(
                        "All proxies in cooldown. Waiting %.0fs for '%s'...",
                        wait_time, soonest.server,
                    )
                    await asyncio.sleep(wait_time)
                    soonest.failures = 0
                    return soonest
                return None

            weights = [p.weight for p in healthy]
            # random.choices con pesos (no requiere import adicional)
            import random
            selected = random.choices(healthy, weights=weights, k=1)[0]
            selected.total_requests += 1
            logger.debug("Proxy selected: %s (weight=%.2f)", selected.server, selected.weight)
            return selected if selected.server != "direct" else None

    # ── Feedback ──────────────────────────────────────────────────────────────

    async def mark_success(self, entry: ProxyEntry) -> None:
        """
        Registra un éxito para el proxy dado, reseteando su contador de fallos.

        Args:
            entry: ProxyEntry que completó la petición correctamente.
        """
        async with self._lock:
            entry.failures = 0
            entry.last_success_ts = time.monotonic()

    async def mark_failure(self, entry: ProxyEntry, reason: str = "") -> None:
        """
        Registra un fallo para el proxy dado, incrementando su contador.

        Args:
            entry:  ProxyEntry que falló.
            reason: Descripción del error (solo para logging).
        """
        async with self._lock:
            entry.failures += 1
            entry.total_failures += 1
            entry.last_failure_ts = time.monotonic()
            logger.warning(
                "Proxy failure #%d: %s | reason=%s",
                entry.failures, entry.server, reason or "unknown",
            )
            if entry.failures >= entry.FAILURE_THRESHOLD:
                logger.warning(
                    "Proxy '%s' entering cooldown (%.0fs).",
                    entry.server, entry.COOLDOWN_SECONDS,
                )

    # ── Estadísticas ──────────────────────────────────────────────────────────

    def get_stats(self) -> list[dict[str, Any]]:
        """
        Devuelve el estado actual de cada proxy en el pool.

        Returns:
            Lista de dicts con server, failures, total_requests, total_failures,
            is_healthy y weight.
        """
        return [
            {
                "server": p.server,
                "failures": p.failures,
                "total_requests": p.total_requests,
                "total_failures": p.total_failures,
                "is_healthy": p.is_healthy,
                "weight": round(p.weight, 3),
            }
            for p in self._pool
        ]