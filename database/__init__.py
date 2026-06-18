"""
Módulo de base de datos.

Migrado de SQLite a Supabase.
"""

from database.supabase_client import (
    SupabaseKeywordRepo,
    SupabaseManager,
    SupabaseUrlRepo,
)

__all__ = [
    "SupabaseManager",
    "SupabaseKeywordRepo",
    "SupabaseUrlRepo",
]