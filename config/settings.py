"""
config/settings.py
Configuración centralizada mediante Pydantic Settings.
Carga variables de entorno, archivo .env y valida tipos al inicio.
"""
from __future__ import annotations

from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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

    # ── Google CSE (opcional) ─────────────────────────────────────────────
    GOOGLE_CSE_ID: str | None = Field(
        default=None,
        description="ID del Custom Search Engine (opcional)",
    )

    # ── Configuración Pydantic ────────────────────────────────────────────
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Validadores ───────────────────────────────────────────────────────
    @field_validator("LOG_DIR", mode="before")
    @classmethod
    def _resolve_log_dir(cls, v: str | Path) -> Path:
        """Resuelve LOG_DIR como absoluto relativo a la raíz del proyecto."""
        path = Path(v) if isinstance(v, str) else v
        if not path.is_absolute():
            return Path(__file__).parent.parent / path
        return path


# Singleton inmutable para toda la aplicación
settings = Settings()
