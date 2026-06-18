"""
config/settings.py
Lectura de configuración desde config.ini con configparser puro (stdlib).

Sin dependencias externas. El fichero config.ini es el contrato estable;
este módulo mapea sus secciones/claves a atributos tipados de _Settings.
"""
from __future__ import annotations

import logging
import warnings
from functools import lru_cache
from pathlib import Path

import configparser

logger = logging.getLogger(__name__)

_INI_PATH: Path = Path(__file__).parent.parent / "config.ini"


class _Settings:
    """
    Configuración completa del scraper leída desde config.ini.

    Atributos tipados con valores por defecto que replican los del ini.
    Las secciones y claves coinciden exactamente con config.ini.
    """

    def __init__(self, ini_path: Path = _INI_PATH) -> None:
        if not ini_path.exists():
            warnings.warn(
                f"config.ini no encontrado en '{ini_path}'. "
                "Se usarán los valores por defecto para todos los parámetros.",
                stacklevel=2,
            )

        parser = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=(";", "#"),
        )
        parser.read(ini_path, encoding="utf-8")

        # ── Helpers internos ─────────────────────────────────────────────────

        def _str(section: str, key: str, fallback: str = "") -> str:
            return parser.get(section, key, fallback=fallback).strip()

        def _bool(section: str, key: str, fallback: bool = False) -> bool:
            return parser.getboolean(section, key, fallback=fallback)

        def _int(section: str, key: str, fallback: int = 0) -> int:
            return parser.getint(section, key, fallback=fallback)

        def _float(section: str, key: str, fallback: float = 0.0) -> float:
            return parser.getfloat(section, key, fallback=fallback)

        # ── [paths] ──────────────────────────────────────────────────────────
        # Sección opcional; si no existe usa rutas relativas al CWD.
        self.SESSION_DIR: Path = Path(_str("paths", "session_dir", "sessions"))
        self.LOG_DIR: Path = Path(_str("paths", "log_dir", "logs"))

        # ── [logging] ────────────────────────────────────────────────────────
        self.LOG_LEVEL: str = _str("logging", "level", "INFO").upper()
        self.LOG_FILE: str = _str("logging", "file", "app.log")
        self.LOG_AUDIT_FILE: str = _str("logging", "audit_file", "audit.log")
        self.LOG_MAX_BYTES: int = _int("logging", "max_bytes", 10_485_760)
        self.LOG_BACKUP_COUNT: int = _int("logging", "backup_count", 3)

        # ── [browser] ────────────────────────────────────────────────────────
        self.BROWSER_TYPE: str = _str("browser", "type", "firefox")
        self.BROWSER_HEADLESS_DEFAULT: bool = _bool("browser", "headless_default", True)
        self.BROWSER_VISIBLE_ON_CAPTCHA: bool = _bool("browser", "visible_on_captcha", True)
        self.HEADLESS_AFTER_CAPTCHA: bool = _bool("browser", "headless_after_captcha", False)
        self.CAPTCHA_MANUAL_TIMEOUT: int = _int("browser", "captcha_manual_timeout", 300)

        # ── [camoufox] ───────────────────────────────────────────────────────
        self.CAMOUFOX_HUMANIZE: bool = _bool("camoufox", "humanize", True)
        self.CAMOUFOX_GEOIP: bool = _bool("camoufox", "geoip", False)
        self.CAMOUFOX_LAUNCH_DELAY: float = _float("camoufox", "launch_delay", 3.0)

        # ── [concurrency] ────────────────────────────────────────────────────
        self.MAX_CONCURRENT_BROWSERS: int = _int("concurrency", "max_browsers", 2)

        # ── [scraping] ───────────────────────────────────────────────────────
        self.TOTAL_PAGES_PER_KEYWORD: int = _int("scraping", "total_pages_per_keyword", 3)
        self.CYCLE_DELAY_SECONDS: int = _int("scraping", "cycle_delay_seconds", 3600)

        # ── [session] ────────────────────────────────────────────────────────
        self.SESSION_PERSIST: bool = _bool("session", "persist", True)

        # ── [output] ─────────────────────────────────────────────────────────
        self.OUTPUT_MODE: str = _str("output", "mode", "supabase")

        # ── [supabase] ───────────────────────────────────────────────────────
        self.SUPABASE_URL: str = _str("supabase", "url", "")
        self.SUPABASE_KEY: str = _str("supabase", "key", "")

        # ── [api] ────────────────────────────────────────────────────────────
        self.DATA_STORE_BASE_URL: str = _str("api", "data_store_base_url", "")
        self.DATA_STORE_TOKEN: str = _str("api", "data_store_token", "")
        self.DATA_STORE_VERIFY_SSL: bool = _bool("api", "data_store_verify_ssl", True)

    def __repr__(self) -> str:
        """Oculta tokens en repr para que no aparezcan en logs."""
        supabase_url_display = (
            f"{self.SUPABASE_URL[:30]}..."
            if len(self.SUPABASE_URL) > 30
            else self.SUPABASE_URL or "(vacío)"
        )
        api_token_display = (
            f"{'*' * 8}{self.DATA_STORE_TOKEN[-4:]}"
            if self.DATA_STORE_TOKEN
            else "(vacío)"
        )
        return (
            f"<Settings "
            f"browser={self.BROWSER_TYPE} "
            f"headless={self.BROWSER_HEADLESS_DEFAULT} "
            f"output={self.OUTPUT_MODE} "
            f"supabase={supabase_url_display} "
            f"concurrent={self.MAX_CONCURRENT_BROWSERS} "
            f"api_token={api_token_display}>"
        )

@lru_cache(maxsize=1)
def _load_settings() -> _Settings:
    """
    Carga y cachea la instancia singleton de _Settings.

    El ini se parsea exactamente una vez por proceso. En tests:
        _load_settings.cache_clear()
        settings = _load_settings()
    """
    instance = _Settings()
    logger.debug("Settings cargados: %r", instance)
    return instance


# Singleton de acceso directo: from config.settings import settings
settings: _Settings = _load_settings()