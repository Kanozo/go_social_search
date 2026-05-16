"""
utils/session_store.py
Persistencia de sesiones de navegador usando Playwright storage state.

Playwright puede serializar todo el estado de un contexto (cookies,
localStorage, sessionStorage) en JSON. Guardar y restaurar este estado
entre ejecuciones simula un usuario que vuelve a la misma web con su
historial de navegación, lo cual es mucho menos sospechoso que arrancar
siempre con un contexto limpio.

Estructura de archivos::

    sessions/
    ├── google_com.json
    ├── facebook_com.json
    └── ...

Cada archivo es el ``storage_state`` de Playwright para ese dominio.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)


def _domain_to_filename(domain: str) -> str:
    """
    Convierte un dominio o URL a un nombre de archivo seguro.

    Args:
        domain: URL completa o solo el dominio (p.ej. "https://google.com" o "google.com").

    Returns:
        Nombre de archivo seguro (p.ej. "google_com.json").

    Example:
        >>> _domain_to_filename("https://www.google.com/search")
        'google_com.json'
    """
    # Extraer solo el dominio si se pasa una URL completa
    domain = re.sub(r"https?://", "", domain)
    domain = domain.split("/")[0]
    domain = re.sub(r"^www\.", "", domain)
    # Reemplazar caracteres no alfanuméricos por _
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", domain)
    return f"{safe_name}.json"


class SessionStore:
    """
    Guarda y restaura el estado de sesión de un BrowserContext de Playwright.

    La persistencia hace que el scraper parezca un usuario recurrente en
    lugar de una sesión de bot recién iniciada (sin cookies, sin historial).

    Args:
        storage_path: Directorio donde se guardan los ficheros de sesión.
                      Se crea automáticamente si no existe.

    Example::

        store = SessionStore(Path("sessions"))
        # Al inicio: cargar sesión guardada si existe
        loaded = await store.load(context, "https://google.com")
        # Al finalizar: guardar el estado actualizado
        await store.save(context, "https://google.com")
    """

    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path
        self._path.mkdir(parents=True, exist_ok=True)

    def _session_file(self, domain: str) -> Path:
        return self._path / _domain_to_filename(domain)

    async def save(self, context: "BrowserContext", domain: str) -> bool:
        """
        Serializa y guarda el storage_state del contexto en disco.

        Incluye cookies, localStorage y sessionStorage de todas las origins
        que el contexto ha visitado.

        Args:
            context: BrowserContext de Playwright activo.
            domain:  Identificador de dominio para el nombre de archivo.

        Returns:
            True si el guardado fue exitoso, False si falló.
        """
        session_file = self._session_file(domain)
        try:
            state: dict[str, Any] = await context.storage_state()
            with session_file.open("w", encoding="utf-8") as fp:
                json.dump(state, fp, indent=2, ensure_ascii=False)
            cookie_count = len(state.get("cookies", []))
            logger.debug(
                "Session saved: %s (%d cookies)", session_file.name, cookie_count
            )
            return True
        except Exception as exc:
            logger.warning("Failed to save session '%s': %s", session_file.name, exc)
            return False

    def load_state_dict(self, domain: str) -> dict[str, Any] | None:
        """
        Carga el storage_state desde disco sin necesidad de un contexto activo.

        Útil para pasar directamente a ``browser.new_context(storage_state=...)``.

        Args:
            domain: Identificador de dominio.

        Returns:
            Dict de storage_state, o None si no existe sesión guardada.
        """
        session_file = self._session_file(domain)
        if not session_file.exists():
            logger.debug("No saved session found for '%s'.", domain)
            return None
        try:
            with session_file.open("r", encoding="utf-8") as fp:
                state: dict[str, Any] = json.load(fp)
            cookie_count = len(state.get("cookies", []))
            logger.info(
                "Session loaded: %s (%d cookies)", session_file.name, cookie_count
            )
            return state
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load session '%s': %s", session_file.name, exc)
            session_file.unlink(missing_ok=True)  # Borrar sesión corrupta
            return None

    def delete(self, domain: str) -> None:
        """
        Borra la sesión guardada para un dominio.

        Útil cuando la sesión queda inválida (cuenta baneada, cookie expirada).

        Args:
            domain: Identificador de dominio.
        """
        session_file = self._session_file(domain)
        session_file.unlink(missing_ok=True)
        logger.info("Session deleted: %s", session_file.name)

    def list_sessions(self) -> list[str]:
        """
        Lista todos los nombres de archivo de sesión guardados.

        Returns:
            Lista de nombres de archivo (p.ej. ["google_com.json"]).
        """
        return [f.name for f in self._path.glob("*.json")]