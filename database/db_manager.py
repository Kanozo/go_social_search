"""
database/db_manager.py
Gestor de SQLite con concurrencia segura a tres niveles.

  Nivel 1 — asyncio.Lock singleton por db_path (intra-proceso)
  ─────────────────────────────────────────────────────────────
  Compartido entre TODAS las instancias SQLiteManager del mismo proceso
  que apunten al mismo archivo. Garantiza que solo un coroutine ejecuta
  una escritura a la vez, independientemente de cuántas instancias existan.

  Nivel 2 — FileLock singleton por db_path (inter-proceso)
  ─────────────────────────────────────────────────────────
  Compartido de la misma forma. Como el asyncio.Lock ya garantiza que
  solo un coroutine llega al FileLock a la vez dentro del proceso,
  el FileLock nunca tiene contención intra-proceso: su única función
  es bloquear a procesos OS distintos.

  El FileLock se adquiere de forma síncrona (sin asyncio.to_thread)
  porque el asyncio.Lock que lo precede ya garantiza exclusión: no
  hay riesgo de bloquear el event loop esperando al FileLock porque
  ningún otro coroutine del mismo proceso puede tenerlo.

  Nivel 3 — BEGIN IMMEDIATE (SQLite nativo)
  ──────────────────────────────────────────
  Reserva el write lock de SQLite desde el inicio de la transacción,
  evitando SQLITE_BUSY con BEGIN DEFERRED en escenarios multi-proceso.

  Lecturas — sin ningún lock
  ───────────────────────────
  WAL mode permite N readers concurrentes sin bloqueo, desde cualquier
  número de coroutines y procesos simultáneamente.

  Flujo completo de una escritura
  ─────────────────────────────────
    coroutine A y B compiten:

    A: asyncio.Lock.acquire()  → granted  (B queda en espera asyncio)
    A: FileLock.acquire()      → granted  (síncrono, nadie más puede llegar aquí)
    A: BEGIN IMMEDIATE         → granted
    A: ... operaciones ...
    A: COMMIT
    A: FileLock.release()
    A: asyncio.Lock.release()  → B despierta
    B: asyncio.Lock.acquire()  → granted
    B: FileLock.acquire()      → granted  (A ya lo soltó)
    ...

  ADVERTENCIA sobre executescript
  ─────────────────────────────────
  SQLite emite un COMMIT implícito antes de executescript(). Nunca
  llames executescript() dentro de ``async with db.write()``.
  _apply_schema() lo gestiona directamente sobre la conexión.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, ClassVar

import aiosqlite
from filelock import FileLock, Timeout as FileLockTimeout

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_VERSION: int = 1

# Tiempo máximo (s) que el FileLock espera a otro proceso OS antes de error.
FILELOCK_TIMEOUT: float = float(os.getenv("SQLITE_FILELOCK_TIMEOUT", "30"))

# El asyncio.Lock espera un poco más para que el FileLock tenga margen.
ASYNCIO_LOCK_TIMEOUT: float = FILELOCK_TIMEOUT + 5.0

_PRAGMAS: list[str] = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA cache_size=-65536",      # 64 MB caché de páginas en RAM
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=268435456",    # 256 MB memory-mapped I/O
    "PRAGMA busy_timeout=5000",      # 5 s de última red ante SQLITE_BUSY
]

_DDL = """
CREATE TABLE IF NOT EXISTS keywords (
    id             INTEGER  PRIMARY KEY AUTOINCREMENT,
    keyword        TEXT     NOT NULL UNIQUE,
    classification TEXT     NOT NULL DEFAULT 'neutro'
                            CHECK (classification IN ('positivo','negativo','neutro')),
    last_scrap     DATETIME,
    label          TEXT     NOT NULL DEFAULT '',
    platform       TEXT     NOT NULL DEFAULT '',
    engine_id      TEXT     NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS posts (
    id        INTEGER  PRIMARY KEY AUTOINCREMENT,
    url       TEXT     NOT NULL UNIQUE,
    keyword   TEXT     NOT NULL,
    platform  TEXT     NOT NULL DEFAULT '',
    scrapt_at DATETIME,
    was_sent  DATETIME,
    sent_at   DATETIME
);

CREATE INDEX IF NOT EXISTS idx_keywords_engine   ON keywords (engine_id, label);
CREATE INDEX IF NOT EXISTS idx_keywords_platform ON keywords (platform);
CREATE INDEX IF NOT EXISTS idx_posts_keyword     ON posts    (keyword);
CREATE INDEX IF NOT EXISTS idx_posts_platform    ON posts    (platform);
CREATE INDEX IF NOT EXISTS idx_posts_scrapt_at   ON posts    (scrapt_at);
CREATE INDEX IF NOT EXISTS idx_posts_sent_at     ON posts    (sent_at);

CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER NOT NULL
);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Registry de singletons por db_path
# ─────────────────────────────────────────────────────────────────────────────

class _LockRegistry:
    """
    Registro de locks (asyncio.Lock + FileLock) indexados por ruta de DB.

    Garantiza que todas las instancias de SQLiteManager en el mismo proceso
    que apunten al mismo archivo compartan EL MISMO par de locks.

    Sin esto, dos instancias distintas tendrían asyncio.Locks independientes
    y podrían competir por el FileLock desde threads distintos, causando
    timeout porque filelock no es reentrant entre threads del mismo proceso.

    Thread-safe: el acceso al dict se protege con threading.Lock
    (no asyncio.Lock, porque el registry se usa en __init__ síncrono).
    """

    _mutex: ClassVar[threading.Lock] = threading.Lock()
    _asyncio_locks: ClassVar[dict[str, asyncio.Lock]] = {}
    _file_locks:    ClassVar[dict[str, FileLock]]     = {}

    @classmethod
    def get_asyncio_lock(cls, db_path: str) -> asyncio.Lock:
        """
        Devuelve el asyncio.Lock compartido para db_path.

        IMPORTANTE: asyncio.Lock es específico del event loop en el que
        se crea. Si el proceso usa un único event loop (asyncio.run),
        este singleton es seguro. En tests con múltiples loops (raro),
        se debe limpiar el registry entre tests.

        Args:
            db_path: Ruta absoluta canónica del archivo de DB.

        Returns:
            asyncio.Lock compartido para esa ruta.
        """
        with cls._mutex:
            if db_path not in cls._asyncio_locks:
                cls._asyncio_locks[db_path] = asyncio.Lock()
                logger.debug("asyncio.Lock creado para: %s", db_path)
            return cls._asyncio_locks[db_path]

    @classmethod
    def get_file_lock(cls, db_path: str) -> FileLock:
        """
        Devuelve el FileLock compartido para db_path.

        Args:
            db_path: Ruta absoluta canónica del archivo de DB.

        Returns:
            FileLock compartido para esa ruta.
        """
        with cls._mutex:
            if db_path not in cls._file_locks:
                cls._file_locks[db_path] = FileLock(
                    db_path + ".lock",
                    timeout=FILELOCK_TIMEOUT,
                )
                logger.debug("FileLock creado para: %s", db_path)
            return cls._file_locks[db_path]

    @classmethod
    def clear(cls) -> None:
        """
        Elimina todos los locks del registry.

        Útil en tests para reiniciar el estado entre casos.
        No llamar en producción mientras haya instancias activas.
        """
        with cls._mutex:
            cls._asyncio_locks.clear()
            cls._file_locks.clear()


# ─────────────────────────────────────────────────────────────────────────────
# SQLiteManager
# ─────────────────────────────────────────────────────────────────────────────

class SQLiteManager:
    """
    Gestor de SQLite con concurrencia segura a tres niveles.

    Todas las instancias que apunten al mismo archivo comparten los mismos
    locks (asyncio.Lock + FileLock via _LockRegistry), garantizando
    exclusión mutua tanto entre coroutines como entre procesos OS.

    Args:
        db_path: Ruta al archivo ``.db``. Se crea si no existe.
                 Usa ``:memory:`` para tests (FileLock omitido).

    Example — contexto recomendado::

        async with SQLiteManager("scraper.db") as db:
            async with db.write() as conn:
                await conn.execute("INSERT OR IGNORE INTO posts ...")
            rows = await db.execute_fetchall("SELECT * FROM keywords")

    Example — múltiples instancias mismo proceso (seguro)::

        db1 = SQLiteManager("scraper.db")
        db2 = SQLiteManager("scraper.db")
        # db1 y db2 comparten el mismo asyncio.Lock y FileLock.
        # No pueden escribir simultáneamente.
        await db1.connect()
        await db2.connect()
        # gather() serializa las escrituras correctamente:
        await asyncio.gather(
            some_writer(db1),
            other_writer(db2),
        )

    Example — múltiples procesos (seguro)::

        # Proceso A y B usan la misma ruta en disco.
        # El FileLock en disco garantiza exclusión entre procesos.
        # Dentro de cada proceso, el asyncio.Lock garantiza exclusión
        # entre coroutines.
    """

    def __init__(self, db_path: str | Path = "url_scraper.db") -> None:
        # Resolver ruta absoluta canónica para que el registry use la misma
        # clave independientemente de cómo se pasó la ruta.
        if str(db_path) == ":memory:":
            self._db_path   = ":memory:"
            self._in_memory = True
        else:
            self._db_path   = str(Path(db_path).resolve())
            self._in_memory = False

        self._conn: aiosqlite.Connection | None = None
        self._pid: int = os.getpid()

        if not self._in_memory:
            # Singletons compartidos con todas las instancias del mismo proceso
            self._write_lock: asyncio.Lock = _LockRegistry.get_asyncio_lock(self._db_path)
            self._file_lock:  FileLock     = _LockRegistry.get_file_lock(self._db_path)
        else:
            # :memory: → locks privados (cada instancia tiene su propia DB)
            self._write_lock = asyncio.Lock()
            self._file_lock  = None   # type: ignore[assignment]

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Abre la conexión, aplica pragmas WAL y crea el esquema.

        Idempotente dentro del mismo proceso. Detecta forks automáticamente
        y fuerza reconexión en el proceso hijo (el fd del padre no es seguro
        tras un fork sin exec).
        """
        if self._conn is not None and os.getpid() != self._pid:
            logger.warning(
                "Fork detectado (padre=%d, actual=%d). Forzando reconexión.",
                self._pid, os.getpid(),
            )
            self._conn = None
            self._pid  = os.getpid()

        if self._conn is not None:
            return   # Ya conectado en este proceso

        if not self._in_memory:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(
            self._db_path,
            isolation_level=None,   # Transacciones manuales: BEGIN/COMMIT/ROLLBACK
        )
        self._conn.row_factory = aiosqlite.Row

        for pragma in _PRAGMAS:
            await self._conn.execute(pragma)

        await self._apply_schema()
        self._pid = os.getpid()

        logger.info(
            "SQLite conectado [PID=%d]: %s | WAL=ON | FileLock=%s",
            self._pid,
            self._db_path,
            "activo" if not self._in_memory else "N/A (:memory:)",
        )

    async def disconnect(self) -> None:
        """
        Cierra la conexión con CHECKPOINT del WAL.

        El checkpoint consolida el archivo .wal en el .db principal,
        reduciendo el tamaño del WAL y asegurando que otros procesos
        que abran la DB vean todos los cambios inmediatamente.
        """
        if self._conn is None:
            return
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._conn.close()
            logger.info(
                "SQLite desconectado [PID=%d]: %s", os.getpid(), self._db_path
            )
        except Exception as exc:
            logger.warning("Error cerrando SQLite: %s", exc)
        finally:
            self._conn = None

    async def _apply_schema(self) -> None:
        """
        Aplica el DDL completo y registra la versión del esquema.

        Gestiona la transacción directamente sobre self._conn porque
        executescript() emite un COMMIT implícito que cerraría cualquier
        transacción BEGIN IMMEDIATE que estuviera activa. Por eso no
        usa el context manager write().
        """
        assert self._conn is not None

        async with self._write_lock:
            self._acquire_file_lock()
            try:
                await self._conn.executescript(_DDL)

                # Transacción separada para la versión del esquema
                await self._conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await self._conn.execute(
                        "SELECT version FROM _schema_version "
                        "ORDER BY version DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                    if row is None:
                        await self._conn.execute(
                            "INSERT INTO _schema_version (version) VALUES (?)",
                            (_SCHEMA_VERSION,),
                        )
                        logger.debug("Esquema v%d inicializado.", _SCHEMA_VERSION)
                    elif row["version"] < _SCHEMA_VERSION:
                        logger.warning(
                            "Versión DB (%d) < esperada (%d). Migración necesaria.",
                            row["version"], _SCHEMA_VERSION,
                        )
                    await self._conn.execute("COMMIT")
                except Exception:
                    await self._conn.execute("ROLLBACK")
                    raise
            finally:
                self._release_file_lock()

    # ── FileLock helpers (síncronos) ──────────────────────────────────────────

    def _acquire_file_lock(self) -> None:
        """
        Adquiere el FileLock de forma síncrona.

        Es seguro llamarlo de forma síncrona (sin to_thread) porque el
        asyncio.Lock que lo precede garantiza que solo un coroutine llega
        aquí a la vez dentro del proceso. Por tanto, el FileLock nunca
        tiene que esperar a otro coroutine del mismo proceso: su única
        función es bloquear a procesos OS distintos.

        Si otro proceso OS tiene el lock, filelock espera en un loop de
        polling con sleep(0.05s por defecto). Este bloqueo síncrono ocurre
        en el thread del event loop, pero dado que las escrituras son breves
        (< 100ms típicamente) el impacto es mínimo. Si el timeout es crítico,
        aumenta SQLITE_FILELOCK_TIMEOUT.

        Raises:
            RuntimeError: Si el lock no se obtiene en FILELOCK_TIMEOUT segundos.
        """
        if self._file_lock is None:
            return
        try:
            self._file_lock.acquire()
            logger.debug("[PID=%d] FileLock adquirido.", os.getpid())
        except FileLockTimeout as exc:
            raise RuntimeError(
                f"[PID={os.getpid()}] FileLock timeout ({FILELOCK_TIMEOUT}s) "
                f"en '{self._db_path}'. "
                "Otro proceso puede estar bloqueado o caído con el lock activo. "
                f"Ajusta SQLITE_FILELOCK_TIMEOUT (actual={FILELOCK_TIMEOUT}s) "
                f"o elimina manualmente '{self._db_path}.lock'."
            ) from exc

    def _release_file_lock(self) -> None:
        """Libera el FileLock. Siempre en bloque finally."""
        if self._file_lock is not None and self._file_lock.is_locked:
            self._file_lock.release()
            logger.debug("[PID=%d] FileLock liberado.", os.getpid())

    # ── Context managers ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Context manager de SOLO LECTURA. Sin ningún lock.

        WAL mode permite lecturas concurrentes ilimitadas desde cualquier
        número de coroutines y procesos simultáneamente sin espera.

        Yields:
            Conexión aiosqlite activa.

        Raises:
            RuntimeError: Si connect() no fue llamado antes.

        Example::

            async with db.read() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM keywords WHERE platform = ?",
                    ("instagram",),
                )
                rows = await cursor.fetchall()
        """
        self._assert_connected()
        try:
            yield self._conn  # type: ignore[misc]
        except Exception as exc:
            logger.debug("[PID=%d] Error en lectura: %s", os.getpid(), exc)
            raise

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Context manager de ESCRITURA (INSERT / UPDATE / DELETE).

        Orden de adquisición de locks:
          1. asyncio.Lock   → un coroutine a la vez en este proceso
                              (compartido entre todas las instancias del proceso
                               que apuntan al mismo archivo)
          2. FileLock       → un proceso OS a la vez en el sistema
                              (adquirido de forma síncrona; seguro porque el
                               asyncio.Lock garantiza que nadie más del proceso
                               puede llegar aquí al mismo tiempo)
          3. BEGIN IMMEDIATE → reserva el write lock de SQLite desde el inicio

        Garantías:
          · COMMIT automático al salir del bloque sin excepción.
          · ROLLBACK automático ante cualquier excepción.
          · Locks liberados siempre en el bloque finally.

        ADVERTENCIA: no uses executescript() dentro de este contexto.
        Su COMMIT implícito cerraría el BEGIN IMMEDIATE prematuramente.

        Yields:
            Conexión aiosqlite dentro de una transacción IMMEDIATE activa.

        Raises:
            RuntimeError: Si connect() no fue llamado, o si algún lock
                          no se obtiene en el timeout configurado.

        Example::

            async with db.write() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO posts "
                    "(url, keyword, platform, scrapt_at) VALUES (?,?,?,?)",
                    (url, keyword, platform, now_iso),
                )
                # COMMIT automático al salir
        """
        self._assert_connected()

        # ── Nivel 1: asyncio.Lock ─────────────────────────────────────────────
        try:
            await asyncio.wait_for(
                self._write_lock.acquire(),
                timeout=ASYNCIO_LOCK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"asyncio.Lock timeout ({ASYNCIO_LOCK_TIMEOUT}s). "
                "Posible deadlock intra-proceso."
            )

        # ── Nivel 2: FileLock (síncrono, seguro porque asyncio.Lock serializa) ─
        self._acquire_file_lock()

        # ── Nivel 3: BEGIN IMMEDIATE ──────────────────────────────────────────
        assert self._conn is not None
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            await self._conn.execute("COMMIT")
        except Exception as exc:
            try:
                await self._conn.execute("ROLLBACK")
            except Exception:
                pass
            logger.error(
                "[PID=%d] Escritura fallida → ROLLBACK: %s", os.getpid(), exc
            )
            raise
        finally:
            # Liberar siempre en orden inverso
            self._release_file_lock()
            if self._write_lock.locked():
                self._write_lock.release()

    # ── Shortcuts de lectura ──────────────────────────────────────────────────

    async def execute_fetchall(
        self, query: str, params: tuple = ()
    ) -> list[aiosqlite.Row]:
        """SELECT que devuelve todas las filas."""
        async with self.read() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()

    async def execute_fetchone(
        self, query: str, params: tuple = ()
    ) -> aiosqlite.Row | None:
        """SELECT que devuelve la primera fila o None."""
        async with self.read() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchone()

    async def table_count(self, table: str) -> int:
        """
        Número de filas de una tabla (solo tablas conocidas del esquema).

        Raises:
            ValueError: Si table no es una de las tablas permitidas.
                        Previene SQL injection en el nombre de tabla.
        """
        allowed = {"keywords", "posts", "_schema_version"}
        if table not in allowed:
            raise ValueError(
                f"Tabla no permitida: {table!r}. Permitidas: {allowed}"
            )
        row = await self.execute_fetchone(f"SELECT COUNT(*) AS n FROM {table}")
        return row["n"] if row else 0

    # ── Diagnóstico ───────────────────────────────────────────────────────────

    async def diagnostics(self) -> dict:
        """
        Métricas de estado para monitoreo y debugging.

        Returns:
            Dict con pid, db_path, journal_mode, tamaño en bytes y MB,
            schema_version, conteo de tablas, estado WAL y estado de locks.
        """
        self._assert_connected()
        assert self._conn is not None

        async with self.read() as conn:
            cur = await conn.execute("PRAGMA journal_mode")
            journal_mode = (await cur.fetchone())[0]
            cur = await conn.execute("PRAGMA page_count")
            page_count = (await cur.fetchone())[0]
            cur = await conn.execute("PRAGMA page_size")
            page_size = (await cur.fetchone())[0]
            cur = await conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            wal = await cur.fetchone()
            cur = await conn.execute(
                "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
            )
            sv = await cur.fetchone()

        db_size = page_count * page_size
        kw_count   = await self.table_count("keywords")
        post_count = await self.table_count("posts")

        return {
            "pid":                 os.getpid(),
            "db_path":             self._db_path,
            "journal_mode":        journal_mode,
            "db_size_bytes":       db_size,
            "db_size_mb":          round(db_size / 1_048_576, 2),
            "schema_version":      sv[0] if sv else None,
            "keywords_count":      kw_count,
            "posts_count":         post_count,
            "wal_busy":            wal[0] if wal else None,
            "wal_log_frames":      wal[1] if wal else None,
            "wal_checkpointed":    wal[2] if wal else None,
            "file_lock_path":      str(self._file_lock.lock_file) if self._file_lock else None,
            "file_lock_held":      self._file_lock.is_locked if self._file_lock else False,
            "asyncio_lock_locked": self._write_lock.locked(),
            "asyncio_lock_shared": not self._in_memory,
        }

    # ── Privados ──────────────────────────────────────────────────────────────

    def _assert_connected(self) -> None:
        """Lanza RuntimeError si la conexión no está activa."""
        if self._conn is None:
            raise RuntimeError(
                "SQLiteManager no conectado. "
                "Usa: async with SQLiteManager(...) as db:  "
                "o llama await db.connect() primero."
            )

    # ── Context manager de ciclo de vida ─────────────────────────────────────

    async def __aenter__(self) -> "SQLiteManager":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()