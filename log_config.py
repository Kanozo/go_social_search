"""
Sistema de logging profesional con múltiples handlers y formateo avanzado.
Soporta logging a archivos y consola con rotación automática.
"""
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timezone
from config.settings import settings
from typing import Optional


class ColoredFormatter(logging.Formatter):
    """Formateador con colores para salida en consola"""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m'
    }
    
    def format(self, record):
        """Formatea el registro con colores"""
        if not hasattr(logging, 'disable') or logging.disable(logging.NOTSET):
            levelname = record.levelname
            if levelname in self.COLORS:
                record.levelname = f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logger(
    name: str,
    log_file: Optional[str] = None,
    level: str = "INFO",
    console: bool = True,
    file_rotation: bool = True
) -> logging.Logger:
    """
    Configura un logger con handlers para archivo y consola.
    
    Args:
        name: Nombre del logger
        log_file: Ruta del archivo de log (opcional)
        level: Nivel de logging
        console: Habilitar output en consola
        file_rotation: Habilitar rotación de archivos
    
    Returns:
        Logger configurado
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))
    
    # Evitar duplicar handlers si ya existen
    if logger.handlers:
        return logger
    
    # Formato detallado para archivos
    detailed_format = (
        '%(asctime)s - %(name)s - %(levelname)s - '
        '[%(filename)s:%(lineno)d] - %(funcName)s() - %(message)s'
    )
    detailed_formatter = logging.Formatter(
        detailed_format,
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Handler para consola
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, level.upper()))
        console_formatter = ColoredFormatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    # Handler para archivo con rotación
    if log_file:
        log_path = Path(settings.LOG_DIR) / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        if file_rotation:
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=settings.LOG_MAX_BYTES,
                backupCount=settings.LOG_BACKUP_COUNT,
                encoding='utf-8'
            )
        else:
            file_handler = logging.FileHandler(log_path, encoding='utf-8')
        
        file_handler.setLevel(getattr(logging, level.upper()))
        file_handler.setFormatter(detailed_formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Obtiene o crea un logger con el nombre especificado.
    
    Args:
        name: Nombre del logger (usualmente __name__)
    
    Returns:
        Logger configurado
    """
    return logging.getLogger(name)


# Loggers principales
logger_app = setup_logger(
    "app",
    log_file=settings.LOG_FILE,
    level=settings.LOG_LEVEL,
    console=True,
    file_rotation=True
)

logger_audit = setup_logger(
    "audit",
    log_file=settings.LOG_AUDIT_FILE,
    level="INFO",
    console=False,
    file_rotation=True
)

logger_database = setup_logger(
    "database",
    log_file="database.log",
    level=settings.LOG_LEVEL,
    console=True,
    file_rotation=True
)


class AuditLogger:
    """
    Logger especializado para eventos de auditoría.
    Registra cambios, accesos y eventos de seguridad.
    """
    
    @staticmethod
    def log_event(
        event_type: str,
        user_id: Optional[str],
        action: str,
        resource: str,
        details: Optional[dict] = None,
        status: str = "success",
        ip_address: Optional[str] = None
    ):
        """
        Registra un evento de auditoría.
        
        Args:
            event_type: Tipo de evento (LOGIN, LOGOUT, CREATE, UPDATE, DELETE, etc.)
            user_id: ID del usuario que realiza la acción
            action: Acción realizada
            resource: Recurso afectado
            details: Detalles adicionales del evento
            status: Estado del evento (success, failed, warning)
            ip_address: Dirección IP del cliente
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc),
            "event_type": event_type,
            "user_id": user_id or "anonymous",
            "action": action,
            "resource": resource,
            "status": status,
            "ip_address": ip_address,
            "details": details or {}
        }
        
        logger_audit.info(f"{event_type}|{user_id}|{action}|{resource}|{status}|{log_entry['details']}")
        
        return log_entry