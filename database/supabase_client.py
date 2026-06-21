"""
Cliente Supabase para keywords y URLs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client, create_client

from config.settings import settings
from database.models import KeywordBatch, KeywordRecord

logger = logging.getLogger(__name__)


class SupabaseKeywordRepo:
    """Repositorio de keywords en Supabase usando job queue nativo."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def claim_keywords_batch(
        self,
        limit: int = 10,
    ) -> KeywordBatch:
        """
        Reclama un lote atómico de keywords usando la función RPC.
        Sin filtro de plataforma: todas las keywords son multi-plataforma.
        
        Args:
            limit: Cantidad de keywords a reclamar.
            
        Returns:
            KeywordBatch con las keywords reclamadas.
        """
        try:
            response = self._client.rpc(
                "claim_keywords_batch",
                {"p_limit": limit},
            ).execute()

            rows = response.data or []
            
            if not rows:
                logger.debug("No hay keywords disponibles")
                return KeywordBatch(keywords=[])

            keywords = [
                KeywordRecord(
                    id=row["id"],
                    term=row["term"],
                )
                for row in rows
            ]

            logger.info("Reclamadas %d keywords", len(keywords))
            return KeywordBatch(keywords=keywords)

        except Exception as exc:
            logger.error("Error reclamando keywords batch: %s", exc)
            return KeywordBatch(keywords=[])

    async def mark_scraped(self, keyword_id: int) -> bool:
        """
        Marca una keyword como scrapeada actualizando scraped_at.
        """
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            response = (
                self._client.table("keywords")
                .update({"scraped_at": now_iso})
                .eq("id", keyword_id)
                .execute()
            )
            return bool(response.data)
        except Exception as exc:
            logger.error("Error marcando keyword id=%d: %s", keyword_id, exc)
            return False


class SupabaseUrlRepo:
    """Repositorio de URLs en Supabase."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def insert_url(
        self,
        url: str,
        keyword: str,
        platform: str,
        send_tg: bool = False,
    ) -> bool:
        """Inserta una URL en Supabase. La plataforma viene del motor usado."""
        try:
            response = (
                self._client.table("url")
                .upsert(
                    {
                        "url": url,
                        "keyword": keyword,
                        "platform": platform,
                        "send_tg": send_tg,
                    },
                    on_conflict="url",
                )
                .execute()
            )
            return bool(response.data)
        except Exception as exc:
            logger.error("Error insertando URL '%s': %s", url[:80], exc)
            return False

    async def bulk_insert_urls(
        self,
        urls: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Inserta múltiples URLs en lote."""
        if not urls:
            return 0, 0

        inserted = 0
        omitted = 0

        for url_data in urls:
            success = await self.insert_url(
                url=url_data["url"],
                keyword=url_data.get("keyword", ""),
                platform=url_data.get("platform", ""),
                send_tg=url_data.get("send_tg", False),
            )
            if success:
                inserted += 1
            else:
                omitted += 1

        return inserted, omitted


class SupabaseManager:
    """Manager central de Supabase."""

    def __init__(self) -> None:
        self._client: Optional[Client] = None
        self.keyword_repo: Optional[SupabaseKeywordRepo] = None
        self.url_repo: Optional[SupabaseUrlRepo] = None

    async def connect(self) -> None:
        """Inicializa la conexión a Supabase."""
        try:
            self._client = create_client(
                settings.SUPABASE_URL, 
                settings.SUPABASE_KEY,
            )
            self.keyword_repo = SupabaseKeywordRepo(self._client)
            self.url_repo = SupabaseUrlRepo(self._client)
            logger.info("Supabase conectado | URL=%s", settings.SUPABASE_URL[:50])
        except Exception as exc:
            logger.error("Error conectando a Supabase: %s", exc)
            raise

    async def disconnect(self) -> None:
        """Cierra la conexión a Supabase."""
        if self._client:
            self._client = None
            self.keyword_repo = None
            self.url_repo = None
            logger.info("Supabase desconectado.")

    async def close(self) -> None:
        await self.disconnect()