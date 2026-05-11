from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit
import re

# Parámetros de tracking a eliminar
_TRACKING: frozenset[str] = frozenset({
    "__cft__", "__tn__", "mibextid", "app", "rdid", "ref", "__eep__", "sk",
})

def clean_url(url: str, remove_tracking: bool = True) -> str:
    """Limpia y normaliza una URL, decodificando caracteres escapados.

    Args:
        url: URL cruda, posiblemente con secuencias %XX.
        remove_tracking: Si es True, elimina parámetros de tracking
            típicos de Facebook (por defecto True).

    Returns:
        URL limpia, lista para ser almacenada.

    Raises:
        ValueError: Si la URL no puede ser parseada.

    Examples:
        >>> clean_url("https://www.facebook.com/CiberCubaNoticias/photos/"
        ...           "%EF%B8%8F-otro-apag%C3%B3n-golpea-a-varios-municipios-"
        ...           "habaneroscibercuba-te-lo-explica-un-nue/1439644548208065/")
        'https://www.facebook.com/CiberCubaNoticias/photos/️-otro-apagón-golpea-a-varios-municipios-habaneroscibercuba-te-lo-explica-un-nue/1439644548208065/'

        >>> clean_url("https://m.facebook.com/story.php?story_fbid=123&__tn__=HH-R")
        'https://www.facebook.com/story.php?story_fbid=123'
    """
    if not url:
        raise ValueError("URL vacía")

    # 1. Limpieza previa de comillas y caracteres extraños heredada del scraper
    url = url.strip().strip('"').strip("\u201c\u201d'").rstrip(")*")
    # Convertir a https y unificar host
    url = re.sub(r"^http://", "https://", url, flags=re.I)
    url = re.sub(r"^https://m\.facebook", "https://www.facebook", url, flags=re.I)
    url = re.sub(r"^https://fb\.com/", "https://www.facebook.com/", url, flags=re.I)

    # 2. Parsear la URL en componentes
    parsed = urlsplit(url)
    scheme = parsed.scheme
    netloc = parsed.netloc
    path = parsed.path or "/"
    query = parsed.query
    fragment = parsed.fragment  # normalmente vacío, pero lo respetamos

    # 3. Decodificar ruta segmento a segmento para no romper separadores
    segments = path.split("/")
    decoded_segments = []
    for seg in segments:
        # unquote convierte %XX a caracteres Unicode (UTF-8)
        decoded_segments.append(unquote(seg))
    decoded_path = "/".join(decoded_segments)

    # 4. Decodificar y limpiar query string
    decoded_query = ""
    if query and remove_tracking:
        qs = parse_qs(query, keep_blank_values=True)
        clean_qs = {
            unquote(k): [unquote(v) for v in vs]
            for k, vs in qs.items()
            if not any(k.startswith(t.rstrip("_")) for t in _TRACKING)
        }
        decoded_query = urlencode(clean_qs, doseq=True, safe="/?&=:")  # safe para mantener estructura
    elif query:
        # Si no eliminamos tracking, simplemente decodificamos re-encodeando
        qs = parse_qs(query, keep_blank_values=True)
        decoded_qs = {unquote(k): [unquote(v) for v in vs] for k, vs in qs.items()}
        decoded_query = urlencode(decoded_qs, doseq=True, safe="/?&=:")
    # else: decoded_query queda ""

    # 5. Fragmento (raro en FB, pero por si acaso)
    decoded_fragment = unquote(fragment) if fragment else ""

    # 6. Reconstruir URL
    clean = urlunsplit((scheme, netloc, decoded_path, decoded_query, decoded_fragment))
    return clean