"""Modelos de datos para Supabase."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class KeywordRecord(BaseModel):
    """Registro de keyword en Supabase."""
    id: int
    term: str
    scraped_at: Optional[datetime] = None


class KeywordBatch(BaseModel):
    """Lote de keywords reclamadas para procesamiento."""
    keywords: list[KeywordRecord]


class EngineConfig(BaseModel):
    """Configuración de un motor de búsqueda."""
    name: str
    engine_id: str
    platform: str  # facebook | instagram (el motor pertenece a una plataforma)


class ScrapedResult(BaseModel):
    """Resultado de scraping de una URL."""
    url: str
    platform: str  # La plataforma viene del motor, no de la keyword
    published_at: Optional[datetime] = None
    published_at_raw: Optional[str] = None