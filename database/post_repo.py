"""
database/post_repo.py
Repositorio CRUD para la tabla ``posts``.

Política de unicidad
────────────────────
La columna ``url`` tiene UNIQUE constraint. ``bulk_insert_new`` usa
``INSERT OR IGNORE`` para descartar silenciosamente URLs duplicadas.
Nunca se lanza excepción por duplicado: el repositorio devuelve
los conteos de insertados/omitidos para que el caller pueda loggear.

Ciclo de vida de los campos timestamp
──────────────────────────────────────
  scrapt_at → fijado en el INSERT (momento de scraping).
  was_sent  → fijado al llamar mark_queued_for_send(url).
  sent_at   → fijado al llamar mark_sent(url) tras confirmación del envío.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

from database.db_manager import SQLiteManager
from database.models import Post, PostCreate

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de conversión datetime ↔ ISO string
# ─────────────────────────────────────────────────────────────────────────────

def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _from_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _row_to_post(row: aiosqlite.Row) -> Post:
    """Mapea una fila SQLite al dataclass Post."""
    return Post(
        id=row["id"],
        url=row["url"],
        keyword=row["keyword"],
        platform=row["platform"],
        scrapt_at=_from_iso(row["scrapt_at"]),
        was_sent=_from_iso(row["was_sent"]),
        sent_at=_from_iso(row["sent_at"]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# PostRepository
# ─────────────────────────────────────────────────────────────────────────────

class PostRepository:
    """
    Acceso a datos para la tabla ``posts``.

    Proporciona operaciones de inserción en bulk (skip duplicates),
    consultas de posts pendientes de envío y actualización del ciclo
    de vida de cada post (scraped → queued → sent).

    Args:
        manager: Instancia de ``SQLiteManager`` ya conectada.

    Example::

        repo = PostRepository(manager)
        inserted, skipped = await repo.bulk_insert_new([
            PostCreate(url="https://fb.com/123", keyword="#Cuba", platform="facebook"),
        ])
        await repo.mark_queued_for_send("https://fb.com/123")
        await repo.mark_sent("https://fb.com/123")
    """

    def __init__(self, manager: SQLiteManager) -> None:
        self._db = manager

    # ── Escritura ─────────────────────────────────────────────────────────────

    async def bulk_insert_new(
        self, posts: list[PostCreate]
    ) -> tuple[int, int]:
        """
        Inserta posts nuevos ignorando duplicados (por ``url``).

        ``scrapt_at`` se fija al momento UTC actual en el INSERT.
        Si la URL ya existe, la fila no se toca (INSERT OR IGNORE).

        Args:
            posts: Lista de ``PostCreate`` a insertar.

        Returns:
            Tupla ``(insertados, omitidos)`` donde
            ``omitidos = len(posts) - insertados``.
        """
        if not posts:
            return 0, 0

        now_iso = _to_iso(datetime.now(timezone.utc))
        inserted = 0

        async with self._db.write() as conn:
            for post in posts:
                cursor = await conn.execute(
                    """
                    INSERT OR IGNORE INTO posts
                        (url, keyword, platform, scrapt_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (post.url, post.keyword, post.platform, now_iso),
                )
                inserted += cursor.rowcount  # 1 si insertó, 0 si ignoró

        skipped = len(posts) - inserted
        logger.info(
            "bulk_insert_new: %d insertados, %d omitidos (duplicados).",
            inserted, skipped,
        )
        return inserted, skipped

    async def insert_one(self, post: PostCreate) -> Post | None:
        """
        Inserta un único post. Devuelve None si la URL ya existía.

        Args:
            post: Datos del post a insertar.

        Returns:
            El ``Post`` insertado con su id y scrapt_at, o None si era duplicado.
        """
        now_iso = _to_iso(datetime.now(timezone.utc))
        async with self._db.write() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO posts (url, keyword, platform, scrapt_at) VALUES (?, ?, ?, ?)",
                (post.url, post.keyword, post.platform, now_iso),
            )
            if cursor.rowcount == 0:
                return None
            row_id = cursor.lastrowid

        return await self.get_by_id(row_id)

    async def mark_queued_for_send(self, url: str) -> bool:
        """
        Fija ``was_sent`` al momento UTC actual, indicando que el post entró
        en la cola de envío.

        Args:
            url: URL del post a marcar.

        Returns:
            True si se actualizó alguna fila.
        """
        async with self._db.write() as conn:
            cursor = await conn.execute(
                "UPDATE posts SET was_sent = ? WHERE url = ? AND was_sent IS NULL",
                (_to_iso(datetime.now(timezone.utc)), url),
            )
            return cursor.rowcount > 0

    async def mark_sent(self, url: str) -> bool:
        """
        Fija ``sent_at`` al momento UTC actual, confirmando el envío exitoso.

        Args:
            url: URL del post enviado.

        Returns:
            True si se actualizó alguna fila.
        """
        async with self._db.write() as conn:
            cursor = await conn.execute(
                "UPDATE posts SET sent_at = ? WHERE url = ? AND sent_at IS NULL",
                (_to_iso(datetime.now(timezone.utc)), url),
            )
            return cursor.rowcount > 0

    async def mark_many_sent(self, urls: list[str]) -> int:
        """
        Fija ``sent_at`` en batch para múltiples URLs en una sola transacción.

        Args:
            urls: Lista de URLs cuyo envío fue confirmado.

        Returns:
            Número de filas actualizadas.
        """
        if not urls:
            return 0
        now_iso = _to_iso(datetime.now(timezone.utc))
        updated = 0
        async with self._db.write() as conn:
            for url in urls:
                cursor = await conn.execute(
                    "UPDATE posts SET sent_at = ? WHERE url = ? AND sent_at IS NULL",
                    (now_iso, url),
                )
                updated += cursor.rowcount
        return updated

    # ── Lectura ───────────────────────────────────────────────────────────────

    async def get_by_id(self, post_id: int) -> Post | None:
        """
        Busca un post por su clave primaria.

        Args:
            post_id: Valor del campo ``id``.

        Returns:
            ``Post`` o None.
        """
        row = await self._db.execute_fetchone(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        )
        return _row_to_post(row) if row else None

    async def get_by_url(self, url: str) -> Post | None:
        """
        Busca un post por su URL exacta.

        Args:
            url: URL canónica del post.

        Returns:
            ``Post`` o None si no existe.
        """
        row = await self._db.execute_fetchone(
            "SELECT * FROM posts WHERE url = ?", (url,)
        )
        return _row_to_post(row) if row else None

    async def url_exists(self, url: str) -> bool:
        """
        Verifica si una URL ya está en la tabla sin cargar la fila completa.

        Args:
            url: URL a verificar.

        Returns:
            True si existe.
        """
        row = await self._db.execute_fetchone(
            "SELECT 1 FROM posts WHERE url = ? LIMIT 1", (url,)
        )
        return row is not None

    async def get_pending_send(
        self,
        limit: int = 500,
        platform: str | None = None,
    ) -> list[Post]:
        """
        Devuelve posts scrapeados que aún no han sido enviados (``sent_at IS NULL``).

        Útil para un worker de envío independiente del scraper.

        Args:
            limit:    Máximo de filas a devolver.
            platform: Si se provee, filtra por plataforma.

        Returns:
            Lista de ``Post`` ordenada por ``scrapt_at`` ascendente (FIFO).
        """
        platform_clause = "AND platform = ?" if platform else ""
        params: tuple = (platform, limit) if platform else (limit,)
        rows = await self._db.execute_fetchall(
            f"""
            SELECT * FROM posts
            WHERE  sent_at IS NULL
            {platform_clause}
            ORDER  BY scrapt_at ASC
            LIMIT  ?
            """,
            params,
        )
        return [_row_to_post(r) for r in rows]

    async def get_by_keyword(
        self,
        keyword: str,
        limit: int = 200,
    ) -> list[Post]:
        """
        Devuelve los posts asociados a un keyword específico.

        Args:
            keyword: Término de búsqueda.
            limit:   Máximo de filas a devolver.

        Returns:
            Lista de ``Post`` ordenada por ``scrapt_at`` descendente (más recientes).
        """
        rows = await self._db.execute_fetchall(
            "SELECT * FROM posts WHERE keyword = ? ORDER BY scrapt_at DESC LIMIT ?",
            (keyword, limit),
        )
        return [_row_to_post(r) for r in rows]

    async def get_recent(
        self,
        hours: int = 24,
        platform: str | None = None,
    ) -> list[Post]:
        """
        Devuelve posts scrapeados en las últimas ``hours`` horas.

        Args:
            hours:    Ventana de tiempo hacia atrás desde ahora.
            platform: Si se provee, filtra por plataforma.

        Returns:
            Lista de ``Post`` ordenada por ``scrapt_at`` descendente.
        """
        platform_clause = "AND platform = ?" if platform else ""
        params: tuple = (hours, platform) if platform else (hours,)
        rows = await self._db.execute_fetchall(
            f"""
            SELECT * FROM posts
            WHERE  scrapt_at >= datetime('now', ? || ' hours')
            {platform_clause}
            ORDER  BY scrapt_at DESC
            """,
            # SQLite espera el modificador como "-24 hours"
            (f"-{hours}", platform) if platform else (f"-{hours}",),
        )
        return [_row_to_post(r) for r in rows]

    async def count(
        self,
        only_unsent: bool = False,
        platform: str | None = None,
    ) -> int:
        """
        Número de posts en la tabla, con filtros opcionales.

        Args:
            only_unsent: Si True, cuenta solo los no enviados (``sent_at IS NULL``).
            platform:    Si se provee, filtra por plataforma.

        Returns:
            Número de filas como entero.
        """
        conditions: list[str] = []
        params: list = []

        if only_unsent:
            conditions.append("sent_at IS NULL")
        if platform:
            conditions.append("platform = ?")
            params.append(platform)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = await self._db.execute_fetchone(
            f"SELECT COUNT(*) AS n FROM posts {where}", tuple(params)
        )
        return row["n"] if row else 0