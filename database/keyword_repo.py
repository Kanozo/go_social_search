"""
database/keyword_repo.py
Repositorio CRUD para la tabla ``keywords``.

Todas las operaciones de lectura usan ``manager.read()`` (sin lock).
Todas las escrituras usan ``manager.write()`` (con lock serializado).

Convención de fechas
────────────────────
SQLite almacena fechas como TEXT en formato ISO-8601 UTC:
``"2024-05-27T14:30:00.000000+00:00"``

Las funciones ``_to_iso`` y ``_from_iso`` convierten entre ``datetime``
(siempre timezone-aware UTC) y ese string. Nunca se almacena un datetime
naive en la DB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

from database.db_manager import SQLiteManager
from database.models import Classification, Keyword, KeywordCreate

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de conversión datetime ↔ ISO string
# ─────────────────────────────────────────────────────────────────────────────

def _to_iso(dt: datetime | None) -> str | None:
    """Convierte datetime UTC → string ISO-8601. None → None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _from_iso(raw: str | None) -> datetime | None:
    """Convierte string ISO-8601 → datetime UTC. None → None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _row_to_keyword(row: aiosqlite.Row) -> Keyword:
    """Mapea una fila SQLite al dataclass Keyword."""
    return Keyword(
        id=row["id"],
        keyword=row["keyword"],
        classification=row["classification"],
        last_scrap=_from_iso(row["last_scrap"]),
        label=row["label"],
        platform=row["platform"],
        engine_id=row["engine_id"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# KeywordRepository
# ─────────────────────────────────────────────────────────────────────────────

class KeywordRepository:
    """
    Acceso a datos para la tabla ``keywords``.

    Encapsula todas las queries relacionadas con keywords: inserción en bulk,
    lectura agrupada por engine para el orquestador, y actualización de
    ``last_scrap`` después de cada ciclo de scraping.

    Args:
        manager: Instancia de ``SQLiteManager`` ya conectada.

    Example::

        repo = KeywordRepository(manager)
        # Seed inicial
        await repo.bulk_upsert([
            KeywordCreate(keyword="#Cuba", label="cluster", platform="instagram", engine_id="abc123"),
        ])
        # El orquestador lee la config de engines
        engine_groups = await repo.get_engine_groups()
        # Después de procesar una keyword
        await repo.mark_scraped("#Cuba")
    """

    def __init__(self, manager: SQLiteManager) -> None:
        self._db = manager

    # ── Escritura ─────────────────────────────────────────────────────────────

    async def bulk_upsert(self, keywords: list[KeywordCreate]) -> tuple[int, int]:
        """
        Inserta keywords nuevas y actualiza clasificación/label/platform/engine_id
        de las existentes (por el campo UNIQUE ``keyword``).

        No actualiza ``last_scrap`` en el upsert: ese campo solo lo toca
        ``mark_scraped()``.

        Args:
            keywords: Lista de ``KeywordCreate`` a insertar o actualizar.

        Returns:
            Tupla ``(insertados, actualizados)``.

        Note:
            Usa ``INSERT OR REPLACE`` que internamente borra+reinserta la fila,
            por lo que el ``id`` cambia para las filas actualizadas. Si necesitas
            estabilidad de id, usa ``INSERT OR IGNORE`` + UPDATE separado.
        """
        if not keywords:
            return 0, 0

        inserted = updated = 0
        async with self._db.write() as conn:
            for kw in keywords:
                # Verificar si ya existe para contabilizar correctamente
                cur = await conn.execute(
                    "SELECT id FROM keywords WHERE keyword = ?", (kw.keyword,)
                )
                exists = await cur.fetchone()

                await conn.execute(
                    """
                    INSERT INTO keywords (keyword, classification, label, platform, engine_id)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(keyword) DO UPDATE SET
                        classification = excluded.classification,
                        label          = excluded.label,
                        platform       = excluded.platform,
                        engine_id      = excluded.engine_id
                    """,
                    (
                        kw.keyword,
                        kw.classification,
                        kw.label,
                        kw.platform,
                        kw.engine_id,
                    ),
                )
                if exists:
                    updated += 1
                else:
                    inserted += 1

        logger.info(
            "bulk_upsert keywords: %d insertados, %d actualizados.", inserted, updated
        )
        return inserted, updated

    async def insert_one(self, kw: KeywordCreate) -> Keyword | None:
        """
        Inserta un único keyword. Si ya existe, no hace nada (INSERT OR IGNORE).

        Args:
            kw: Datos del keyword a insertar.

        Returns:
            El ``Keyword`` insertado con su id, o None si ya existía.
        """
        async with self._db.write() as conn:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO keywords
                    (keyword, classification, label, platform, engine_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kw.keyword, kw.classification, kw.label, kw.platform, kw.engine_id),
            )
            if cursor.rowcount == 0:
                return None  # Ya existía
            row_id = cursor.lastrowid

        return await self.get_by_id(row_id)

    async def mark_scraped(self, keyword: str) -> bool:
        """
        Actualiza ``last_scrap`` al momento UTC actual para el keyword dado.

        Se llama en ``run_scraper.py`` después de procesar cada keyword.

        Args:
            keyword: Término de búsqueda exacto (case-sensitive, como está en DB).

        Returns:
            True si se actualizó alguna fila, False si el keyword no existe.
        """
        now_iso = _to_iso(datetime.now(timezone.utc))
        async with self._db.write() as conn:
            cursor = await conn.execute(
                "UPDATE keywords SET last_scrap = ? WHERE keyword = ?",
                (now_iso, keyword),
            )
            updated = cursor.rowcount > 0

        if updated:
            logger.debug("last_scrap actualizado para keyword='%s'.", keyword)
        else:
            logger.warning("mark_scraped: keyword='%s' no encontrado.", keyword)
        return updated

    async def update_classification(
        self, keyword: str, classification: Classification
    ) -> bool:
        """
        Cambia la clasificación de un keyword existente.

        Args:
            keyword:        Término de búsqueda.
            classification: "positivo" | "negativo" | "neutro"

        Returns:
            True si la fila fue actualizada.
        """
        async with self._db.write() as conn:
            cursor = await conn.execute(
                "UPDATE keywords SET classification = ? WHERE keyword = ?",
                (classification, keyword),
            )
            return cursor.rowcount > 0

    async def delete(self, keyword: str) -> bool:
        """
        Elimina un keyword de la tabla.

        Args:
            keyword: Término a eliminar.

        Returns:
            True si se eliminó alguna fila.
        """
        async with self._db.write() as conn:
            cursor = await conn.execute(
                "DELETE FROM keywords WHERE keyword = ?", (keyword,)
            )
            return cursor.rowcount > 0

    # ── Lectura ───────────────────────────────────────────────────────────────

    async def get_by_id(self, keyword_id: int) -> Keyword | None:
        """
        Busca un keyword por su clave primaria.

        Args:
            keyword_id: Valor del campo ``id``.

        Returns:
            ``Keyword`` o None si no existe.
        """
        row = await self._db.execute_fetchone(
            "SELECT * FROM keywords WHERE id = ?", (keyword_id,)
        )
        return _row_to_keyword(row) if row else None

    async def get_by_keyword(self, keyword: str) -> Keyword | None:
        """
        Busca un keyword por su valor exacto.

        Args:
            keyword: Término de búsqueda.

        Returns:
            ``Keyword`` o None si no existe.
        """
        row = await self._db.execute_fetchone(
            "SELECT * FROM keywords WHERE keyword = ?", (keyword,)
        )
        return _row_to_keyword(row) if row else None

    async def get_all(
        self,
        platform: str | None = None,
        classification: Classification | None = None,
    ) -> list[Keyword]:
        """
        Devuelve todos los keywords, con filtros opcionales.

        Args:
            platform:       Si se provee, filtra por plataforma.
            classification: Si se provee, filtra por clasificación.

        Returns:
            Lista de ``Keyword``, ordenada por label y keyword.
        """
        conditions: list[str] = []
        params: list[str] = []

        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if classification:
            conditions.append("classification = ?")
            params.append(classification)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = await self._db.execute_fetchall(
            f"SELECT * FROM keywords {where} ORDER BY label, keyword",
            tuple(params),
        )
        return [_row_to_keyword(r) for r in rows]

    async def get_engine_groups(self) -> list[dict]:
        """
        Agrupa los keywords por ``(engine_id, label)`` para construir la
        configuración de engines que consume ``_fetch_engines_config``.

        Formato de retorno compatible con el orquestador::

            [
                {
                    "engine_id": "c4b97eed1414fcb14",
                    "label":     "IG-KW-Engine",
                    "platform":  "instagram",
                    "keywords":  ["#Cuba", "#LaPatriaSeDefiende", ...],
                },
                ...
            ]

        Solo incluye grupos con al menos un keyword. Los keywords dentro de
        cada grupo se ordenan alfabéticamente.

        Returns:
            Lista de dicts listos para iterar en el orquestador.
        """
        rows = await self._db.execute_fetchall(
            """
            SELECT engine_id, label, platform, keyword
            FROM   keywords
            WHERE  engine_id != ''
            ORDER  BY last_scrap
            """
        )

        # Agrupar en memoria: O(n) con un dict ordenado
        groups: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["engine_id"], row["label"])
            if key not in groups:
                groups[key] = {
                    "engine_id": row["engine_id"],
                    "label":     row["label"],
                    "platform":  row["platform"],
                    "keywords":  [],
                }
            groups[key]["keywords"].append(row["keyword"])

        return list(groups.values())

    async def count(self) -> int:
        """Número total de keywords en la tabla."""
        return await self._db.table_count("keywords")