"""
config/settings.py
Configuración centralizada de la aplicación.

Todos los parámetros sensibles o tunables se leen primero de variables de
entorno y caen en valores por defecto razonables si no existen.
"""
from __future__ import annotations

import os
from pathlib import Path
from pydantic import Field

# ─────────────────────────────────────────────────────────────────────────────
# Rutas base
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).resolve().parent.parent
SESSION_DIR: Path = BASE_DIR / "sessions"
LOG_DIR: Path = BASE_DIR / "logs"

SESSION_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


class _Settings:
    """
    Configuración de la aplicación. Prioridad: Env vars > .env > defaults.
    """

    # ── Ciclo de Scraping ─────────────────────────────────────────────────
    CYCLE_DELAY_SECONDS: int = Field(
        default=3600,
        ge=60,
        description="Segundos entre ciclos de scraping (mín. 60)",
    )

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_DIR: Path = Field(
        default=Path("logs"),
        description="Directorio base para archivos de log",
    )
    LOG_FILE: str = Field(
        default="app.log",
        description="Nombre del archivo de log principal",
    )
    LOG_AUDIT_FILE: str = Field(
        default="audit.log",
        description="Nombre del archivo de log de auditoría",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
        description="Nivel mínimo de logging",
    )
    LOG_MAX_BYTES: int = Field(
        default=10_485_760,  # 10 MB
        ge=1_048_576,
        description="Tamaño máximo por archivo de log",
    )
    LOG_BACKUP_COUNT: int = Field(
        default=3,
        ge=1,
        description="Archivos de backup a mantener",
    )

    # ── Colecciones ───────────────────────────────────────────────────────
    GOOGLE_RESULTS_COLLECTION: str = Field(
        default="google_results",
        description="Colección para resultados de Google CSE",
    )

    # ── Ciclo principal ───────────────────────────────────────────────────────
    CYCLE_DELAY_SECONDS: int = int(os.getenv("CYCLE_DELAY_SECONDS", "3600"))
    TOTAL_PAGES_PER_KEYWORD: int = int(os.getenv("TOTAL_PAGES_PER_KEYWORD", "3"))

    # ── Data Store API ───────────────────────────────────────────────────────
    # ⚠️  NUNCA hardcodear tokens en código fuente — usa variables de entorno.
    DATA_STORE_TOKEN: str = os.getenv("DATA_STORE_TOKEN", "42|htoFv3uJ8ZIJMuWoSDQkmLOK0vnv5GSoGbQaKDWBf2cb6b41")
    DATA_STORE_BASE_URL: str = os.getenv(
        "DATA_STORE_BASE_URL", "https://notires.rem.cu/api"
    )
    DATA_STORE_VERIFY_SSL: bool = os.getenv("DATA_STORE_VERIFY_SSL", "false").lower() == "true"

    # ── Browser ───────────────────────────────────────────────────────────────
    BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    BROWSER_TYPE: str = os.getenv("BROWSER_TYPE", "firefox")  # "firefox" | "chromium"

    # ── Sesiones persistentes ────────────────────────────────────────────────
    SESSION_PERSIST: bool = os.getenv("SESSION_PERSIST", "true").lower() == "true"
    SESSION_DIR: Path = SESSION_DIR

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: Path = LOG_DIR


settings = _Settings()