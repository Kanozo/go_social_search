"""
database/google_result_db.py
Repositorio especializado para la colección `google_results`.
Implementa inserción masiva que omite silenciosamente duplicados (url, keyword).
"""
from __future__ import annotations

from typing import List, Tuple
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError
from database.core_db import BaseRepository, DatabaseManager
from log_config import logger_database
from schemas.google_result_schema import GoogleResultInDB
from config.settings import settings
from schemas.base_schema import PyObjectId
from datetime import datetime, timezone
from bson import ObjectId

class GoogleResultRepository(BaseRepository[GoogleResultInDB]):
    """
    Repositorio de la colección `google_results`.
    
    ÍNDICES DECLARADOS:
        - ``(url, keyword)`` único  → Garantiza idempotencia estricta.
        - ``keyword``               → Filtrado rápido por término.
        - ``published_at``          → Ordenación cronológica.
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        collection_name = getattr(settings, "GOOGLE_RESULTS_COLLECTION", "google_results")
        super().__init__(
            collection_name=collection_name,
            model_class=GoogleResultInDB,
            db_manager=db_manager,
            sortable_fields={"_id", "created_at", "updated_at", "published_at", "keyword", "url"},
            default_indexes=[
                {
                    "keys": [("url", ASCENDING), ("keyword", ASCENDING)],
                    "name": "idx_google_result_url_keyword_unique",
                    "unique": True,
                },
                {
                    "keys": [("keyword", ASCENDING)],
                    "name": "idx_google_result_keyword",
                },
                {
                    "keys": [("published_at", DESCENDING)],
                    "name": "idx_google_result_published_at",
                },
                {
                    "keys": [("inserted_at", DESCENDING)],
                    "name": "idx_google_result_inserted_at",
                },
            ],
        )

    async def bulk_insert_skip_existing(
        self,
        results: List[GoogleResultInDB],
    ) -> Tuple[int, int]:
        """
        Inserta únicamente resultados nuevos. Si (url, keyword) ya existe,
        se ignora silenciosamente SIN actualizar ni modificar el documento.
        
        Usa ``insert_many`` con ``ordered=False``. MongoDB intenta insertar
        todos; los duplicados lanzan error 11000 que capturamos y contamos
        como "omitidos". Los válidos se insertan en una sola ronda de red.

        Args:
            results: Lista de instancias ``GoogleResultInDB``.

        Returns:
            Tupla ``(insertados, omitidos)``.
        """
        collection = await self._get_collection()
        if not results:
            return 0, 0

        # Preparar payloads limpios (sin _id ni id interno)
        docs = [
            r.model_dump(by_alias=True, exclude_unset=True, exclude={"id", "_id"})
            for r in results
        ]

        try:
            result = await collection.insert_many(docs, ordered=False)
            inserted = len(result.inserted_ids)
            logger_database.info(f"Bulk insert completado: {inserted} nuevos documentos insertados.")
            return inserted, 0
            
        except BulkWriteError as bwe:
            inserted = len(bwe.details.get("insertedIds", {}))
            # Contar explícitamente duplicados (código 11000)
            skipped = sum(1 for e in bwe.details.get("writeErrors", []) if e.get("code") == 11000)
            # Loguear otros errores si los hubiera (ej: validación, tipo)
            other_errors = [e for e in bwe.details.get("writeErrors", []) if e.get("code") != 11000]
            if other_errors:
                logger_database.warning(f"{len(other_errors)} errores no relacionados con duplicados en bulk insert.")
                
            logger_database.info(f"Bulk insert: {inserted} nuevos, {skipped} omitidos (ya existentes).")
            return inserted, skipped
        
    async def get_unprocessed(self, limit: int = 1000) -> List[GoogleResultInDB]:
        """
        Recupera documentos que no han sido procesados.
        Se usa $ne: True para capturar tanto False como campos inexistentes.
        """
        collection = await self._get_collection()
        cursor = collection.find({"processed": {"$ne": True}}).limit(limit)
        
        # Acceso directo al schema para evitar el AttributeError de la clase base
        return [GoogleResultInDB(**doc) for doc in await cursor.to_list(length=limit)]    

    async def mark_as_processed(self, doc_id: PyObjectId) -> bool:
        """Actualiza el estado de procesamiento a True."""
        collection = await self._get_collection()
        result = await collection.update_one(
            {"_id": ObjectId(doc_id)},
            {"$set": {"processed": True, "updated_at": datetime.now(timezone.utc)}}
        )
        return result.modified_count > 0