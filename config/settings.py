"""
Lectura de configuración desde config.ini con configparser puro (stdlib).
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
    """

    def __init__(self, ini_path: Path = _INI_PATH) -> None:
        if not ini_path.exists():
            warnings.warn(
                f"config.ini no encontrado en '{ini_path}'. "
                "Se usarán los valores por defecto.",
                stacklevel=2,
            )

        parser = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=(";", "#"),
        )
        parser.read(ini_path, encoding="utf-8")

        def _str(section: str, key: str, fallback: str = "") -> str:
            return parser.get(section, key, fallback=fallback).strip()

        def _bool(section: str, key: str, fallback: bool = False) -> bool:
            return parser.getboolean(section, key, fallback=fallback)

        def _int(section: str, key: str, fallback: int = 0) -> int:
            return parser.getint(section, key, fallback=fallback)

        def _float(section: str, key: str, fallback: float = 0.0) -> float:
            return parser.getfloat(section, key, fallback=fallback)

        # ── [output] ─────────────────────────────────────────────────────────
        self.OUTPUT_MODE: str = _str("output", "mode", "supabase")

        # ── [supabase] ───────────────────────────────────────────────────────
        self.SUPABASE_URL: str = _str("supabase", "url", "")
        self.SUPABASE_KEY: str = _str("supabase", "key", "")

        # ── [api] ────────────────────────────────────────────────────────────
        self.DATA_STORE_BASE_URL: str = _str("api", "data_store_base_url", "")
        self.DATA_STORE_TOKEN: str = _str("api", "data_store_token", "")
        self.DATA_STORE_VERIFY_SSL: bool = _bool("api", "data_store_verify_ssl", True)

        # ── [browser] ────────────────────────────────────────────────────────
        self.BROWSER_TYPE: str = _str("browser", "type", "firefox")
        self.BROWSER_HEADLESS_DEFAULT: bool = _bool("browser", "headless_default", True)
        self.BROWSER_VISIBLE_ON_CAPTCHA: bool = _bool("browser", "visible_on_captcha", True)
        self.CAPTCHA_MANUAL_TIMEOUT: int = _int("browser", "captcha_manual_timeout", 300)
        self.HEADLESS_AFTER_CAPTCHA: bool = _bool("browser", "headless_after_captcha", False)

        # ── [camoufox] ───────────────────────────────────────────────────────
        self.CAMOUFOX_HUMANIZE: bool = _bool("camoufox", "humanize", True)
        self.CAMOUFOX_GEOIP: bool = _bool("camoufox", "geoip", False)
        self.CAMOUFOX_LAUNCH_DELAY: float = _float("camoufox", "launch_delay", 3.0)

        # ── [concurrency] ────────────────────────────────────────────────────
        # Solo max_workers: cada worker usa 1 browser propio
        self.MAX_CONCURRENT_WORKERS: int = _int("concurrency", "max_workers", 2)

        # ── [scraping] ───────────────────────────────────────────────────────
        self.CYCLE_DELAY_SECONDS: int = _int("scraping", "cycle_delay_seconds", 120)
        self.TOTAL_PAGES_PER_KEYWORD: int = _int("scraping", "total_pages_per_keyword", 3)
        self.KEYWORDS_PER_BATCH: int = _int("scraping", "keywords_per_batch", 10)
        self.SCRAPE_COOLDOWN_MINUTES: int = _int("scraping", "scrape_cooldown_minutes", 60)

        # ── [session] ────────────────────────────────────────────────────────
        self.SESSION_PERSIST: bool = _bool("session", "persist", True)

        # ── [paths] ──────────────────────────────────────────────────────────
        self.SESSION_DIR: Path = Path(_str("paths", "session_dir", "sessions"))
        self.LOG_DIR: Path = Path(_str("paths", "log_dir", "logs"))

        # ── [logging] ────────────────────────────────────────────────────────
        self.LOG_LEVEL: str = _str("logging", "level", "INFO").upper()
        self.LOG_FILE: str = _str("logging", "file", "app.log")
        self.LOG_AUDIT_FILE: str = _str("logging", "audit_file", "audit.log")
        self.LOG_MAX_BYTES: int = _int("logging", "max_bytes", 10_485_760)
        self.LOG_BACKUP_COUNT: int = _int("logging", "backup_count", 3)

        # ── [engines] ────────────────────────────────────────────────────────
        self.ENGINES: dict[str, list[dict[str, str]]] = {"facebook": [], "instagram": []}

        if parser.has_section("engines"):
            for key, value in parser.items("engines"):
                value = value.strip()
                if not value:
                    continue

                engine_name = key.strip()
                engine_id = value

                if engine_name.startswith("fb_engine_"):
                    self.ENGINES["facebook"].append(
                        {"name": engine_name, "engine_id": engine_id}
                    )
                elif engine_name.startswith("ig_engine_"):
                    self.ENGINES["instagram"].append(
                        {"name": engine_name, "engine_id": engine_id}
                    )
                else:
                    logger.warning(
                        "Clave de motor desconocida en [engines]: '%s'. "
                        "Use 'fb_engine_N' o 'ig_engine_N'.",
                        engine_name,
                    )

        if not self.ENGINES["facebook"] and not self.ENGINES["instagram"]:
            logger.warning(
                "No se encontraron motores en [engines]. Usando valores por defecto."
            )
            self.ENGINES = {
                "facebook": [
                    {"name": "fb_engine_1", "engine_id": "c4b97eed1414fcb14"},
                    {"name": "fb_engine_2", "engine_id": "a1b2c3d4e5f678901"},
                ],
                "instagram": [
                    {"name": "ig_engine_1", "engine_id": "x9y8z7w6v5u4t3s2"},
                    {"name": "ig_engine_2", "engine_id": "r1q2p3o4n5m6l7k8"},
                ],
            }

    def __repr__(self) -> str:
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
        engines_summary = (
            f"fb={len(self.ENGINES['facebook'])} "
            f"ig={len(self.ENGINES['instagram'])}"
        )
        return (
            f"<Settings "
            f"workers={self.MAX_CONCURRENT_WORKERS} "
            f"batch={self.KEYWORDS_PER_BATCH} "
            f"output={self.OUTPUT_MODE} "
            f"supabase={supabase_url_display} "
            f"engines=({engines_summary}) "
            f"api_token={api_token_display}>"
        )


@lru_cache(maxsize=1)
def _load_settings() -> _Settings:
    instance = _Settings()
    logger.debug("Settings cargados: %r", instance)
    return instance


settings: _Settings = _load_settings()