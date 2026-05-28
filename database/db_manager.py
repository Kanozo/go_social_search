"""
database/db_manager.py
Gestor maestro de SQLite con soporte async, WAL mode y serialización de escrituras.

Diseño de concurrencia
──────────────────────
SQLite en WAL (Write-Ahead Logging) mode permite:
  - Lecturas concurrentes ilimitadas (readers no bloquean al writer).
  - Un único writer a la vez (el lock lo garantiza a nivel asyncio).

``aiosqlite`` delega cada operación de DB a un thread dedicado via
``asyncio.to_thread``, por lo que nunca bloquea el event loop aunque
la query tarde. El ``asyncio.Lock`` interno garantiza que solo un
coroutine ejecute una escritura a la vez, evitando "database is locked".

Pragmas aplicados al conectar
──────────────────────────────
  PRAGMA journal_mode=WAL      → Lecturas concurrentes sin bloqueo.
  PRAGMA synchronous=NORMAL    → Buen balance durabilidad/rendimiento.
  PRAGMA foreign_keys=ON       → Integridad referencial activada.
  PRAGMA cache_size=-65536     → 64 MB de caché en memoria.
  PRAGMA temp_store=MEMORY     → Tablas temporales en RAM.
  PRAGMA mmap_size=268435456   → 256 MB de memory-mapped I/O.
  PRAGMA busy_timeout=5000     → 5s antes de devolver SQLITE_BUSY.

Uso básico
──────────
    manager = SQLiteManager("scraper.db")
    await manager.connect()
    async with manager.read() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM keywords")
    async with manager.write() as conn:
        await conn.execute("INSERT INTO keywords ...", (...,))
    await manager.disconnect()
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

logger = logging.getLogger(__name__)

# Versión actual del esquema. Incrementar al añadir columnas o tablas.
_SCHEMA_VERSION: int = 1

# DDL completo del esquema
_DDL = """
-- ── keywords ────────────────────────────────────────────────────────────────
-- Almacena los términos de búsqueda con su configuración y estado.
-- engine_id permite agrupar keywords por motor CSE en _fetch_engines_config.
CREATE TABLE IF NOT EXISTS keywords (
    id             INTEGER  PRIMARY KEY AUTOINCREMENT,
    keyword        TEXT     NOT NULL UNIQUE,
    classification TEXT     NOT NULL DEFAULT 'neutro'
                            CHECK (classification IN ('positivo', 'negativo', 'neutro')),
    last_scrap     DATETIME,                          -- UTC, NULL = nunca scrapeado
    label          TEXT     NOT NULL DEFAULT '',      -- etiqueta del grupo de búsqueda
    platform       TEXT     NOT NULL DEFAULT '',      -- "instagram" | "facebook" | ...
    engine_id      TEXT     NOT NULL DEFAULT ''       -- ID del Google CSE
);

-- ── posts ────────────────────────────────────────────────────────────────────
-- Una fila por URL única. Los campos *_at son NULL hasta que ocurre el evento.
CREATE TABLE IF NOT EXISTS posts (
    id        INTEGER  PRIMARY KEY AUTOINCREMENT,
    url       TEXT     NOT NULL UNIQUE,
    keyword   TEXT     NOT NULL,
    platform  TEXT     NOT NULL DEFAULT '',
    scrapt_at DATETIME,           -- UTC: momento en que se scrapeó
    was_sent  DATETIME,           -- UTC: momento en que se encoló para envío
    sent_at   DATETIME            -- UTC: momento en que el envío fue confirmado
);

-- ── índices ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_keywords_engine   ON keywords (engine_id, label);
CREATE INDEX IF NOT EXISTS idx_keywords_platform ON keywords (platform);
CREATE INDEX IF NOT EXISTS idx_posts_keyword     ON posts    (keyword);
CREATE INDEX IF NOT EXISTS idx_posts_platform    ON posts    (platform);
CREATE INDEX IF NOT EXISTS idx_posts_scrapt_at   ON posts    (scrapt_at);
CREATE INDEX IF NOT EXISTS idx_posts_sent_at     ON posts    (sent_at);

-- ── metadata de versión de esquema ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER NOT NULL
);
"""


class SQLiteManager:
    """
    Gestor de ciclo de vida de una base de datos SQLite.

    Responsabilidades:
      - Abrir/cerrar la conexión aiosqlite (una por instancia).
      - Aplicar pragmas de rendimiento y configurar WAL al conectar.
      - Crear/migrar el esquema si es necesario.
      - Serializar escrituras con un ``asyncio.Lock``.
      - Proveer context managers tipados para lectura y escritura.

    Args:
        db_path: Ruta al archivo .db. Se crea si no existe.
                 Pasar ``:memory:`` para una base de datos en RAM (tests).

    Example::

        manager = SQLiteManager("url_scraper.db")
        await manager.connect()
        try:
            async with manager.write() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO keywords (keyword, label, ...) VALUES (?, ?, ...)",
                    ("Cuba", "cluster", ...),
                )
        finally:
            await manager.disconnect()
    """

    def __init__(self, db_path: str | Path = "scraper.db") -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        # Lock de escritura: garantiza un único writer async a la vez.
        self._write_lock = asyncio.Lock()

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Abre la conexión, configura pragmas y aplica el esquema DDL.

        Idempotente: si ya está conectado, no hace nada.

        Raises:
            aiosqlite.OperationalError: Si la ruta del archivo no es accesible.
        """
        if self._conn is not None:
            return

        # Asegurar que el directorio padre existe
        parent = Path(self._db_path).parent
        if str(parent) != "." and self._db_path != ":memory:":
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(
            self._db_path,
            # Devolver filas como sqlite3.Row (acceso por nombre de columna)
            isolation_level=None,   # Autocommit desactivado; control manual de tx
        )
        self._conn.row_factory = aiosqlite.Row

        # ── Pragmas de configuración ──────────────────────────────────────────
        pragmas = [
            "PRAGMA journal_mode=WAL",        # Lecturas concurrentes sin bloqueo
            "PRAGMA synchronous=NORMAL",       # Balance durabilidad/rendimiento
            "PRAGMA foreign_keys=ON",          # Integridad referencial
            "PRAGMA cache_size=-65536",        # 64 MB de caché de páginas en RAM
            "PRAGMA temp_store=MEMORY",        # Tablas temporales en memoria
            "PRAGMA mmap_size=268435456",      # 256 MB de memory-mapped I/O
            "PRAGMA busy_timeout=5000",        # Esperar 5s antes de SQLITE_BUSY
        ]
        for pragma in pragmas:
            await self._conn.execute(pragma)

        await self._apply_schema()
        logger.info("SQLite conectado: %s (WAL mode)", self._db_path)

    async def disconnect(self) -> None:
        """
        Cierra la conexión de forma limpia.

        Hace CHECKPOINT del WAL antes de cerrar para consolidar páginas
        pendientes y reducir el tamaño del archivo .wal.
        """
        if self._conn is None:
            return
        try:
            # Checkpoint: mueve el WAL al archivo principal
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._conn.close()
            logger.info("SQLite desconectado: %s", self._db_path)
        except Exception as exc:
            logger.warning("Error al cerrar SQLite: %s", exc)
        finally:
            self._conn = None

    async def _apply_schema(self) -> None:
        """
        Crea las tablas si no existen y verifica la versión del esquema.

        Usa ``executescript`` para ejecutar el DDL completo en una sola
        transacción. Si la versión en DB es menor que ``_SCHEMA_VERSION``,
        registra una advertencia (migraciones automáticas no implementadas).
        """
        assert self._conn is not None
        async with self._write_lock:
            # executescript hace commit implícito; válido para DDL
            await self._conn.executescript(_DDL)
            await self._conn.commit()

            # Verificar/registrar versión del esquema
            cursor = await self._conn.execute(
                "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row is None:
                await self._conn.execute(
                    "INSERT INTO _schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
                await self._conn.commit()
                logger.debug("Esquema inicializado en versión %d.", _SCHEMA_VERSION)
            elif row["version"] < _SCHEMA_VERSION:
                logger.warning(
                    "Versión de esquema en DB (%d) < versión esperada (%d). "
                    "Considera ejecutar migraciones.",
                    row["version"], _SCHEMA_VERSION,
                )

    # ── Context managers ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Context manager para operaciones de SOLO LECTURA.

        No adquiere el write lock → múltiples readers concurrentes son posibles
        gracias al WAL mode. No hace commit al salir.

        Yields:
            Conexión aiosqlite activa.

        Raises:
            RuntimeError: Si ``connect()`` no fue llamado antes.

        Example::

            async with manager.read() as conn:
                cursor = await conn.execute("SELECT * FROM keywords WHERE platform=?", ("instagram",))
                rows = await cursor.fetchall()
        """
        self._assert_connected()
        try:
            yield self._conn  # type: ignore[misc]
        except Exception as exc:
            logger.debug("Error en operación de lectura: %s", exc)
            raise

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Context manager para operaciones de ESCRITURA (INSERT/UPDATE/DELETE).

        Adquiere el ``_write_lock`` antes de entrar y hace ``COMMIT`` al
        salir exitosamente, o ``ROLLBACK`` si hay excepción.
        Solo un coroutine puede estar en este bloque a la vez.

        Yields:
            Conexión aiosqlite activa, dentro de una transacción.

        Raises:
            RuntimeError: Si ``connect()`` no fue llamado antes.

        Example::

            async with manager.write() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO posts (url, keyword, platform, scrapt_at) "
                    "VALUES (?, ?, ?, ?)",
                    (url, keyword, platform, now_iso()),
                )
        """
        self._assert_connected()
        async with self._write_lock:
            assert self._conn is not None
            await self._conn.execute("BEGIN")
            try:
                yield self._conn
                await self._conn.execute("COMMIT")
            except Exception as exc:
                await self._conn.execute("ROLLBACK")
                logger.error("Escritura SQLite fallida → ROLLBACK: %s", exc)
                raise

    # ── Utilidades públicas ───────────────────────────────────────────────────

    async def execute_fetchall(
        self,
        query: str,
        params: tuple = (),
    ) -> list[aiosqlite.Row]:
        """
        Shorthand para SELECT que devuelve todas las filas.

        Args:
            query:  Query SQL de lectura.
            params: Parámetros posicionales (evita SQL injection).

        Returns:
            Lista de ``aiosqlite.Row`` (acceso por nombre o índice).
        """
        async with self.read() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()

    async def execute_fetchone(
        self,
        query: str,
        params: tuple = (),
    ) -> aiosqlite.Row | None:
        """
        Shorthand para SELECT que devuelve una sola fila o None.

        Args:
            query:  Query SQL de lectura.
            params: Parámetros posicionales.

        Returns:
            Una ``aiosqlite.Row`` o None si no hay resultado.
        """
        async with self.read() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchone()

    async def table_count(self, table: str) -> int:
        """
        Devuelve el número de filas de una tabla.

        Args:
            table: Nombre de la tabla (no parametrizable por SQL; validado internamente).

        Returns:
            Número de filas como entero.
        """
        # Validación básica para evitar SQL injection en el nombre de tabla
        allowed = {"keywords", "posts", "_schema_version"}
        if table not in allowed:
            raise ValueError(f"Tabla no permitida: {table!r}")
        row = await self.execute_fetchone(f"SELECT COUNT(*) AS n FROM {table}")
        return row["n"] if row else 0

    # ── Privados ──────────────────────────────────────────────────────────────

    def _assert_connected(self) -> None:
        """Lanza RuntimeError si la conexión no está activa."""
        if self._conn is None:
            raise RuntimeError(
                "SQLiteManager no está conectado. "
                "Llama await manager.connect() antes de usarlo."
            )