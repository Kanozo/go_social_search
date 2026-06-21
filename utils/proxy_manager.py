"""
Gestor de proxys para el scraper.
Si no se configura un proxy válido, devuelve None para usar conexión directa.
"""
from __future__ import annotations
import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


class ProxyManager:
    """Gestiona la configuración de proxys para Playwright/Camoufox."""

    def __init__(self) -> None:
        self._enabled: bool = getattr(settings, "PROXY_ENABLED", False)
        self._server: str | None = getattr(settings, "PROXY_SERVER", None)
        self._username: str | None = getattr(settings, "PROXY_USERNAME", None)
        self._password: str | None = getattr(settings, "PROXY_PASSWORD", None)

    @property
    def is_enabled(self) -> bool:
        return self._enabled and bool(self._server)

    @property
    def playwright_proxy(self) -> dict[str, Any] | None:
        """
        Devuelve el diccionario de proxy compatible con Playwright/Camoufox.
        """
        if not self.is_enabled:
            return None

        proxy_config: dict[str, Any] = {
            "server": self._server,
        }

        if self._username and self._password:
            proxy_config["username"] = self._username
            proxy_config["password"] = self._password

        logger.info("Proxy configurado: %s", self._server)
        return proxy_config


# Instancia singleton para importar en otros módulos
proxy_manager = ProxyManager()