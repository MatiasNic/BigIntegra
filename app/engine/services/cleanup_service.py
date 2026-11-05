import logging
import shutil
import time

from app.config import STORAGE_DIR, release_session_lock

logger = logging.getLogger("bigpdf")


def cleanup_session_temp_dir() -> dict:
    """Borra por completo la carpeta temporal de la sesión actual.

    Reintenta unas pocas veces porque Windows puede demorar la liberación de
    archivos recién cerrados. Los restos que no puedan borrarse se purgan al
    iniciar la próxima sesión.
    """
    if not STORAGE_DIR.exists():
        return {"removed": False}

    release_session_lock()

    for _attempt in range(5):
        shutil.rmtree(STORAGE_DIR, ignore_errors=True)
        if not STORAGE_DIR.exists():
            return {"removed": True}
        time.sleep(0.15)

    logger.warning(
        "cleanup_session_temp_dir: no se pudo borrar %s; "
        "los restos se limpiarán en el próximo inicio.",
        STORAGE_DIR,
    )
    return {"removed": False}
