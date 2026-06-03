# Manual de Usuario — Base de Datos SQLite del Scraper

## Índice

1. [Visión general](#1-visión-general)
2. [Esquema de tablas](#2-esquema-de-tablas)
3. [Configuración](#3-configuración)
4. [Gestión de Keywords](#4-gestión-de-keywords)
5. [Gestión de Posts](#5-gestión-de-posts)
6. [Consultas frecuentes](#6-consultas-frecuentes)
7. [Administración y mantenimiento](#7-administración-y-mantenimiento)
8. [Referencia de la API Python](#8-referencia-de-la-api-python)
9. [Solución de problemas](#9-solución-de-problemas)

---

## 1. Visión general

El scraper usa **SQLite 3** como base de datos embebida, accedida de forma asíncrona mediante la librería `aiosqlite`. No requiere servidor externo ni proceso separado.

### Arquitectura de concurrencia

```
                    asyncio event loop
                           │
              ┌────────────┴────────────┐
              │                         │
         reader 1                  writer 1   ←── asyncio.Lock
         reader 2                  (serializado)
         reader N
              │                         │
         WAL mode                  WAL mode
     (sin bloqueo)            (un writer a la vez)
              └────────────┬────────────┘
                      scraper.db
                     ├── scraper.db-wal
                     └── scraper.db-shm
```

- **WAL mode** (Write-Ahead Logging): las lecturas no bloquean al writer y viceversa. Múltiples coroutines pueden leer simultáneamente.
- **`asyncio.Lock`**: serializa las escrituras a nivel de coroutine. Garantiza "un writer a la vez" sin bloquear el event loop.
- **`PRAGMA busy_timeout=5000`**: si SQLite reporta `SQLITE_BUSY`, espera hasta 5 segundos antes de lanzar error.

### Archivos en disco

| Archivo | Descripción |
|---|---|
| `scraper.db` | Base de datos principal |
| `scraper.db-wal` | Write-Ahead Log (activo durante operaciones) |
| `scraper.db-shm` | Shared memory file (índice del WAL) |

Los archivos `-wal` y `-shm` se consolidan en `scraper.db` automáticamente en cada `PRAGMA wal_checkpoint` (ejecutado al desconectar).

---

## 2. Esquema de tablas

### Tabla `keywords`

Almacena los términos de búsqueda con su configuración y estado de scraping.

```sql
CREATE TABLE keywords (
    id             INTEGER  PRIMARY KEY AUTOINCREMENT,
    keyword        TEXT     NOT NULL UNIQUE,
    classification TEXT     NOT NULL DEFAULT 'neutro'
                            CHECK (classification IN ('positivo', 'negativo', 'neutro')),
    last_scrap     DATETIME,          -- UTC ISO-8601; NULL = nunca scrapeado
    label          TEXT     NOT NULL DEFAULT '',   -- grupo/cluster al que pertenece
    platform       TEXT     NOT NULL DEFAULT '',   -- "instagram" | "facebook" | ...
    engine_id      TEXT     NOT NULL DEFAULT ''    -- ID del Google CSE
);
```

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PK | Auto-generado por SQLite |
| `keyword` | TEXT UNIQUE | Término de búsqueda (case-sensitive) |
| `classification` | TEXT | `positivo` \| `negativo` \| `neutro` |
| `last_scrap` | DATETIME | Última vez procesado (UTC). `NULL` = nunca |
| `label` | TEXT | Etiqueta del grupo (p.ej. `"IG-KW-Engine"`) |
| `platform` | TEXT | Plataforma objetivo (`"instagram"`, `"facebook"`) |
| `engine_id` | TEXT | ID del Google Custom Search Engine |

**Índices:**
- `idx_keywords_engine` → `(engine_id, label)` — búsqueda por grupo
- `idx_keywords_platform` → `(platform)` — filtrado por plataforma

---

### Tabla `posts`

Almacena las URLs de posts scrapeados. Cada URL es única en la tabla.

```sql
CREATE TABLE posts (
    id        INTEGER  PRIMARY KEY AUTOINCREMENT,
    url       TEXT     NOT NULL UNIQUE,
    keyword   TEXT     NOT NULL,
    platform  TEXT     NOT NULL DEFAULT '',
    scrapt_at DATETIME,    -- UTC: momento del scraping
    was_sent  DATETIME,    -- UTC: momento en que entró en cola de envío
    sent_at   DATETIME     -- UTC: momento de confirmación de envío exitoso
);
```

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER PK | Auto-generado por SQLite |
| `url` | TEXT UNIQUE | URL canónica del post (sin parámetros de tracking) |
| `keyword` | TEXT | Keyword que originó este resultado |
| `platform` | TEXT | Plataforma (`"instagram"`, `"facebook"`) |
| `scrapt_at` | DATETIME | Fijado en el INSERT. `NULL` solo antes de insertar |
| `was_sent` | DATETIME | Fijado al llamar `mark_queued_for_send()`. Puede ser `NULL` |
| `sent_at` | DATETIME | Fijado al confirmar envío exitoso. `NULL` = no enviado |

**Ciclo de vida de un post:**
```
INSERT (scrapt_at=now, was_sent=NULL, sent_at=NULL)
    → mark_queued_for_send() → was_sent=now
        → mark_sent()        → sent_at=now
```

**Índices:**
- `idx_posts_keyword`  → `(keyword)`
- `idx_posts_platform` → `(platform)`
- `idx_posts_scrapt_at` → `(scrapt_at)`
- `idx_posts_sent_at`  → `(sent_at)`

---

### Tabla `_schema_version`

Tabla interna para control de versiones del esquema. No modificar manualmente.

```sql
CREATE TABLE _schema_version (version INTEGER NOT NULL);
```

---

## 3. Configuración

### Variables de entorno

```bash
# Modo de salida: dónde se guardan los resultados
OUTPUT_MODE=sqlite          # "sqlite" (default) | "api"

# Ruta del archivo de base de datos
# (relativo a BASE_DIR; se crea automáticamente si no existe)
# La ruta hardcodeada en el código es: BASE_DIR/scraper.db
# Para cambiarla, modificar run_scraper.py → _db_connect()

# Nombre de la colección de resultados en SQLite
GOOGLE_RESULTS_COLLECTION=google_results
```

### Primer arranque

Al ejecutar el scraper por primera vez:

1. SQLite crea `scraper.db` automáticamente.
2. Se aplican todos los `CREATE TABLE IF NOT EXISTS`.
3. Se registra la versión del esquema en `_schema_version`.
4. Si la tabla `keywords` está vacía, el orquestador usa `_FALLBACK_ENGINES`.

```bash
# Verificar que la DB se creó correctamente
python -c "
import asyncio
from database import SQLiteManager, KeywordRepository

async def check():
    db = SQLiteManager('scraper.db')
    await db.connect()
    repo = KeywordRepository(db)
    print(f'Keywords: {await repo.count()}')
    await db.disconnect()

asyncio.run(check())
"
```

---

## 4. Gestión de Keywords

### 4.1 Insertar keywords

#### Desde Python (script de seed)

```python
import asyncio
from database import SQLiteManager, KeywordRepository
from database.models import KeywordCreate

KEYWORDS_SEED = [
    KeywordCreate(
        keyword="#LaPatriaSeDefiende",
        label="IG-KW-Engine",
        platform="instagram",
        engine_id="c4b97eed1414fcb14",
        classification="positivo",
    ),
    KeywordCreate(
        keyword="#Cuba",
        label="FB-KW-Engine",
        platform="facebook",
        engine_id="b3d8ab5d4c4a84c70",
        classification="neutro",
    ),
    # ... más keywords
]

async def seed():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = KeywordRepository(db)
    inserted, updated = await repo.bulk_upsert(KEYWORDS_SEED)
    print(f"Insertados: {inserted} | Actualizados: {updated}")
    await db.disconnect()

asyncio.run(seed())
```

#### Desde SQLite CLI

```bash
sqlite3 scraper.db << 'SQL'
INSERT INTO keywords (keyword, label, platform, engine_id, classification)
VALUES
    ('#Cuba',               'IG-Engine', 'instagram', 'abc123', 'neutro'),
    ('#LaPatriaSeDefiende', 'IG-Engine', 'instagram', 'abc123', 'positivo'),
    ('#CubaLibre',          'FB-Engine', 'facebook',  'def456', 'neutro');
SQL
```

### 4.2 Listar keywords

```python
async def list_keywords():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = KeywordRepository(db)

    # Todos
    all_kws = await repo.get_all()

    # Solo Instagram
    ig_kws = await repo.get_all(platform="instagram")

    # Solo positivos
    pos_kws = await repo.get_all(classification="positivo")

    for kw in all_kws:
        print(f"[{kw.id}] {kw.keyword} | {kw.platform} | {kw.label} | last: {kw.last_scrap}")

    await db.disconnect()
```

```sql
-- Desde SQLite CLI: todos los keywords ordenados
SELECT id, keyword, platform, label, classification, last_scrap
FROM   keywords
ORDER  BY label, keyword;

-- Keywords nunca scrapeados
SELECT keyword, label FROM keywords WHERE last_scrap IS NULL;

-- Keywords del engine específico
SELECT keyword FROM keywords WHERE engine_id = 'c4b97eed1414fcb14';
```

### 4.3 Ver agrupación por engine (lo que usa el orquestador)

```python
async def show_engine_groups():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = KeywordRepository(db)
    groups = await repo.get_engine_groups()
    for g in groups:
        print(f"\nEngine: {g['engine_id']} | Label: {g['label']} | Platform: {g['platform']}")
        for kw in g['keywords']:
            print(f"  - {kw}")
    await db.disconnect()
```

### 4.4 Actualizar clasificación

```python
async def update_classification():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = KeywordRepository(db)
    ok = await repo.update_classification("#Cuba", "positivo")
    print("Actualizado" if ok else "No encontrado")
    await db.disconnect()
```

```sql
-- Desde SQLite CLI
UPDATE keywords SET classification = 'negativo' WHERE keyword = '#Ejemplo';
```

### 4.5 Eliminar keyword

```python
async def delete_keyword():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = KeywordRepository(db)
    ok = await repo.delete("#CubaLibre")
    print("Eliminado" if ok else "No encontrado")
    await db.disconnect()
```

### 4.6 Ver cuándo se procesó cada keyword (last_scrap)

```sql
-- Keywords procesados en las últimas 24 horas
SELECT keyword, last_scrap
FROM   keywords
WHERE  last_scrap >= datetime('now', '-24 hours')
ORDER  BY last_scrap DESC;

-- Keywords más antiguos (sin scraping reciente)
SELECT keyword, last_scrap
FROM   keywords
ORDER  BY last_scrap ASC NULLS FIRST
LIMIT  20;
```

---

## 5. Gestión de Posts

### 5.1 Consultar posts scrapeados

```python
async def show_posts():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = PostRepository(db)

    # Total de posts
    total = await repo.count()
    print(f"Total posts: {total}")

    # Posts sin enviar
    pending = await repo.count(only_unsent=True)
    print(f"Pendientes de envío: {pending}")

    # Posts de Instagram de las últimas 48h
    recent = await repo.get_recent(hours=48, platform="instagram")
    for p in recent:
        print(f"[{p.scrapt_at}] {p.url}")

    await db.disconnect()
```

```sql
-- Posts scrapeados hoy
SELECT url, keyword, platform, scrapt_at
FROM   posts
WHERE  scrapt_at >= date('now')
ORDER  BY scrapt_at DESC;

-- Posts sin enviar por plataforma
SELECT platform, COUNT(*) AS pendientes
FROM   posts
WHERE  sent_at IS NULL
GROUP  BY platform;

-- Posts de una keyword específica
SELECT url, scrapt_at, sent_at
FROM   posts
WHERE  keyword = '#Cuba'
ORDER  BY scrapt_at DESC
LIMIT  50;
```

### 5.2 Marcar posts como enviados

```python
async def mark_posts_sent():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = PostRepository(db)

    # Marcar un post como en cola de envío
    await repo.mark_queued_for_send("https://instagram.com/p/abc123")

    # Confirmar envío exitoso
    await repo.mark_sent("https://instagram.com/p/abc123")

    # Marcar muchos posts enviados en batch (más eficiente)
    urls_enviadas = [
        "https://instagram.com/p/abc123",
        "https://instagram.com/p/def456",
        "https://facebook.com/post/789",
    ]
    updated = await repo.mark_many_sent(urls_enviadas)
    print(f"Marcados como enviados: {updated}")

    await db.disconnect()
```

### 5.3 Obtener posts pendientes de envío (worker independiente)

```python
async def process_pending_posts():
    """
    Patrón recomendado para un worker de envío independiente del scraper.
    """
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = PostRepository(db)

    # Obtener los primeros 200 posts sin enviar (FIFO por scrapt_at)
    pending = await repo.get_pending_send(limit=200, platform="instagram")
    print(f"Procesando {len(pending)} posts pendientes...")

    sent_urls = []
    for post in pending:
        # Marcar en cola antes del envío (trazabilidad)
        await repo.mark_queued_for_send(post.url)

        # Tu lógica de envío aquí...
        # success = await send_to_external_api(post.url)
        success = True  # Ejemplo

        if success:
            sent_urls.append(post.url)

    # Confirmar todos los enviados en un solo batch (una transacción)
    total_sent = await repo.mark_many_sent(sent_urls)
    print(f"Confirmados como enviados: {total_sent}")

    await db.disconnect()
```

### 5.4 Verificar si una URL ya existe

```python
async def check_url():
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = PostRepository(db)

    url = "https://instagram.com/p/abc123"
    exists = await repo.url_exists(url)
    print(f"URL {'ya existe' if exists else 'es nueva'}")

    await db.disconnect()
```

```sql
-- Desde SQLite CLI
SELECT EXISTS(SELECT 1 FROM posts WHERE url = 'https://instagram.com/p/abc123');
```

---

## 6. Consultas frecuentes

### Estadísticas generales

```sql
-- Resumen completo del estado de la DB
SELECT
    (SELECT COUNT(*) FROM keywords)                                AS total_keywords,
    (SELECT COUNT(*) FROM keywords WHERE last_scrap IS NULL)       AS keywords_sin_scraping,
    (SELECT COUNT(*) FROM posts)                                   AS total_posts,
    (SELECT COUNT(*) FROM posts WHERE sent_at IS NULL)             AS posts_pendientes,
    (SELECT COUNT(*) FROM posts WHERE sent_at IS NOT NULL)         AS posts_enviados,
    (SELECT COUNT(*) FROM posts WHERE scrapt_at >= date('now'))    AS posts_hoy;
```

### Posts por plataforma y estado

```sql
SELECT
    platform,
    COUNT(*)                                           AS total,
    SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS enviados,
    SUM(CASE WHEN sent_at IS NULL     THEN 1 ELSE 0 END) AS pendientes
FROM posts
GROUP BY platform;
```

### Keywords más productivos

```sql
-- Keywords que más posts han generado
SELECT
    k.keyword,
    k.platform,
    COUNT(p.id) AS total_posts,
    k.last_scrap
FROM keywords k
LEFT JOIN posts p ON p.keyword = k.keyword
GROUP BY k.keyword
ORDER BY total_posts DESC
LIMIT 20;
```

### Posts duplicados detectados (diagnóstico)

```sql
-- Verificar que no hay URLs duplicadas (debe retornar 0)
SELECT url, COUNT(*) AS repeticiones
FROM posts
GROUP BY url
HAVING COUNT(*) > 1;
```

### Actividad por hora del día

```sql
-- Distribución de scraping por hora
SELECT
    strftime('%H', scrapt_at) AS hora,
    COUNT(*) AS posts
FROM posts
WHERE scrapt_at >= datetime('now', '-7 days')
GROUP BY hora
ORDER BY hora;
```

### Últimas 10 URLs scrapeadas

```sql
SELECT url, keyword, platform, scrapt_at
FROM   posts
ORDER  BY scrapt_at DESC
LIMIT  10;
```

---

## 7. Administración y mantenimiento

### 7.1 Backup manual

```bash
# Método seguro (SQLite online backup, no bloquea operaciones activas)
sqlite3 scraper.db ".backup scraper_backup_$(date +%Y%m%d).db"

# O simplemente copiar cuando el scraper no está corriendo
cp scraper.db scraper_backup_$(date +%Y%m%d_%H%M%S).db
```

### 7.2 Vacuum (compactar el archivo)

```bash
# Después de borrar muchos registros, el archivo no se reduce automáticamente.
# VACUUM reconstruye la DB y libera espacio. Requiere 2x el espacio en disco.
sqlite3 scraper.db "VACUUM;"
```

### 7.3 Limpiar posts antiguos

```sql
-- Borrar posts enviados hace más de 30 días
DELETE FROM posts
WHERE  sent_at IS NOT NULL
AND    sent_at < datetime('now', '-30 days');

-- Borrar posts no enviados de hace más de 7 días (probablemente obsoletos)
DELETE FROM posts
WHERE  sent_at IS NULL
AND    scrapt_at < datetime('now', '-7 days');
```

### 7.4 Resetear last_scrap de todos los keywords

```sql
-- Útil para forzar un re-scraping completo
UPDATE keywords SET last_scrap = NULL;
```

### 7.5 Ver tamaño de la DB y páginas

```sql
PRAGMA page_count;
PRAGMA page_size;
PRAGMA freelist_count;  -- páginas libres (candidatas a VACUUM)
```

### 7.6 Checkpoint manual del WAL

```sql
-- Consolida el WAL en el archivo principal (reduce tamaño de scraper.db-wal)
PRAGMA wal_checkpoint(TRUNCATE);
```

### 7.7 Verificar integridad

```bash
sqlite3 scraper.db "PRAGMA integrity_check;"
# Debe retornar: ok
```

### 7.8 Exportar a CSV

```bash
# Exportar tabla posts a CSV
sqlite3 -csv -header scraper.db "SELECT * FROM posts ORDER BY scrapt_at DESC;" > posts_export.csv

# Exportar keywords
sqlite3 -csv -header scraper.db "SELECT * FROM keywords ORDER BY label, keyword;" > keywords_export.csv
```

### 7.9 Importar keywords desde CSV

Formato del CSV: `keyword,label,platform,engine_id,classification`

```python
import asyncio
import csv
from database import SQLiteManager, KeywordRepository
from database.models import KeywordCreate

async def import_from_csv(csv_path: str) -> None:
    db = SQLiteManager("scraper.db")
    await db.connect()
    repo = KeywordRepository(db)

    keywords = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keywords.append(KeywordCreate(
                keyword=row["keyword"].strip(),
                label=row["label"].strip(),
                platform=row["platform"].strip(),
                engine_id=row["engine_id"].strip(),
                classification=row.get("classification", "neutro"),
            ))

    inserted, updated = await repo.bulk_upsert(keywords)
    print(f"Importados: {inserted} nuevos, {updated} actualizados")
    await db.disconnect()

asyncio.run(import_from_csv("keywords.csv"))
```

---

## 8. Referencia de la API Python

### SQLiteManager

```python
from database import SQLiteManager

db = SQLiteManager("scraper.db")   # o ":memory:" para tests

await db.connect()            # Abre conexión, aplica pragmas, crea tablas
await db.disconnect()         # Checkpoint WAL + cierre limpio

# Context managers
async with db.read() as conn:       # Lectura sin lock
    rows = await conn.execute_fetchall("SELECT * FROM posts LIMIT 10")

async with db.write() as conn:      # Escritura con lock (BEGIN/COMMIT/ROLLBACK)
    await conn.execute("UPDATE posts SET sent_at = ? WHERE url = ?", (now, url))

# Shortcuts
rows = await db.execute_fetchall("SELECT * FROM keywords", ())
row  = await db.execute_fetchone("SELECT * FROM posts WHERE id = ?", (42,))
n    = await db.table_count("posts")   # "keywords" | "posts" | "_schema_version"
```

### KeywordRepository

```python
from database import KeywordRepository
repo = KeywordRepository(db)

# Inserción
inserted, updated = await repo.bulk_upsert(list[KeywordCreate])
keyword_obj       = await repo.insert_one(KeywordCreate(...))   # None si ya existe

# Lectura
kw     = await repo.get_by_id(42)
kw     = await repo.get_by_keyword("#Cuba")
all_kw = await repo.get_all(platform="instagram", classification="positivo")
groups = await repo.get_engine_groups()   # → list[dict] para el orquestador
total  = await repo.count()

# Actualización
ok = await repo.mark_scraped("#Cuba")                          # → bool
ok = await repo.update_classification("#Cuba", "positivo")    # → bool
ok = await repo.delete("#Cuba")                               # → bool
```

### PostRepository

```python
from database import PostRepository
repo = PostRepository(db)

# Inserción (INSERT OR IGNORE — duplicados descartados silenciosamente)
inserted, skipped = await repo.bulk_insert_new(list[PostCreate])
post              = await repo.insert_one(PostCreate(...))    # None si duplicado

# Ciclo de vida
ok      = await repo.mark_queued_for_send("https://…")  # was_sent = now
ok      = await repo.mark_sent("https://…")             # sent_at = now
updated = await repo.mark_many_sent(list[str])          # batch, → int

# Lectura
post    = await repo.get_by_id(42)
post    = await repo.get_by_url("https://…")
exists  = await repo.url_exists("https://…")            # → bool (sin cargar fila)
pending = await repo.get_pending_send(limit=200, platform="instagram")
by_kw   = await repo.get_by_keyword("#Cuba", limit=100)
recent  = await repo.get_recent(hours=24, platform="facebook")
total   = await repo.count(only_unsent=True, platform="instagram")
```

---

## 9. Solución de problemas

### `database is locked`

**Causa:** Otro proceso tiene una escritura activa y el `busy_timeout` (5s) expiró.

**Solución:**
```bash
# Verificar procesos usando la DB
lsof scraper.db

# Si hay un proceso zombie, terminarlo
kill <PID>

# Forzar checkpoint manual
sqlite3 scraper.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### `database disk image is malformed`

**Causa:** Corrupción del archivo (pérdida de energía, disco lleno).

**Solución:**
```bash
# Intentar recuperar datos
sqlite3 scraper.db ".recover" | sqlite3 scraper_recovered.db

# Verificar integridad del archivo recuperado
sqlite3 scraper_recovered.db "PRAGMA integrity_check;"
```

### La tabla `keywords` está vacía en la primera ejecución

**Causa:** Normal en el primer arranque.

**Solución:** El orquestador usa `_FALLBACK_ENGINES` automáticamente. Carga tus keywords:

```python
asyncio.run(seed())  # Tu script de seed
```

### `CHECK constraint failed: classification`

**Causa:** Se intenta insertar un valor de `classification` distinto de `positivo`, `negativo` o `neutro`.

**Solución:** Verificar que el valor sea exactamente uno de los tres permitidos (minúsculas).

### El archivo `scraper.db-wal` crece demasiado

**Causa:** Muchas escrituras sin checkpoint.

**Solución:**
```sql
-- El checkpoint ocurre automáticamente al desconectar.
-- Para forzarlo manualmente:
PRAGMA wal_checkpoint(TRUNCATE);
```

### Posts duplicados en la DB

**Causa:** No debería ocurrir (UNIQUE constraint). Si se detectan, verificar:

```sql
SELECT url, COUNT(*) FROM posts GROUP BY url HAVING COUNT(*) > 1;
```

Si hay duplicados (posible en DB migrada de otra fuente):
```sql
-- Eliminar duplicados conservando el más antiguo
DELETE FROM posts
WHERE id NOT IN (
    SELECT MIN(id) FROM posts GROUP BY url
);
```

---

*Versión del esquema: 1 | Generado para scraper v2.0 con SQLite WAL mode*