"""
schemas/google_result_schema.py
Schema para resultados de Google Custom Search Engine (CSE).
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field
from schemas.base_schema import PyObjectId, TimestampModel


class GoogleResultBase(BaseModel):
    """Campos base de un resultado de búsqueda de Google CSE."""
    url: str = Field(
        ...,
        max_length=2000,
        description="URL canónica del resultado.",
        examples=["https://example.com/article"]
    )
    published_at: Optional[datetime] = Field(
        None,
        description="Fecha de publicación parseada a datetime UTC.",
    )
    published_at_raw: Optional[str] = Field(
        None,
        max_length=100,
        description="Timestamp relativo original (ej: 'hace 5 horas').",
        examples=["hace 3 días", "hace 2 horas"]
    )
    keyword: str = Field(
        ...,
        max_length=200,
        description="Término de búsqueda que generó este resultado.",
        examples=["cuba", "economía latinoamericana"]
    )

    model_config = ConfigDict(from_attributes=True)


class GoogleResultCreate(GoogleResultBase):
    """Payload para crear un nuevo resultado en la base de datos."""
    model_config = ConfigDict(extra="forbid")


class GoogleResultUpdate(BaseModel):
    """Payload para actualización parcial (PATCH) de un resultado."""
    url: Optional[str] = Field(None, max_length=2000)
    published_at: Optional[datetime] = None
    published_at_raw: Optional[str] = Field(None, max_length=100)

    model_config = ConfigDict(extra="ignore")


class GoogleResultInDB(GoogleResultBase, TimestampModel):
    """
    Documento completo tal como se almacena en MongoDB.
    
    Attributes:
        id: ObjectId del documento como string (alias de _id).
        inserted_at: Fecha/hora exacta en que el scraper insertó este resultado.
                    Siempre en UTC. Se asigna automáticamente al crear.
        created_at / updated_at: Timestamps de auditoría de TimestampModel.
    """
    id: Optional[PyObjectId] = Field(None, alias="_id")

    processed: bool = Field(
        default=False,
        description="Indica si la URL ya fue enviada a Telegram."
    )
    
    # ── NUEVO: Fecha de inserción explícita del scraper ───────────────────
    inserted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Fecha/hora UTC en que el scraper insertó este resultado.",
    )

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
    )