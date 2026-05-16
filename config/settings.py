"""
config/settings.py
Configuración centralizada de la aplicación.

Todos los parámetros se leen primero de variables de entorno y caen
en valores por defecto razonables si no existen.

Variables de entorno relevantes::

    # Modo de salida: dónde se persisten los resultados scrapeados
    OUTPUT_MODE=mongodb          # "mongodb" | "api"

    # MongoDB (solo necesario si OUTPUT_MODE=mongodb)
    MONGO_URL=mongodb://localhost:27017
    DB_NAME=scraper_db

    # API externa (solo necesario si OUTPUT_MODE=api)
    DATA_STORE_TOKEN=<token>
    DATA_STORE_BASE_URL=https://notires.rem.cu/api
    DATA_STORE_VERIFY_SSL=false

    # Browser
    BROWSER_HEADLESS=false
    BROWSER_TYPE=firefox          # "firefox" | "chromium"

    # Ciclo de scraping
    CYCLE_DELAY_SECONDS=3600
    TOTAL_PAGES_PER_KEYWORD=3

    # Sesiones
    SESSION_PERSIST=true

    # Logging
    LOG_LEVEL=INFO
"""
from __future__ import annotations

import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Rutas base (calculadas en tiempo de importación, independiente de env vars)
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).resolve().parent.parent
SESSION_DIR: Path = BASE_DIR / "sessions"
LOG_DIR: Path = BASE_DIR / "logs"

SESSION_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _bool_env(key: str, default: bool) -> bool:
    """Lee una variable de entorno como booleano (true/false, case-insensitive)."""
    return os.getenv(key, str(default)).lower() == "true"


def _int_env(key: str, default: int) -> int:
    """Lee una variable de entorno como entero, con fallback al default."""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class _Settings:
    """
    Contenedor de configuración de la aplicación.

    Prioridad de resolución: variable de entorno > valor por defecto.
    Todos los atributos son de tipo Python nativo (str, int, bool, Path),
    nunca descriptores de Pydantic — esta no es un BaseModel.

    Para cambiar un valor en runtime, exporta la variable de entorno
    correspondiente ANTES de importar este módulo.
    """

    # ── Modo de salida ────────────────────────────────────────────────────────
    # Controla dónde se persisten los resultados scrapeados.
    # "mongodb" → inserta en MongoDB vía GoogleResultRepository
    # "api"     → envía al endpoint HTTP externo vía httpx
    OUTPUT_MODE: str = os.getenv("OUTPUT_MODE", "mongodb")

    # ── MongoDB ───────────────────────────────────────────────────────────────
    MONGO_URL: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    DB_NAME:   str = os.getenv("DB_NAME", "reaper_db")

    # ── API externa ───────────────────────────────────────────────────────────
    # ⚠️  NUNCA hardcodear tokens aquí — usa la variable de entorno DATA_STORE_TOKEN.
    DATA_STORE_TOKEN:      str  = os.getenv("DATA_STORE_TOKEN", "42|htoFv3uJ8ZIJMuWoSDQkmLOK0vnv5GSoGbQaKDWBf2cb6b41")
    DATA_STORE_BASE_URL:   str  = os.getenv("DATA_STORE_BASE_URL", "https://notires.rem.cu/api")
    DATA_STORE_VERIFY_SSL: bool = _bool_env("DATA_STORE_VERIFY_SSL", False)

    # ── Browser ───────────────────────────────────────────────────────────────
    BROWSER_HEADLESS: bool = _bool_env("BROWSER_HEADLESS", False)
    BROWSER_TYPE:     str  = os.getenv("BROWSER_TYPE", "firefox")  # "firefox" | "chromium"

    # ── Ciclo de scraping ─────────────────────────────────────────────────────
    CYCLE_DELAY_SECONDS:     int = _int_env("CYCLE_DELAY_SECONDS", 3600)
    TOTAL_PAGES_PER_KEYWORD: int = _int_env("TOTAL_PAGES_PER_KEYWORD", 3)

    # ── Sesiones persistentes ─────────────────────────────────────────────────
    SESSION_PERSIST: bool = _bool_env("SESSION_PERSIST", True)
    SESSION_DIR:     Path = SESSION_DIR

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str  = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR:   Path = LOG_DIR
    LOG_FILE:  str  = os.getenv("LOG_FILE", "app.log")

    LOG_AUDIT_FILE: str = os.getenv("LOG_AUDIT_FILE", "audit.log")

    LOG_MAX_BYTES: int = os.getenv("LOG_MAX_BYTES", 10_485_760)
    LOG_BACKUP_COUNT: int = os.getenv("LOG_BACKUP_COUNT", 3)


    # ── Colecciones MongoDB ───────────────────────────────────────────────────
    GOOGLE_RESULTS_COLLECTION: str = os.getenv("GOOGLE_RESULTS_COLLECTION", "google_results")
    # ── Pool de conexiones MongoDB ────────────────────────────────────────
    MAX_POOL_SIZE: int = os.getenv("MAX_POOL_SIZE", 10)
    MIN_POOL_SIZE: int = os.getenv("MIN_POOL_SIZE", 1)

    TO_ENDPOINT: bool = _bool_env("TO_ENDPOINT", False)

    def __repr__(self) -> str:
        """Representación segura: oculta el token del API."""
        token_preview = f"{self.DATA_STORE_TOKEN[:6]}..." if self.DATA_STORE_TOKEN else "(vacío)"
        return (
            f"<Settings OUTPUT_MODE={self.OUTPUT_MODE!r} "
            f"BROWSER_TYPE={self.BROWSER_TYPE!r} "
            f"LOG_LEVEL={self.LOG_LEVEL!r} "
            f"DATA_STORE_TOKEN={token_preview}>"
        )


settings = _Settings()