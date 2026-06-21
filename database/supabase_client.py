"""
Cliente Supabase para keywords y URLs.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any

from supabase import Client, create_client

from config.settings import settings
from database.models import KeywordClaimResult

logger = logging.getLogger(__name__)


class SupabaseKeywordRepo:
    """Repositorio de keywords en Supabase."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def claim_keywords(
        self,
        label: str | None = None,
        limit: int = 10,
    ) -> list[KeywordClaimResult]:
        """
        Reclama keywords para scraping con bloqueo atómico.
        """
        try:
            query = (
                self._client.table("keyword")
                .select("id, keyword, platform, engine, label, scraped_at")
                .eq("scraping", False)
                .order("scraped_at", desc=False, nullsfirst=True)
                .limit(limit)
            )

            if label is not None:
                query = query.eq("label", label)

            select_response = query.execute()
            rows = select_response.data or []

            if not rows:
                label_msg = f"label='{label}'" if label else "todos los labels"
                logger.debug("No hay keywords disponibles para %s", label_msg)
                return []

            keyword_ids = [row["id"] for row in rows]
            
            update_response = (
                self._client.table("keyword")
                .update({"scraping": True})
                .in_("id", keyword_ids)
                .execute()
            )

            if not update_response.data:
                 logger.warning("No se pudieron bloquear las keywords seleccionadas.")
                 return []

            logger.info(
                "Reclamadas %d keywords para %s",
                len(keyword_ids),
                f"label='{label}'" if label else "todos los labels",
            )

            return [
                KeywordClaimResult(
                    id=row["id"],
                    keyword=row["keyword"],
                    platform=row["platform"],
                    engine=row.get("engine", ""),
                    label=row.get("label", label or ""),
                )
                for row in rows
                if row.get("keyword")
            ]

        except Exception as exc:
            logger.error("Error reclamando keywords: %s", exc)
            return []

    async def get_distinct_labels(self) -> list[str]:
        """Obtiene todos los labels distintos."""
        try:
            response = (
                self._client.table("keyword")
                .select("label")
                .execute()
            )
            rows = response.data or []
            labels = list({row["label"] for row in rows if row.get("label")})
            labels.sort()
            return labels
        except Exception as exc:
            logger.error("Error obteniendo labels: %s", exc)
            return []

    async def mark_scraped(self, keyword_id: int) -> bool:
        """Marca una keyword como scrapeada."""
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            response = (
                self._client.table("keyword")
                .update({"scraped_at": now_iso, "scraping": False})
                .eq("id", keyword_id)
                .execute()
            )
            if response.data:
                return True
            return False
        except Exception as exc:
            logger.error("Error marcando keyword id=%d: %s", keyword_id, exc)
            return False

    async def release_keywords(self, keyword_ids: list[int]) -> None:
        """Libera keywords no completadas."""
        if not keyword_ids:
            return
        try:
            self._client.table("keyword").update({
                "scraping": False,
            }).in_("id", keyword_ids).execute()
            logger.debug("Liberadas %d keywords.", len(keyword_ids))
        except Exception as exc:
            logger.error("Error liberando keywords: %s", exc)


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
        """Inserta una URL en Supabase."""
        try:
            response = (
                self._client.table("url")
                .upsert({
                    "url": url,
                    "keyword": keyword,
                    "send_tg": send_tg,
                }, on_conflict="url")
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
        self._client: Client | None = None
        self.keyword_repo: SupabaseKeywordRepo | None = None
        self.url_repo: SupabaseUrlRepo | None = None

    async def connect(self) -> None:
        """Inicializa la conexión a Supabase."""
        try:
            self._client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
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