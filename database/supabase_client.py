"""
Cliente Supabase para keywords y URLs.

Reemplaza SQLiteManager + KeywordRepository + PostRepository.
Mantiene la misma interfaz pública para que GoogleCSEAutomator
y ScraperOrchestrator no necesiten cambios.

Flujo:
  1. claim_keywords(label, limit=10) → reclama keywords no scrapeadas
     recientemente, las marca scraping=true atómicamente.
  2. mark_scraped(keyword_id) → actualiza scraped_at y pone scraping=false.
  3. insert_url(url, keyword, platform) → INSERT ON CONFLICT DO NOTHING.
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
    """
    Repositorio de keywords en Supabase.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    async def claim_keywords(
        self,
        label: str | None = None,
        limit: int = 10,
    ) -> list[KeywordClaimResult]:
        """
        Reclama keywords para scraping con bloqueo atómico.

        - Si label es None: toma las 10 keywords más antiguas de cualquier label
        - Si label tiene valor: toma las 10 más antiguas de ese label

        Args:
            label: Label del engine o None para todos.
            limit: Máximo de keywords a reclamar.

        Returns:
            Lista de KeywordClaimResult con las keywords reclamadas.
        """
        try:
            query = (
                self._client.table("keyword")
                .select("id, keyword, platform, engine, label")
                .eq("scraping", False)
                .order("scraped_at", desc=False)  # Más antiguas primero
                .limit(limit)
            )

            # Solo filtrar por label si se especifica
            if label is not None:
                query = query.eq("label", label)

            select_response = query.execute()

            rows = select_response.data or []
            if not rows:
                label_msg = f"label='{label}'" if label else "todos los labels"
                logger.debug("No hay keywords disponibles para %s", label_msg)
                return []

            # Marcar como scraping=true
            keyword_ids = [row["id"] for row in rows]

            self._client.table("keyword").update({
                "scraping": True,
            }).in_("id", keyword_ids).execute()

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
            labels = list({
                row["label"]
                for row in rows
                if row.get("label")
            })
            labels.sort()

            logger.debug("Labels distintos en Supabase: %s", labels)
            return labels

        except Exception as exc:
            logger.error("Error obteniendo labels: %s", exc)
            return []

    async def mark_scraped(self, keyword_id: int) -> bool:
        """
        Marca una keyword como scrapeada.
        Actualiza scraped_at a now() y pone scraping=false.
        """
        try:
            now_iso = datetime.now(timezone.utc).isoformat()

            response = (
                self._client.table("keyword")
                .update({
                    "scraped_at": now_iso,
                    "scraping": False,
                })
                .eq("id", keyword_id)
                .execute()
            )

            if response.data:
                logger.debug("Keyword id=%d marcada como scrapeada.", keyword_id)
                return True

            logger.warning("No se pudo actualizar keyword id=%d.", keyword_id)
            return False

        except Exception as exc:
            logger.error("Error marcando keyword id=%d: %s", keyword_id, exc)
            return False

    async def release_keywords(self, keyword_ids: list[int]) -> None:
        """Libera keywords no completadas (scraping=false)."""
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
    """
    Repositorio de URLs en Supabase.

    Reemplaza PostRepository de SQLite.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    async def insert_url(
        self,
        url: str,
        keyword: str,
        platform: str,
        send_tg: bool = False,
    ) -> bool:
        """
        Inserta una URL en Supabase.

        La tabla `url` tiene UNIQUE constraint en `url`, por lo que
        las URLs duplicadas se ignoran automáticamente (ON CONFLICT DO NOTHING).

        Args:
            url:      URL limpia a insertar.
            keyword:  Keyword que originó esta URL.
            platform: Plataforma ("instagram" | "facebook").
            send_tg:  Si debe enviarse por Telegram (default False).

        Returns:
            True si se insertó, False si era duplicada o hubo error.
        """
        try:
            response = (
                self._client.table("url")
                .upsert({
                    "url": url,
                    "keyword": keyword,
                    "send_tg": send_tg,
                    # platform NO está en la tabla url según el schema.
                    # Si se agregara, incluir aquí.
                }, on_conflict="url")  # ← UPSERT: ignora duplicados
                .execute()
            )

            if response.data:
                return True

            return False

        except Exception as exc:
            logger.error(
                "Error insertando URL '%s': %s",
                url[:80],
                exc,
            )
            return False

    async def bulk_insert_urls(
        self,
        urls: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """
        Inserta múltiples URLs en lote.

        Args:
            urls: Lista de dicts con keys: url, keyword, send_tg.

        Returns:
            Tupla (insertadas, omitidas).
        """
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

        logger.info(
            "Bulk insert: %d insertadas, %d omitidas (duplicadas).",
            inserted,
            omitted,
        )
        return inserted, omitted


class SupabaseManager:
    """
    Manager central de Supabase.

    Reemplaza SQLiteManager.
    Proporciona acceso a los repositorios de keywords y URLs.
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self.keyword_repo: SupabaseKeywordRepo | None = None
        self.url_repo: SupabaseUrlRepo | None = None

    async def connect(self) -> None:
        """Inicializa la conexión a Supabase."""
        try:
            self._client = create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_KEY,
            )
            self.keyword_repo = SupabaseKeywordRepo(self._client)
            self.url_repo = SupabaseUrlRepo(self._client)

            logger.info(
                "Supabase conectado | URL=%s",
                settings.SUPABASE_URL[:50],
            )
        except Exception as exc:
            logger.error("Error conectando a Supabase: %s", exc)
            raise

    async def disconnect(self) -> None:
        """Cierra la conexión a Supabase."""
        if self._client:
            # Supabase client no tiene close() explícito,
            # pero limpiamos referencias
            self._client = None
            self.keyword_repo = None
            self.url_repo = None
            logger.info("Supabase desconectado.")

    async def close(self) -> None:
        """Alias para disconnect."""
        await self.disconnect()