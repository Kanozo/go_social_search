"""
utils/fb_url_validator.py
Validador de URLs de Facebook basado en expresiones regulares de clasificación.

Este módulo expone únicamente la función `is_valid_fb_url()` para filtrar
resultados de scraping. No incluye lógica de parsing ni enrutamiento a parsers.

Las regex se extraen del código de clasificación original, compilándose
una vez al importar el módulo para máxima eficiencia en bucles de scraping.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from typing import Final

# ---------------------------------------------------------------------------
# Parámetros de tracking a eliminar durante la normalización.
# ---------------------------------------------------------------------------
_TRACKING_PARAMS: Final[frozenset[str]] = frozenset({
    "__cft__", "__tn__", "mibextid", "app", "rdid", "ref", "__eep__", "sk",
})

# ---------------------------------------------------------------------------
# Segmentos de ruta reservados por Facebook (no son vanity URLs de usuario).
# ---------------------------------------------------------------------------
_RESERVED_SEGMENTS: Final[frozenset[str]] = frozenset({
    "groups", "pages", "events", "marketplace", "watch", "gaming",
    "photos", "videos", "reels", "stories", "live", "notifications",
    "messages", "friends", "bookmarks", "saved", "search", "explore",
    "help", "settings", "privacy", "about", "ads", "business",
    "profile.php", "permalink.php", "login", "logout", "share", "hashtag",
    "photo.php", "photo", "video.php",
})

# ---------------------------------------------------------------------------
# Patrones regex compilados para validación de URLs de Facebook.
# Se compilan una vez al importar el módulo (no en cada llamada).
# ---------------------------------------------------------------------------
_FB_URL_PATTERNS: Final[list[re.Pattern[str]]] = [
    # Regla 1: Hashtag
    re.compile(r"^/hashtag/", re.I),
    
    # Regla 2: Reel directo (/reel/<id>)
    re.compile(r"^/reel/\d+"),
    
    # Regla 3: Vídeo nativo (path o query)
    re.compile(r"/videos/(?:[^/]+/)*\d+", re.I),
    re.compile(r"(?:^|&)v=\d+"),
    
    # Regla 4: Foto individual (photo.php o /photo/ con fbid o /photos/<id>)
    re.compile(r"/photo(?:s|\.php)?/?", re.I),
    re.compile(r"(?:^|&)fbid=\d+"),
    re.compile(r"/photos?/\d+", re.I),
    
    # Regla 5: Post en grupo (/groups/<id>/posts|permalink/)
    re.compile(r"^/groups/[^/]+/(?:posts|permalink)/", re.I),
    
    # Regla 6: Grupo raíz (/groups/)
    re.compile(r"^/groups/", re.I),
    
    # Regla 7: Post directo (/<x>/posts/<id>)
    re.compile(r"/posts/(?:\d+|pfbid[A-Za-z0-9]+)", re.I),
    
    # Regla 8: Perfil numérico (profile.php?id=<id>)
    re.compile(r"/profile\.php", re.I),
    re.compile(r"(?:^|&)id=\d+"),
    
    # Regla 9: permalink.php o story.php con story_fbid o id numérico
    re.compile(r"/(?:story|permalink)\.php", re.I),
    re.compile(r"story_fbid="),
    
    # Reglas 10–13: Shortlinks share/*
    re.compile(r"^/share/r/", re.I),
    re.compile(r"^/share/p/", re.I),
    re.compile(r"^/share/v/", re.I),
    re.compile(r"^/share/", re.I),
    
    # Regla 14: Perfil /people/<nombre>/<id_numérico>/
    re.compile(r"^/people/[^/]+/\d+/?$", re.I),
    
    # Regla 15: Vanity URL (/<slug>) — validación adicional contra _RESERVED
    re.compile(r"^/([A-Za-z0-9._-]+)/?$"),
]


def _normalize_fb_url(url: str) -> tuple[str, str]:
    """
    Normaliza una URL de Facebook eliminando parámetros de tracking.
    
    Args:
        url: URL cruda extraída del scraping.
        
    Returns:
        Tupla ``(path, query_string)`` normalizada.
    """
    parsed = urlparse(url)
    
    # Filtrar parámetros de tracking
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    filtered_params = {
        k: v for k, v in query_params.items() 
        if k not in _TRACKING_PARAMS
    }
    
    # Reconstruir query string (orden estable para consistencia)
    normalized_query = urlencode(filtered_params, doseq=True, safe="/")
    
    # Path sin trailing slash (excepto raíz)
    path = parsed.path.rstrip("/") or "/"
    
    return path, normalized_query


def is_valid_fb_url(url: str) -> bool:
    """
    Valida si una URL de Facebook coincide con al menos un patrón conocido.
    
    Esta función usa *exclusivamente* las expresiones regulares del código
    de clasificación original, sin lógica de enrutamiento a parsers.
    
    Args:
        url: URL a validar (ej: ``"https://www.facebook.com/reel/123"``).
        
    Returns:
        ``True`` si la URL coincide con al menos un patrón válido,
        ``False`` en caso contrario.
        
    Examples:
        >>> is_valid_fb_url("https://www.facebook.com/reel/816043001524221")
        True
        >>> is_valid_fb_url("https://www.facebook.com/photo.php?fbid=861685686929188")
        True
        >>> is_valid_fb_url("https://www.facebook.com/groups/12345")
        True
        >>> is_valid_fb_url("https://www.facebook.com/zurdobo7")
        True  # Vanity URL válida
        >>> is_valid_fb_url("https://www.facebook.com/login")
        False  # Segmento reservado
        >>> is_valid_fb_url("https://example.com/not-facebook")
        False  # Dominio inválido
    """
    # 1. Validar dominio Facebook (soporta variantes comunes)
    parsed = urlparse(url)
    if not any(
        domain in parsed.netloc.lower()
        for domain in ("facebook.com", "fb.com", "m.facebook.com", "www.facebook.com")
    ):
        return False
    
    # 2. Normalizar URL (eliminar tracking, estandarizar path/query)
    path, query = _normalize_fb_url(url)
    
    # 3. Validar contra patrones regex
    for pattern in _FB_URL_PATTERNS:
        # Patrones que requieren contexto de query
        if pattern in (_FB_URL_PATTERNS[3], _FB_URL_PATTERNS[5], _FB_URL_PATTERNS[9], _FB_URL_PATTERNS[11]):
            if pattern.search(query):
                return True
        # Patrones de path
        elif pattern.search(path):
            # Validación especial para Regla 15 (Vanity URL)
            if pattern == _FB_URL_PATTERNS[-1]:
                match = pattern.match(path)
                if match:
                    slug = match.group(1).lower()
                    if slug not in _RESERVED_SEGMENTS and not slug.isdigit():
                        return True
            else:
                return True
    
    return False