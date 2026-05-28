"""
database/models.py
Modelos de datos como dataclasses tipados.

No se usa ningún ORM: las filas SQLite se mapean manualmente a estas
clases en los repositorios. Esto mantiene la dependencia en cero
(solo stdlib + aiosqlite) y el control total sobre las queries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ─────────────────────────────────────────────────────────────────────────────
# Tipos
# ─────────────────────────────────────────────────────────────────────────────

Classification = Literal["positivo", "negativo", "neutro"]


# ─────────────────────────────────────────────────────────────────────────────
# Keyword
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Keyword:
    """
    Representa una keyword de búsqueda almacenada en la tabla ``keywords``.

    Attributes:
        keyword:        Término de búsqueda (UNIQUE en DB).
        label:          Etiqueta descriptiva del grupo al que pertenece.
        platform:       Plataforma objetivo (p.ej. "instagram", "facebook").
        engine_id:      ID del Google Custom Search Engine asociado.
        classification: Clasificación semántica del keyword.
        last_scrap:     Última vez que se procesó este keyword (UTC).
        id:             Clave primaria autogenerada. None antes de insertar.
    """
    keyword:        str
    label:          str
    platform:       str
    engine_id:      str
    classification: Classification = "neutro"
    last_scrap:     datetime | None = None
    id:             int | None = field(default=None, repr=False)


@dataclass
class KeywordCreate:
    """DTO para insertar un nuevo keyword (sin id ni last_scrap)."""
    keyword:        str
    label:          str
    platform:       str
    engine_id:      str
    classification: Classification = "neutro"


# ─────────────────────────────────────────────────────────────────────────────
# Post
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Post:
    """
    Representa la URL de un post scrapeado, almacenada en la tabla ``posts``.

    Ciclo de vida de los campos datetime:
      - ``scrapt_at``:  se fija al insertar (momento del scraping).
      - ``was_sent``:   se fija cuando el post entra en cola de envío.
      - ``sent_at``:    se fija cuando el envío se confirma como exitoso.
      Los tres pueden ser None si aún no ocurrió el evento correspondiente.

    Attributes:
        url:       URL canónica del post (UNIQUE en DB).
        keyword:   Keyword que originó este resultado.
        platform:  Plataforma de la URL (p.ej. "instagram", "facebook").
        scrapt_at: Timestamp UTC del scraping. None solo antes de insertar.
        was_sent:  Timestamp UTC en que se marcó para envío.
        sent_at:   Timestamp UTC de confirmación de envío exitoso.
        id:        Clave primaria autogenerada. None antes de insertar.
    """
    url:       str
    keyword:   str
    platform:  str
    scrapt_at: datetime | None = None
    was_sent:  datetime | None = None
    sent_at:   datetime | None = None
    id:        int | None = field(default=None, repr=False)


@dataclass
class PostCreate:
    """DTO para insertar un nuevo post. ``scrapt_at`` se asigna en el repo."""
    url:      str
    keyword:  str
    platform: str