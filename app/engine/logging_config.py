import logging
import sys
from logging.handlers import RotatingFileHandler

from app.config import LOGS_DIR


def setup_logging(filename: str = "bigpdf.log") -> logging.Logger:
    """Activa el log en APP_DATA_DIR/logs (persistente, rotativo 5 MB x 3)."""
    _log_file = LOGS_DIR / filename
    logger = logging.getLogger("bigpdf")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # En el .exe sin consola sys.stderr es None: solo agregar consola si existe.
    if sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


def shutdown_logging() -> None:
    """Cierra y desconecta los handlers del logger 'bigpdf' (libera el handle de
    bigpdf.log). Debe llamarse antes de borrar la carpeta temporal de sesión —
    si el archivo de log queda abierto, Windows no permite borrar su carpeta."""
    logger = logging.getLogger("bigpdf")
    for handler in list(logger.handlers):
        try:
            handler.close()
        finally:
            logger.removeHandler(handler)
