"""Modelos de datos para Supabase."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class KeywordRecord(BaseModel):
    """Registro de keyword en Supabase."""
    id: int
    scraped_at: datetime
    keyword: str | None = None
    platform: str | None = None
    scraping: bool = False
    engine: str | None = None
    label: str | None = None


class KeywordClaimResult(BaseModel):
    """Resultado de reclamar keywords para scraping."""
    id: int
    keyword: str
    platform: str
    engine: str
    label: str


class UrlRecord(BaseModel):
    """URL a insertar en Supabase."""
    url: str
    keyword: str
    platform: str
    send_tg: bool = False