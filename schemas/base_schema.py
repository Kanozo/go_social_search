"""
schemas/base_schema.py

Tipos base, utilidades y modelos compartidos para toda la aplicación.

CONTENIDO:
    PyObjectId       →  tipo Annotated para campos _id de MongoDB.
    TimestampModel   →  mixin con created_at / updated_at UTC.
    PaginatedResult  →  contenedor genérico para resultados paginados.
    Respuesta        →  wrapper de respuesta genérico para operaciones.
    HealthResponse   →  estado del sistema (DB, versión, etc.).
    ErrorResponse    →  error estándar con código y timestamp.
    validar_fecha    →  helper para parsear fechas desde string.
    validar_hora     →  helper para parsear horas desde string.

DEPENDENCIAS:
    pydantic>=2.0, pymongo / motor (para ObjectId)
"""

from __future__ import annotations

from bson import ObjectId
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, Generic, List, Optional, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
)

T = TypeVar("T")


# ===========================================================================
# Helpers de validación de fechas y horas
# ===========================================================================
def validar_fecha(value: Any) -> Any:
    """
    Parsea una fecha desde string a ``date``.

    Acepta:
        - ``dd-mm-yyyy`` (formato latino).
        - ``yyyy-mm-dd`` (ISO 8601).

    Si el valor ya es una instancia de ``date``, se devuelve sin cambios.

    Args:
        value: String con la fecha o instancia ``date`` nativa.

    Returns:
        Instancia de ``date``.

    Raises:
        ValueError: Si el string no coincide con ninguno de los formatos.
    """
    if not isinstance(value, str):
        return value

    value = value.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    raise ValueError(
        f"Formato de fecha inválido: '{value}'. Use dd-mm-yyyy o yyyy-mm-dd."
    )


def validar_hora(value: Any) -> Any:
    """
    Parsea una hora desde string a ``time``.

    Acepta únicamente ``HH:MM`` (formato 24h).

    Si el valor ya es una instancia de ``time``, se devuelve sin cambios.

    Args:
        value: String con la hora o instancia ``time`` nativa.

    Returns:
        Instancia de ``time``.

    Raises:
        ValueError: Si el string no tiene el formato ``HH:MM``.
    """
    if not isinstance(value, str):
        return value

    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(
            f"Formato de hora inválido: '{value}'. Use HH:MM (24h)."
        )


# ===========================================================================
# PyObjectId — tipo Annotated para campos _id de MongoDB
# ===========================================================================
def _validate_object_id(value: Any) -> str:
    """
    Valida y normaliza un ObjectId de MongoDB a string.

    Args:
        value: ``ObjectId``, string hexadecimal de 24 chars, o cualquier otro tipo.

    Returns:
        Representación en string del ObjectId.

    Raises:
        ValueError: Si el valor no es un ObjectId válido.
    """
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, str) and ObjectId.is_valid(value):
        return str(ObjectId(value))
    raise ValueError(
        f"Se esperaba un ObjectId o string hexadecimal de 24 caracteres, "
        f"se recibió: {type(value).__name__!r}."
    )


PyObjectId = Annotated[
    str,
    BeforeValidator(_validate_object_id),
    PlainSerializer(str, return_type=str),
    WithJsonSchema(
        {
            "type": "string",
            "format": "objectid",
            "pattern": "^[0-9a-fA-F]{24}$",
            "example": "507f1f77bcf86cd799439011",
            "description": "MongoDB ObjectId serializado como string hexadecimal de 24 caracteres.",
        }
    ),
]
"""
Tipo Pydantic v2 para campos ``_id`` de MongoDB.

Acepta ``ObjectId`` o string hexadecimal de 24 chars como entrada.
Siempre serializa como string.

Uso::

    from schemas.base_schema import PyObjectId

    class MiDocumento(TimestampModel):
        id: Optional[PyObjectId] = Field(None, alias="_id")
"""


# ===========================================================================
# TimestampModel — mixin de auditoría
# ===========================================================================
class TimestampModel(BaseModel):
    """
    Mixin que añade campos de auditoría temporal a cualquier modelo.

    ``created_at`` se fija automáticamente al instanciar el modelo y
    nunca debe modificarse manualmente.

    ``updated_at`` es ``None`` hasta que el repositorio realice la primera
    actualización. El método ``BaseRepository.update()`` lo sobreescribe
    automáticamente con la hora real de la operación.

    Attributes:
        created_at: Timestamp UTC de creación del documento.
        updated_at: Timestamp UTC de la última modificación. ``None`` si
                    el documento nunca ha sido actualizado.
    """

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp UTC de creación del documento.",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp UTC de la última actualización. None si nunca fue modificado.",
    )

    model_config = ConfigDict(from_attributes=True)


# ===========================================================================
# Contenedores genéricos
# ===========================================================================
class PaginatedResult(BaseModel, Generic[T]):
    """
    Contenedor genérico para resultados paginados del repositorio.

    Uso::

        items, total = await repo.list(skip=0, limit=10)
        pagina = PaginatedResult[ProductoInDB](
            data=items,
            total=total,
            skip=0,
            limit=10,
        )
        print(f"{len(pagina.data)} de {pagina.total} resultados")

    Attributes:
        data:    Lista de modelos de la página actual.
        total:   Total de documentos que coinciden con el filtro (sin paginación).
        skip:    Offset aplicado en la consulta.
        limit:   Tamaño de página aplicado en la consulta.
        success: Siempre ``True`` cuando se construye sin error.
    """

    data: List[T]
    total: int
    skip: int = 0
    limit: int = 100
    success: bool = True

    model_config = ConfigDict(from_attributes=True)


class Respuesta(BaseModel):
    """
    Wrapper de resultado para operaciones de escritura (create, update, delete).

    Attributes:
        success:   ``True`` si la operación tuvo éxito.
        detail:    Mensaje descriptivo del resultado.
        data:      Payload opcional (ej: ``{"id": "abc123"}``).
        timestamp: Timestamp UTC de cuando se generó la respuesta.
    """

    success: bool
    detail: str = Field(..., description="Descripción del resultado.")
    data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Datos adicionales de la operación (ej: id del documento creado).",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp UTC de la respuesta.",
    )

    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """
    Estado de salud del sistema para diagnóstico.

    Attributes:
        status:    Estado general: ``"ok"`` o ``"degraded"``.
        version:   Versión del sistema o del scraper.
        database:  Estado de la conexión: ``"connected"`` o ``"disconnected"``.
        timestamp: Timestamp UTC de la verificación.
    """

    status: str = Field(..., description="Estado general: 'ok' o 'degraded'.")
    version: str = Field(..., description="Versión del sistema.")
    database: str = Field(..., description="Estado de la conexión a MongoDB.")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp UTC de la verificación.",
    )

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """
    Representación estándar de un error interno.

    Attributes:
        detail:     Descripción legible del error.
        error_code: Código de error interno para logging/trazabilidad.
        timestamp:  Timestamp UTC de cuando ocurrió el error.
    """

    detail: str = Field(..., description="Descripción del error.")
    error_code: Optional[str] = Field(None, description="Código de error interno.")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp UTC del error.",
    )

    model_config = ConfigDict(from_attributes=True)
