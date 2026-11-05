import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path


def _resource_root() -> Path:
    """Root de recursos compatible con fuente y PyInstaller (assets estáticos, no storage)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


_SESSION_LOCK_NAME = ".session.lock"
# Handle abierto del lock de la sesión actual: se mantiene durante toda la vida
# del proceso. El SO lo libera solo si la app se cierra o crashea.
_session_lock_handle = None


def _try_lock(path: Path):
    """Intenta tomar un lock exclusivo no bloqueante. Devuelve el handle o None."""
    try:
        handle = open(path, "a+b")  # noqa: SIM115 — debe quedar abierto
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def release_session_lock() -> None:
    """Suelta el lock de la sesión actual. Llamar justo antes de borrarla: en
    Windows un archivo abierto impide borrar su carpeta."""
    global _session_lock_handle
    handle, _session_lock_handle = _session_lock_handle, None
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def _session_in_use(session_dir: Path) -> bool:
    lock_path = session_dir / _SESSION_LOCK_NAME
    if not lock_path.exists():
        return False
    handle = _try_lock(lock_path)
    if handle is None:
        return True
    handle.close()
    return False


def _purge_stale_sessions(temp_root: Path, current_session_dir: Path) -> None:
    """Borra restos de sesiones previas en %TEMP%/BigIntegra (cierres inesperados).

    Respeta las sesiones cuyo lock sigue tomado por otra instancia abierta: antes
    una segunda ventana borraba los archivos de la primera.
    """
    if not temp_root.exists():
        return
    for entry in temp_root.iterdir():
        if entry == current_session_dir:
            continue
        try:
            if entry.is_dir():
                if _session_in_use(entry):
                    continue
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except Exception:
            pass


def _app_data_dir() -> Path:
    """Carpeta PERSISTENTE de la app (sobrevive a reinicios), fuera de %TEMP%.

    Guarda caché de acceso, logs y preferencias. Nunca documentos del usuario.
    """
    override = os.environ.get("BIGPDF_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "BigIntegra"
        return Path.home() / "AppData" / "Local" / "BigIntegra"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "BigIntegra"


BASE_DIR = _resource_root()

# El proceso independiente de exportación debe trabajar sobre la misma sesión
# temporal que la interfaz principal. El parent pasa BIGPDF_SESSION_DIR al child.
_EXTERNAL_SESSION_DIR = os.environ.get("BIGPDF_SESSION_DIR", "").strip()
_IS_EXPORT_WORKER = os.environ.get("BIGPDF_EXPORT_WORKER", "") == "1"

# BIG PDF no conserva documentos del usuario dentro del proyecto ni en una carpeta
# persistente: cada sesión trabaja sobre una carpeta temporal propia bajo %TEMP%,
# que se purga de restos de sesiones anteriores al iniciar y se borra por completo
# al cerrar la ventana (ver cleanup_session_temp_dir() y MainWindow.closeEvent).
if _EXTERNAL_SESSION_DIR:
    STORAGE_DIR = Path(_EXTERNAL_SESSION_DIR).expanduser().resolve()
    _BIGPDF_TEMP_ROOT = STORAGE_DIR.parent
    SESSION_ID = STORAGE_DIR.name
else:
    _BIGPDF_TEMP_ROOT = Path(tempfile.gettempdir()) / "BigIntegra"
    SESSION_ID = uuid.uuid4().hex
    STORAGE_DIR = _BIGPDF_TEMP_ROOT / SESSION_ID

_BIGPDF_TEMP_ROOT.mkdir(parents=True, exist_ok=True)

UPLOADS_DIR = STORAGE_DIR / "uploads"
WORKING_DIR = STORAGE_DIR / "working"
EXPORTS_DIR = STORAGE_DIR / "exports"
# Persistentes: los logs deben sobrevivir al cierre para poder diagnosticar
# problemas reportados por usuarios.
APP_DATA_DIR = _app_data_dir()
LOGS_DIR = APP_DATA_DIR / "logs"

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB — desktop, no web upload constraints
MAX_CONCURRENT_PDF_OPS = 1  # admisión: notebook dual-core, evita que se amontonen pedidos pesados
PDF_SLOT_WAIT_SECONDS = 600  # las operaciones pesadas se encolan: esperan a que termine la anterior

MAX_PAGES_PER_PDF = 1000
MAX_FILES_PER_OPERATION = 30


def ensure_storage_dirs() -> None:
    """Garantiza que las carpetas temporales de la sesión existan.

    En modo PyInstaller onefile, o si Windows/antivirus/limpieza de %TEMP% borra
    carpetas mientras la app sigue abierta, una carga posterior puede fallar con
    WinError 3. Esta función es idempotente y barata; se llama antes de cada
    operación de import/export/conversión.
    """
    _BIGPDF_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    for d in [UPLOADS_DIR, WORKING_DIR, EXPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


ensure_storage_dirs()

if not _IS_EXPORT_WORKER:
    _session_lock_handle = _try_lock(STORAGE_DIR / _SESSION_LOCK_NAME)

# Purga de sesiones anteriores en hilo daemon — puede tardar si las sesiones son grandes.
# Los directorios de la sesión actual ya están creados arriba, disponibles inmediatamente.
if not _IS_EXPORT_WORKER:
    threading.Thread(
        target=_purge_stale_sessions,
        args=(_BIGPDF_TEMP_ROOT, STORAGE_DIR),
        daemon=True,
    ).start()
