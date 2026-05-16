"""
database/core_db.py

Capa de acceso a datos con Motor (async MongoDB) y arquitectura Repository.

DISEÑO:
    DatabaseManager  →  gestiona ciclo de vida de la conexión (1 instancia por app)
    BaseRepository   →  CRUD genérico y tipado con Pydantic v2 (1 instancia por colección)

FLUJO DE INICIO OBLIGATORIO:
    1. db_manager = DatabaseManager(url, db_name)
    2. await db_manager.connect()
    3. repo = MiRepositorio(db_manager)
    4. await repo.initialize()   ← crea índices y cachea la colección

DEPENDENCIAS:
    motor>=3.3, pymongo>=4.6, pydantic>=2.0
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, Set, Tuple, Type, TypeVar, Union

from bson import ObjectId
from motor.core import AgnosticCollection as AsyncCollection
from motor.core import AgnosticDatabase
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure, ServerSelectionTimeoutError

from config.settings import settings
from log_config import logger_database

# ---------------------------------------------------------------------------
# Tipo genérico acotado a modelos Pydantic
# ---------------------------------------------------------------------------
T = TypeVar("T", bound=BaseModel)


# ===========================================================================
# DatabaseManager
# ===========================================================================
class DatabaseManager:
    """
    Gestor centralizado de la conexión a MongoDB.

    Responsabilidades:
        - Crear y mantener el cliente Motor (pool de conexiones).
        - Exponer health_check para readiness probes.
        - Cerrar la conexión limpiamente en el shutdown.

    Ejemplo de uso en FastAPI lifespan::

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            db_manager = DatabaseManager(settings.MONGO_URL, settings.DB_NAME)
            await db_manager.connect()
            app.state.db = db_manager
            yield
            await db_manager.disconnect()

    Attributes:
        mongo_url: URL de conexión (incluye credenciales si aplica).
        db_name: Nombre de la base de datos objetivo.
    """

    # Parámetros de reintento de conexión
    _MAX_RETRIES: int = 3
    _BASE_BACKOFF_SECONDS: float = 2.0

    def __init__(self, mongo_url: str, db_name: str) -> None:
        """
        Args:
            mongo_url: URI de MongoDB. Ej: ``mongodb://user:pass@host:27017``.
            db_name:   Nombre de la base de datos a usar.
        """
        self.mongo_url = mongo_url
        self.db_name = db_name
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AgnosticDatabase] = None

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------
    async def connect(self) -> AgnosticDatabase:
        """
        Establece la conexión con MongoDB con reintentos exponenciales.

        La lógica de reintento usa un contador local (no estado de instancia)
        para permitir reconexiones correctas después de fallos previos.

        Returns:
            La instancia de base de datos lista para usarse.

        Raises:
            RuntimeError: Si no se pudo conectar tras ``_MAX_RETRIES`` intentos.
        """
        if self._db is not None:
            logger_database.debug("Reutilizando conexión existente a MongoDB.")
            return self._db

        last_error: Optional[Exception] = None

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                logger_database.info(f"Conectando a MongoDB (intento {attempt}/{self._MAX_RETRIES})…")

                self._client = AsyncIOMotorClient(
                    self.mongo_url,
                    maxPoolSize=settings.MAX_POOL_SIZE,
                    minPoolSize=settings.MIN_POOL_SIZE,
                    serverSelectionTimeoutMS=5_000,
                    connectTimeoutMS=10_000,
                    socketTimeoutMS=30_000,
                    waitQueueTimeoutMS=10_000,
                    retryWrites=True,
                    retryReads=True,
                )

                await self._client.admin.command("ping")
                self._db = self._client[self.db_name]
                logger_database.info(f"Conexión exitosa a '{self.db_name}'.")
                return self._db

            except ServerSelectionTimeoutError as exc:
                last_error = exc
                logger_database.warning(f"Timeout en intento {attempt}: {exc}")

                if attempt < self._MAX_RETRIES:
                    wait = self._BASE_BACKOFF_SECONDS ** attempt
                    logger_database.info(f"Reintentando en {wait:.1f}s…")
                    await asyncio.sleep(wait)

            except Exception as exc:
                logger_database.error(f"Error inesperado conectando a MongoDB: {exc}")
                raise

        raise RuntimeError(
            f"No se pudo conectar a MongoDB tras {self._MAX_RETRIES} intentos."
        ) from last_error

    async def disconnect(self) -> None:
        """
        Cierra la conexión de forma segura.

        Es idempotente: puede llamarse varias veces sin efecto secundario.
        """
        if self._client is not None:
            try:
                self._client.close()
                logger_database.info("Desconectado de MongoDB.")
            except Exception as exc:
                logger_database.warning(f"Advertencia al cerrar conexión: {exc}")
            finally:
                self._client = None
                self._db = None

    async def get_database(self) -> AgnosticDatabase:
        """
        Devuelve la base de datos, conectando si aún no se hizo.

        Returns:
            Instancia de ``AgnosticDatabase`` lista para operar.
        """
        if self._db is None:
            await self.connect()
        return self._db  # type: ignore[return-value]

    async def health_check(self) -> bool:
        """
        Verifica si la conexión a MongoDB está activa.

        Útil para endpoints ``/health`` o readiness probes de Kubernetes.

        Returns:
            ``True`` si la base de datos responde; ``False`` en caso contrario.
        """
        try:
            if self._db is not None:
                await self._db.command("ping")
                return True
        except Exception as exc:
            logger_database.error(f"Health check fallido: {exc}")
        return False


# ===========================================================================
# BaseRepository
# ===========================================================================
class BaseRepository(Generic[T]):
    """
    Repositorio genérico y tipado para operaciones CRUD sobre MongoDB.

    Proporciona:
        - ``create`` / ``read`` / ``update`` / ``delete``
        - ``list`` con paginación, filtro y ordenación segura
        - ``count``, ``exists``, ``find``, ``delete_many``
        - Creación automática de índices en ``initialize()``
        - Deserialización de documentos MongoDB → modelos Pydantic

    INICIO OBLIGATORIO::

        repo = ParteRepository(db_manager)
        await repo.initialize()

    Args:
        collection_name: Nombre de la colección MongoDB.
        model_class:     Clase Pydantic que representa el documento.
        db_manager:      Instancia de ``DatabaseManager`` ya conectada (o a conectar).
        sortable_fields: Conjunto de campos permitidos para ``sort_by`` en ``list()``.
                         Evita inyección de campos arbitrarios.
        default_indexes: Especificaciones de índices a crear en ``initialize()``.
                         Formato: ``[{"keys": [...], "name": "...", "unique": bool}]``.
    """

    def __init__(
        self,
        collection_name: str,
        model_class: Type[T],
        db_manager: DatabaseManager,
        sortable_fields: Optional[Set[str]] = None,
        default_indexes: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.collection_name = collection_name
        self.model_class = model_class
        self.db_manager = db_manager
        self.sortable_fields: Set[str] = sortable_fields or {"_id", "created_at"}
        self.default_indexes: List[Dict[str, Any]] = default_indexes or []
        self._collection: Optional[AsyncCollection] = None

    # ------------------------------------------------------------------
    # Inicialización
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """
        Prepara el repositorio para operar.

        Acciones:
            1. Obtiene y cachea la colección MongoDB.
            2. Crea todos los índices declarados en ``default_indexes``.

        Debe llamarse UNA VEZ durante el startup de la aplicación.
        Llamadas repetidas son inofensivas (los índices ya existentes se omiten).
        """
        self._collection = await self._get_collection()
        #await self._create_indexes()
        logger_database.info(f"Repositorio '{self.collection_name}' inicializado.")

    # ------------------------------------------------------------------
    # Gestión de la colección
    # ------------------------------------------------------------------
    async def _get_collection(self) -> AsyncCollection:
        """
        Devuelve la colección, usando caché si ya fue inicializada.

        Returns:
            Instancia de ``AsyncCollection`` lista para operar.
        """
        if self._collection is None:
            db = await self.db_manager.get_database()
            self._collection = db[self.collection_name]
        return self._collection

    # ------------------------------------------------------------------
    # Índices
    # ------------------------------------------------------------------
    async def _create_indexes(self) -> None:
        """
        Crea todos los índices declarados más el índice base de ``created_at``.

        Los índices ya existentes se ignoran silenciosamente.
        Un fallo en la creación de un índice loguea el error pero no interrumpe
        el inicio de la aplicación (salvo que sea un índice único en conflicto
        con datos existentes, que sí lanzará ``OperationFailure``).
        """
        collection = await self._get_collection()

        base_index: Dict[str, Any] = {
            "keys": [("created_at", DESCENDING)],
            "name": f"idx_{self.collection_name}_created_at",
        }
        all_indexes = [base_index] + self.default_indexes

        for spec in all_indexes:
            keys: List[Tuple[str, int]] = spec.get("keys", [])
            name: Optional[str] = spec.get("name")
            unique: bool = spec.get("unique", False)
            sparse: bool = spec.get("sparse", False)
            expire_after_seconds: Optional[int] = spec.get("expireAfterSeconds")

            kwargs: Dict[str, Any] = {
                "name": name,
                "unique": unique,
                "sparse": sparse,
            }
            if expire_after_seconds is not None:
                kwargs["expireAfterSeconds"] = expire_after_seconds

            try:
                await collection.create_index(keys, **kwargs)
                logger_database.info(f"Índice listo: '{name}' en '{self.collection_name}'.")
            except OperationFailure as exc:
                # Índice ya existe con la misma definición → OK
                if "already exists" in str(exc).lower():
                    logger_database.debug(f"Índice '{name}' ya existe, omitiendo.")
                else:
                    logger_database.error(f"Error creando índice '{name}': {exc}")

    # ------------------------------------------------------------------
    # Deserialización
    # ------------------------------------------------------------------
    def _deserialize_document(self, doc: Dict[str, Any]) -> T:
        """
        Convierte un documento crudo de MongoDB en una instancia del modelo Pydantic.

        Mapeo aplicado:
            ``_id`` (ObjectId)  →  ``"id"`` (str)

        El campo ``_id`` original se elimina del dict para evitar colisiones con
        el alias ``identificador`` definido en los schemas.

        Args:
            doc: Documento MongoDB tal como lo devuelve Motor.

        Returns:
            Instancia de ``T`` validada por Pydantic.

        Raises:
            ValidationError: Si el documento no cumple el schema del modelo.
        """
        payload = dict(doc)

        if "_id" in payload:
            payload["id"] = str(payload.pop("_id"))

        return self.model_class(**payload)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def create(self, document: T) -> str:
        """
        Inserta un nuevo documento en la colección.

        El campo ``id`` / ``identificador`` se excluye del payload porque
        MongoDB genera el ``_id`` automáticamente.

        Args:
            document: Instancia del modelo a persistir.

        Returns:
            ID del documento recién creado (string hexadecimal de ObjectId).

        Raises:
            ValueError:   Si viola un índice único.
            Exception:    Cualquier otro error de Motor propagado al caller.
        """
        collection = await self._get_collection()
        try:
            # Excluir campos de ID para que Mongo los genere
            payload = document.model_dump(
                by_alias=True,
                exclude_unset=True,
                exclude={"id", "identificador"},
            )
            result = await collection.insert_one(payload)
            logger_database.info(f"[{self.collection_name}] Documento creado: {result.inserted_id}.")
            return str(result.inserted_id)

        except DuplicateKeyError as exc:
            logger_database.error(f"[{self.collection_name}] Clave duplicada: {exc.details}")
            raise ValueError(f"Ya existe un documento con esa clave única.") from exc

        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en create: {exc}")
            raise

    async def read(self, document_id: str) -> Optional[T]:
        """
        Obtiene un documento por su ``_id``.

        Args:
            document_id: ObjectId en formato string.

        Returns:
            Instancia del modelo o ``None`` si no existe.
        """
        collection = await self._get_collection()
        try:
            doc = await collection.find_one({"_id": ObjectId(document_id)})
            return self._deserialize_document(doc) if doc else None
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en read({document_id}): {exc}")
            return None

    async def read_by_field(self, field: str, value: Any) -> Optional[T]:
        """
        Busca el primer documento que coincida con ``{field: value}``.

        Args:
            field: Nombre del campo MongoDB.
            value: Valor exacto a buscar.

        Returns:
            Instancia del modelo o ``None`` si no existe.
        """
        collection = await self._get_collection()
        try:
            doc = await collection.find_one({field: value})
            return self._deserialize_document(doc) if doc else None
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en read_by_field({field}): {exc}")
            return None

    async def update(
        self,
        document_id: str,
        update_data: Union[T, Dict[str, Any]],
    ) -> bool:
        """
        Actualiza parcialmente un documento con ``$set``.

        Si ``update_data`` es un modelo Pydantic, sólo se persisten los campos
        que fueron explícitamente enviados (``exclude_unset=True``) y que no
        son ``None`` (``exclude_none=True``).

        Args:
            document_id: ObjectId en formato string.
            update_data: Modelo Pydantic o dict con los campos a actualizar.

        Returns:
            ``True`` si se modificó al menos un documento; ``False`` si no se encontró.

        Raises:
            Exception: Cualquier error de Motor propagado al caller.
        """
        collection = await self._get_collection()
        try:
            if isinstance(update_data, BaseModel):
                update_dict = update_data.model_dump(
                    exclude_unset=True,
                    exclude_none=True,
                    by_alias=True,
                )
            else:
                update_dict = dict(update_data)

            update_dict["updated_at"] = datetime.now(timezone.utc)

            result = await collection.update_one(
                {"_id": ObjectId(document_id)},
                {"$set": update_dict},
            )

            updated = result.modified_count > 0
            if updated:
                logger_database.info(f"[{self.collection_name}] Documento actualizado: {document_id}.")
            else:
                logger_database.warning(f"[{self.collection_name}] No encontrado para actualizar: {document_id}.")
            return updated

        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en update({document_id}): {exc}")
            raise

    async def delete(self, document_id: str) -> bool:
        """
        Elimina un documento por su ``_id``.

        Args:
            document_id: ObjectId en formato string.

        Returns:
            ``True`` si se eliminó; ``False`` si no se encontró.

        Raises:
            Exception: Cualquier error de Motor propagado al caller.
        """
        collection = await self._get_collection()
        try:
            result = await collection.delete_one({"_id": ObjectId(document_id)})
            deleted = result.deleted_count > 0
            if deleted:
                logger_database.info(f"[{self.collection_name}] Documento eliminado: {document_id}.")
            else:
                logger_database.warning(f"[{self.collection_name}] No encontrado para eliminar: {document_id}.")
            return deleted
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en delete({document_id}): {exc}")
            raise

    async def delete_many(self, filter_query: Dict[str, Any]) -> int:
        """
        Elimina todos los documentos que coincidan con el filtro.

        Args:
            filter_query: Filtro MongoDB. **No puede ser vacío** para evitar
                          borrados accidentales de toda la colección. Usa
                          ``{}`` sólo si estás absolutamente seguro.

        Returns:
            Número de documentos eliminados.

        Raises:
            Exception: Cualquier error de Motor propagado al caller.
        """
        collection = await self._get_collection()
        try:
            result = await collection.delete_many(filter_query)
            logger_database.info(
                f"[{self.collection_name}] {result.deleted_count} documentos eliminados con filtro {filter_query}."
            )
            return result.deleted_count
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en delete_many: {exc}")
            raise

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        filter_query: Optional[Dict[str, Any]] = None,
        sort_by: str = "_id",
        sort_order: int = DESCENDING,
    ) -> Tuple[List[T], int]:
        """
        Lista documentos con paginación, filtro y ordenación segura.

        El parámetro ``sort_by`` sólo acepta campos declarados en
        ``sortable_fields`` para evitar inyección de campos arbitrarios.

        Args:
            skip:         Documentos a omitir (para paginación).
            limit:        Máximo de documentos a devolver.
            filter_query: Filtro MongoDB. ``None`` devuelve todos.
            sort_by:      Campo de ordenación (debe estar en ``sortable_fields``).
            sort_order:   ``ASCENDING`` o ``DESCENDING``.

        Returns:
            Tupla ``(lista_de_modelos, total_de_coincidencias)``.
            El total refleja el conteo real sin paginación.

        Raises:
            ValueError: Si ``sort_by`` no es un campo permitido.
            Exception:  Cualquier error de Motor propagado al caller.
        """
        if sort_by not in self.sortable_fields:
            raise ValueError(
                f"Campo de ordenación no permitido: '{sort_by}'. "
                f"Permitidos: {self.sortable_fields}"
            )

        collection = await self._get_collection()
        query = filter_query or {}

        try:
            total = await collection.count_documents(query)
            cursor = (
                collection.find(query)
                .sort(sort_by, sort_order)
                .skip(skip)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            models = [self._deserialize_document(doc) for doc in docs]
            logger_database.info(
                f"[{self.collection_name}] list() → {len(models)} docs (total={total})."
            )
            return models, total

        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en list(): {exc}")
            raise

    async def find(
        self,
        filter_query: Dict[str, Any],
        sort: Optional[List[Tuple[str, int]]] = None,
    ) -> List[T]:
        """
        Búsqueda libre sin paginación.

        Útil para queries internas o de agregación donde se necesita
        el resultado completo. Para listas de API, prefiere ``list()``.

        Args:
            filter_query: Filtro MongoDB.
            sort:         Lista de tuplas ``[(campo, orden), …]`` para ordenar.
                          Ej: ``[("created_at", DESCENDING)]``.

        Returns:
            Lista de modelos que coinciden con el filtro.

        Raises:
            Exception: Cualquier error de Motor propagado al caller.
        """
        collection = await self._get_collection()
        try:
            cursor = collection.find(filter_query)
            if sort:
                cursor = cursor.sort(sort)
            docs = await cursor.to_list(length=None)
            return [self._deserialize_document(doc) for doc in docs]
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en find(): {exc}")
            raise

    async def count(self, filter_query: Optional[Dict[str, Any]] = None) -> int:
        """
        Cuenta documentos que coincidan con el filtro.

        Args:
            filter_query: Filtro MongoDB. ``None`` cuenta todos los documentos.

        Returns:
            Número de documentos que coinciden.

        Raises:
            Exception: Cualquier error de Motor propagado al caller.
        """
        collection = await self._get_collection()
        try:
            return await collection.count_documents(filter_query or {})
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en count(): {exc}")
            raise

    async def exists(self, document_id: str) -> bool:
        """
        Verifica si un documento existe por su ``_id``.

        Usa proyección ``{"_id": 1}`` para minimizar transferencia de datos.

        Args:
            document_id: ObjectId en formato string.

        Returns:
            ``True`` si existe; ``False`` en caso contrario.
        """
        collection = await self._get_collection()
        try:
            doc = await collection.find_one(
                {"_id": ObjectId(document_id)},
                {"_id": 1},
            )
            return doc is not None
        except Exception as exc:
            logger_database.error(f"[{self.collection_name}] Error en exists({document_id}): {exc}")
            return False