"""
database/
Capa de persistencia SQLite del scraper.

Exports públicos::

    SQLiteManager      → gestor de conexión WAL + write lock
    KeywordRepository  → CRUD para la tabla keywords
    PostRepository     → CRUD para la tabla posts
    Keyword            → dataclass de lectura de keyword
    KeywordCreate      → DTO para insertar keyword
    Post               → dataclass de lectura de post
    PostCreate         → DTO para insertar post
"""
from database.db_manager import SQLiteManager
from database.keyword_repo import KeywordRepository
from database.models import Keyword, KeywordCreate, Post, PostCreate
from database.post_repo import PostRepository

__all__ = [
    "SQLiteManager",
    "KeywordRepository",
    "PostRepository",
    "Keyword",
    "KeywordCreate",
    "Post",
    "PostCreate",
]