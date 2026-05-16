"""
utils/url_clean.py
Normalización y limpieza de URLs extraídas del scraper.

Elimina parámetros de tracking, fragmentos y redirects de Google/Facebook
para obtener la URL canónica de cada resultado.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Parámetros de tracking/analytics que no aportan información canónica
_TRACKING_PARAMS: frozenset[str] = frozenset({
    # UTM (Google Analytics)
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    # Facebook
    "fbclid", "fb_action_ids", "fb_action_types", "fb_ref", "fb_source",
    # Google
    "gclid", "gclsrc", "dclid",
    # Generales
    "_ga", "_gl", "ref", "referrer", "source", "medium",
    # Twitter
    "twclid",
    # Microsoft
    "msclkid",
})

# Patrones de URL de redirect de Google CSE
_GOOGLE_REDIRECT_PATTERN = re.compile(
    r"https?://(?:www\.)?google\.com/url\?.*?(?:url|q)=([^&]+)", re.IGNORECASE
)


def clean_url(raw_url: str) -> str:
    """
    Normaliza una URL eliminando parámetros de tracking y resolviendo redirects.

    Operaciones aplicadas en orden:
      1. Resolver redirects de Google (``google.com/url?q=...``)
      2. Eliminar el fragmento (``#section``)
      3. Eliminar parámetros de tracking conocidos
      4. Reconstruir la URL limpia

    Args:
        raw_url: URL cruda extraída del scraper (puede ser un redirect).

    Returns:
        URL normalizada y limpia.

    Example:
        >>> clean_url("https://www.facebook.com/post/123?fbclid=abc&utm_source=x")
        'https://www.facebook.com/post/123'
    """
    if not raw_url or not raw_url.startswith(("http://", "https://")):
        return raw_url

    # 1. Resolver redirect de Google
    redirect_match = _GOOGLE_REDIRECT_PATTERN.search(raw_url)
    if redirect_match:
        from urllib.parse import unquote
        raw_url = unquote(redirect_match.group(1))

    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return raw_url

    # 2. Eliminar fragmento
    # 3. Filtrar parámetros de tracking
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=False)
        clean_params = {
            key: values
            for key, values in params.items()
            if key.lower() not in _TRACKING_PARAMS
        }
        clean_query = urlencode(clean_params, doseq=True)
    else:
        clean_query = ""

    cleaned = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        clean_query,
        "",  # Sin fragmento
    ))

    # Eliminar trailing slash solo si no hay path significativo
    if cleaned.endswith("/") and cleaned.count("/") <= 3:
        cleaned = cleaned.rstrip("/")

    return cleaned