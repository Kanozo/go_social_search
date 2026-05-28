from pathlib import Path
from typing import List, Union
import logging

logger = logging.getLogger(__name__)


def load_sources_from_file(file_path: Union[str, Path]) -> List[str]:
    """
    Lee un fichero de texto y devuelve una lista de elementos procesados.
    
    Cada línea del fichero se procesa de la siguiente manera:
    - Si la línea comienza con 'http', se mantiene tal cual (URL)
    - Si la línea NO comienza con 'http', se encierra en comillas dobles
    
    Args:
        file_path: Ruta del fichero de texto a leer.
        
    Returns:
        List[str]: Lista con los elementos procesados según el formato especificado.
        
    Raises:
        FileNotFoundError: Si el fichero especificado no existe.
        PermissionError: Si no hay permisos para leer el fichero.
        UnicodeDecodeError: Si el fichero no puede ser decodificado como UTF-8.
    """
    path = Path(file_path)
    result: List[str] = []
    
    try:
        with path.open(mode='r', encoding='utf-8') as file:
            for line in file:
                # Eliminar espacios en blanco y saltos de línea
                element = line.strip()
                
                # Saltar líneas vacías
                if not element:
                    continue
                
                # Verificar si es una URL (comienza con http)
                if element.startswith('http'):
                    result.append(element)
                elif element.startswith('//'):
                    continue
                else:
                    # Encerrar en comillas dobles si no es URL
                    result.append(f'"{element}"')
                    
    except FileNotFoundError:
        logger.error(f"Fichero no encontrado: {path}")
        raise
    except PermissionError:
        logger.error(f"Permiso denegado para leer: {path}")
        raise
    except UnicodeDecodeError as e:
        logger.error(f"Error de codificación al leer {path}: {e}")
        raise
    
    logger.info(f"Se cargaron {len(result)} elementos desde {path}")
    return result