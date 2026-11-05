"""Big Integra Desktop — PySide6 + QWebEngineView + QWebChannel.

Ejecutar:
    python main.py

No usa FastAPI/Uvicorn, no abre puertos, no usa /api ni 127.0.0.1.
La interfaz HTML/CSS/JS se carga localmente y llama a Python mediante
QWebChannel. No hay control de acceso ni login: abre directamente.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from pathlib import Path

from app import __version__ as APP_VERSION

os.environ["LOCAL"] = "1"

# Modo interno de proceso independiente. Se resuelve antes de importar PySide6,
# QtWebEngine para que el child sea liviano y headless.
if len(sys.argv) >= 3 and sys.argv[1] == "--bigpdf-export-worker":
    os.environ["BIGPDF_EXPORT_WORKER"] = "1"
    from app.desktop.export_process import export_worker_entry
    raise SystemExit(export_worker_entry(sys.argv[2]))

# AppUserModelID de Windows.
# Debe setearse antes de crear QApplication para que la barra de tareas
# asocie correctamente la ventana abierta con el icono de la app.
APP_USER_MODEL_ID = f"BigIntegra.Desktop.{APP_VERSION}"


def activar_app_id_windows() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


activar_app_id_windows()

# GPU habilitada por defecto — Chromium usa compositor GPU.
# Mejora drag interno, PDF.js, canvas, zoom (will-change promueve capas GPU).
# Modo compatibilidad (pantalla negra, crash, drivers problemáticos), cualquiera de:
#   - abrir con el argumento:  BigIntegra.exe --modo-compatible
#   - PowerShell: $env:BIGPDF_DISABLE_GPU=1   /   CMD: set BIGPDF_DISABLE_GPU=1
#   - "disable_gpu": true en %LOCALAPPDATA%\BigIntegra\prefs.json (persistente)
def _gpu_compat_requested() -> bool:
    if os.environ.get("BIGPDF_DISABLE_GPU") or "--modo-compatible" in sys.argv:
        return True
    try:
        from app.engine.services.prefs_service import get_pref
        return bool(get_pref("disable_gpu", False))
    except Exception:
        return False


_chromium_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
if _gpu_compat_requested() and "--disable-gpu" not in _chromium_flags:
    _chromium_flags = ("--disable-gpu " + _chromium_flags).strip()
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _chromium_flags

import logging

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from app.desktop.bridge import BigPdfBridge
from app.engine.services.prefs_service import get_last_dir, set_last_dir

logger = logging.getLogger("bigpdf")

# Extensiones que se importan al soltar una carpeta.
_IMPORTABLE_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

# El drop nativo no llega al HTML: la clase .dragover puede quedar pegada.
_CLEAR_DRAGOVER_JS = (
    "document.querySelectorAll('#ws-root .dragover, #ws-empty-area .dragover')"
    ".forEach(e => e.classList.remove('dragover'))"
)

_DRAG_EVENT_TYPES = frozenset({
    QEvent.Type.DragEnter,
    QEvent.Type.DragMove,
    QEvent.Type.DragLeave,
    QEvent.Type.Drop,
})


def resource_path(*parts: str) -> Path:
    """Ruta compatible con ejecución normal y PyInstaller."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root.joinpath(*parts)


def get_app_icon_path() -> Path | None:
    candidates = [
        resource_path("app", "ui", "web", "assets", "brand", "bigpdf.ico"),
        resource_path("app", "ui", "web", "assets", "brand", "bigpdf-icon-v3-512x512.ico"),
        resource_path("app", "ui", "web", "assets", "brand", "bigpdf-icon-v3-512x512.png"),
        resource_path("app", "ui", "web", "assets", "brand", "bigpdf-icon-v3-192x192.png"),
        resource_path("app", "ui", "web", "assets", "brand", "bigpdf-icon-v3-32x32.png"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def get_app_icon() -> QIcon:
    icon_path = get_app_icon_path()
    if icon_path:
        return QIcon(str(icon_path))
    return QIcon()


def force_windows_taskbar_icon(widget, icon_path: Path | None = None) -> None:
    """Fuerza icono BIG/SMALL de ventana para la barra de tareas de Windows.

    En PySide6 + QtWebEngine + PyInstaller onefile, a veces setWindowIcon()
    alcanza para el .exe pero Windows mantiene icono genérico en la barra.
    WM_SETICON corrige el icono real de la ventana en runtime.
    """
    if sys.platform != "win32":
        return

    icon_path = icon_path or get_app_icon_path()
    if not icon_path or not icon_path.exists():
        return

    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return

        user32 = ctypes.windll.user32

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        ICON_SMALL2 = 2

        SM_CXICON = 11
        SM_CYICON = 12
        SM_CXSMICON = 49
        SM_CYSMICON = 50

        load_image = user32.LoadImageW
        load_image.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        load_image.restype = ctypes.c_void_p

        send_message = user32.SendMessageW
        send_message.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_void_p,
        ]
        send_message.restype = ctypes.c_void_p

        cx_big = user32.GetSystemMetrics(SM_CXICON) or 32
        cy_big = user32.GetSystemMetrics(SM_CYICON) or 32
        cx_small = user32.GetSystemMetrics(SM_CXSMICON) or 16
        cy_small = user32.GetSystemMetrics(SM_CYSMICON) or 16

        flags = LR_LOADFROMFILE
        hicon_big = load_image(None, str(icon_path), IMAGE_ICON, cx_big, cy_big, flags)
        hicon_small = load_image(None, str(icon_path), IMAGE_ICON, cx_small, cy_small, flags)

        handles = []
        if hicon_big:
            send_message(hwnd, WM_SETICON, ICON_BIG, hicon_big)
            handles.append(hicon_big)

        if hicon_small:
            send_message(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            send_message(hwnd, WM_SETICON, ICON_SMALL2, hicon_small)
            handles.append(hicon_small)

        # Mantener vivos los HICON durante toda la vida de la ventana.
        # Si se liberan antes, Windows puede volver al icono genérico.
        if handles:
            previous = getattr(widget, "_bigpdf_windows_hicons", [])
            widget._bigpdf_windows_hicons = [*previous, *handles]
    except Exception:
        pass


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_FOLDERID_DOWNLOADS = _GUID(
    0x374DE290, 0x123F, 0x4565,
    (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
)


def get_downloads_dir() -> Path:
    """Carpeta Descargas real de Windows (sigue redirecciones de OneDrive/políticas)."""
    if sys.platform == "win32":
        try:
            path_ptr = ctypes.c_wchar_p()
            hresult = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(_FOLDERID_DOWNLOADS), 0, None, ctypes.byref(path_ptr)
            )
            if hresult == 0 and path_ptr.value:
                candidate = Path(path_ptr.value)
                if not candidate.exists():
                    # Descargas redirigida (OneDrive/política) que todavía no existe:
                    # crearla, en vez de guardar en otra carpeta que el usuario no mira.
                    try:
                        candidate.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                if candidate.exists():
                    return candidate
        except Exception:
            pass
    fallback = Path.home() / "Downloads"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_unique_download_path(filename: str) -> Path:
    """Ruta única en Descargas sin sobrescribir: archivo.pdf, archivo (2).pdf..."""
    downloads = get_downloads_dir()
    name = Path(filename or "bigpdf_export.pdf").name
    base = Path(name).stem or "bigpdf_export"
    ext = Path(name).suffix or ".pdf"
    candidate = downloads / f"{base}{ext}"
    counter = 2
    while candidate.exists():
        candidate = downloads / f"{base} ({counter}){ext}"
        counter += 1
    return candidate


_NAV_ACTIONS = frozenset({
    QWebEnginePage.WebAction.Back,
    QWebEnginePage.WebAction.Forward,
    QWebEnginePage.WebAction.Reload,
    QWebEnginePage.WebAction.ReloadAndBypassCache,
    QWebEnginePage.WebAction.SavePage,
    QWebEnginePage.WebAction.ViewSource,
})


class DesktopPage(QWebEnginePage):
    """Página local de escritorio con intercepción de QFileDialog."""

    def __init__(self, profile: QWebEngineProfile, parent=None, bridge: BigPdfBridge | None = None) -> None:
        super().__init__(profile, parent)
        self.bridge = bridge

    def triggerAction(self, action, checked=False) -> None:  # noqa: N802
        if action in _NAV_ACTIONS:
            return
        super().triggerAction(action, checked)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
        return super().javaScriptConsoleMessage(level, message, line_number, source_id)

    def chooseFiles(self, mode, old_files, accepted_mime_types):  # noqa: N802
        files = self._choose_files(mode, accepted_mime_types)
        if files:
            try:
                set_last_dir(files[0])
            except Exception:
                pass
        return files

    def _choose_files(self, mode, accepted_mime_types):
        # Recordar la última carpeta usada en vez de abrir siempre en Descargas.
        initial_dir = str(get_last_dir(get_downloads_dir()))
        name_filter = self._build_name_filter(accepted_mime_types)
        parent = self.parent()

        if mode == QWebEnginePage.FileSelectionMode.FileSelectOpenMultiple:
            files, _ = QFileDialog.getOpenFileNames(parent, "Seleccionar archivos", initial_dir, name_filter)
            # El diálogo de Windows devuelve primero el último archivo clickeado:
            # se importan en el orden de la carpeta ("2" antes que "10").
            files = sorted(files, key=lambda f: _natural_key(Path(f).name))
            if self.bridge:
                self.bridge.set_last_selected_paths(files)
            return files

        if mode == QWebEnginePage.FileSelectionMode.FileSelectUploadFolder:
            folder = QFileDialog.getExistingDirectory(parent, "Seleccionar carpeta", initial_dir)
            files = [folder] if folder else []
            if self.bridge:
                self.bridge.set_last_selected_paths(files)
            return files

        file, _ = QFileDialog.getOpenFileName(parent, "Seleccionar archivo", initial_dir, name_filter)
        files = [file] if file else []
        if self.bridge:
            self.bridge.set_last_selected_paths(files)
        return files

    @staticmethod
    def _build_name_filter(accepted_mime_types) -> str:
        exts = [t for t in (accepted_mime_types or []) if str(t).startswith(".")]
        if exts:
            patterns = " ".join(f"*{e}" for e in exts)
            return f"Archivos soportados ({patterns});;Todos los archivos (*)"
        return "Todos los archivos (*)"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Big Integra")
        self.setMinimumSize(1024, 768)

        self._app_icon_path = get_app_icon_path()
        self._app_icon = QIcon(str(self._app_icon_path)) if self._app_icon_path else QIcon()
        if not self._app_icon.isNull():
            self.setWindowIcon(self._app_icon)
        force_windows_taskbar_icon(self, self._app_icon_path)

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        if not self._app_icon.isNull():
            self.view.setWindowIcon(self._app_icon)
        self.setCentralWidget(self.view)

        # Perfil off-the-record: cookies/caché solo en memoria.
        self._web_profile = QWebEngineProfile(self)
        self.bridge = BigPdfBridge(
            self,
            get_downloads_dir=get_downloads_dir,
            get_unique_download_path=get_unique_download_path,
        )
        self.page = DesktopPage(self._web_profile, self.view, bridge=self.bridge)
        self.view.setPage(self.page)

        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)

        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bigpdfBridge", self.bridge)
        self.page.setWebChannel(self.channel)

        for _ks in ("F5", "Ctrl+R", "Ctrl+Shift+R", "Alt+Left", "Alt+Right"):
            _sc = QShortcut(QKeySequence(_ks), self)
            _sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            _sc.activated.connect(lambda: None)

        # Drop nativo de archivos desde Windows Explorer.
        # Qt envía los eventos de drag al widget interno de render de QWebEngineView
        # (RenderWidgetHostViewQtDelegate), no al QWebEngineView en sí. En vez de un
        # filtro global sobre TODA la aplicación (cada evento de Qt pasaba por Python,
        # y en algunos entornos provocaba un cierre inesperado), se instala el filtro
        # en la vista y en sus widgets hijos; ChildAdded cubre el widget de render que
        # QtWebEngine recrea si el proceso de render se reinicia.
        self.view.setAcceptDrops(True)
        self._install_drag_filter(self.view)

        # Estado de drag nativo para feedback visual en JS.
        # _drag_timer: watchdog de 400 ms después de DragLeave; si no vuelve DragMove,
        # desactiva el overlay (cubre Escape, drag que sale de pantalla, etc.).
        self._native_drag_active = False
        self._drag_timer = QTimer(self)
        self._drag_timer.setSingleShot(True)
        self._drag_timer.setInterval(400)
        self._drag_timer.timeout.connect(self._on_drag_timeout_end)

        self._load_app()

    def _install_drag_filter(self, widget) -> None:
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def _on_drag_timeout_end(self) -> None:
        """Apaga el feedback visual si el drag se canceló y no volvió DragMove."""
        if self._native_drag_active:
            self._native_drag_active = False
            self.bridge.nativeDragStateChanged.emit(False)

    def _has_local_files(self, event) -> bool:
        try:
            md = event.mimeData()
            return bool(md and md.hasUrls() and any(u.isLocalFile() for u in md.urls()))
        except Exception:
            return False

    def eventFilter(self, obj, event):  # noqa: N802
        """Filtro de la vista web: intercepta drops OS-level de archivos antes de que lleguen a Chromium.

        Early-exit para eventos no-drag → overhead mínimo.
        DragEnter/DragMove: acepta + pasa a Chromium (return False) → feedback HTML.
        Drop con archivos locales: BLOQUEA Chromium (return True) → sin base64.
        DragLeave: inicia watchdog para apagar el feedback visual con delay.
        """
        etype = event.type()

        if etype == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self._install_drag_filter(child)
            return False

        # Early-exit: solo procesar eventos de drag.
        if etype not in _DRAG_EVENT_TYPES:
            return False

        # Verificar que el objeto es self.view o un descendiente suyo.
        p = obj
        is_view_family = False
        try:
            while p is not None:
                if p is self.view:
                    is_view_family = True
                    break
                p = p.parent()
        except Exception:
            pass

        if not is_view_family:
            return False

        has_files = self._has_local_files(event) if etype != QEvent.Type.DragLeave else False

        if etype == QEvent.Type.DragEnter:
            if has_files:
                event.acceptProposedAction()
                self._drag_timer.stop()  # cancelar watchdog pendiente de DragLeave previo
                if not self._native_drag_active:
                    self._native_drag_active = True
                    self.bridge.nativeDragStateChanged.emit(True)
            return False  # Chromium también ve el evento → efecto visual JS

        if etype == QEvent.Type.DragMove:
            if has_files:
                event.acceptProposedAction()
                if self._drag_timer.isActive():
                    self._drag_timer.stop()  # DragMove llegó antes del watchdog → continuar
                if not self._native_drag_active:
                    self._native_drag_active = True
                    self.bridge.nativeDragStateChanged.emit(True)
            return False

        if etype == QEvent.Type.DragLeave:
            # No apagar inmediatamente: DragLeave puede ser espurio (cursor en title bar,
            # transición entre widgets hijos). El watchdog apaga en 400 ms si no vuelve.
            if self._native_drag_active:
                self._drag_timer.start()
            return False

        if etype == QEvent.Type.Drop:
            self._drag_timer.stop()
            if self._native_drag_active:
                self._native_drag_active = False
                self.bridge.nativeDragStateChanged.emit(False)
            mime = event.mimeData()
            if mime and mime.hasUrls():
                paths, skipped = _expand_dropped_paths(
                    u.toLocalFile() for u in mime.urls() if u.isLocalFile()
                )
                if not paths and skipped:
                    event.acceptProposedAction()
                    QMessageBox.information(
                        self, "Big Integra",
                        "La carpeta no tiene archivos que Big Integra pueda abrir "
                        "(PDF, imágenes JPG/PNG, Word, Excel o PowerPoint).",
                    )
                    QTimer.singleShot(50, lambda: self.page.runJavaScript(_CLEAR_DRAGOVER_JS))
                    return True
                if paths:
                    self.bridge.set_last_selected_paths(paths, skipped_in_folders=skipped)
                    self.bridge.filesDropped.emit()
                    event.acceptProposedAction()
                    # HTML drop nunca llega → clase .dragover puede quedar pegada → limpiar
                    QTimer.singleShot(50, lambda: self.page.runJavaScript(_CLEAR_DRAGOVER_JS))
                    return True  # Bloquear Chromium: sin base64

        return False

    def _load_app(self) -> None:
        index = resource_path("app", "ui", "web", "index.html").resolve()
        self.view.load(QUrl.fromLocalFile(str(index)))

    def bring_to_front(self) -> None:
        """Llamado cuando el usuario abre Big Integra otra vez (instancia única)."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        # _force_close: cierre decidido por la app, sin preguntar.
        if not getattr(self, "_force_close", False) and self.bridge.has_active_jobs():
            answer = QMessageBox.question(
                self,
                "Big Integra",
                "Hay una tarea en curso (exportación, compresión o importación).\n\n"
                "Si cerrás ahora se cancela y se pierde lo que se estaba procesando.\n"
                "¿Cerrar igual?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        try:
            self.bridge.shutdown_background_jobs()
        except Exception:
            pass
        try:
            from app.engine.logging_config import shutdown_logging
            shutdown_logging()
        except Exception:
            pass
        try:
            from app.engine.services.cleanup_service import cleanup_session_temp_dir
            cleanup_session_temp_dir()
        except Exception:
            pass
        QTimer.singleShot(100, QApplication.instance().quit)
        event.accept()


def _natural_key(name: str) -> list:
    """Orden como el Explorador de Windows: "2.pdf" antes que "10.pdf"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


# Archivos de sistema que Windows/Office dejan en las carpetas: no se cuentan como
# "no admitidos" (el usuario no los ve en el Explorador).
_HIDDEN_FILE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def _is_system_file(child: Path) -> bool:
    name = child.name.lower()
    return name in _HIDDEN_FILE_NAMES or name.startswith(("~$", "."))


def _expand_dropped_paths(raw_paths) -> tuple[list[str], int]:
    """Archivos soltados: los archivos se importan tal cual y las carpetas se
    expanden a sus archivos importables (primer nivel, en orden natural como el
    Explorador). Devuelve también cuántos archivos de las carpetas se omitieron
    por formato no admitido, para avisarlo."""
    paths: list[str] = []
    skipped = 0
    for raw in raw_paths:
        p = Path(raw)
        try:
            if p.is_file():
                paths.append(str(p))
            elif p.is_dir():
                for child in sorted(p.iterdir(), key=lambda c: _natural_key(c.name)):
                    if not child.is_file() or _is_system_file(child):
                        continue
                    if child.suffix.lower() in _IMPORTABLE_SUFFIXES:
                        paths.append(str(child))
                    else:
                        skipped += 1
        except OSError:
            continue
    return paths, skipped


def _single_instance_key() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"BigIntegra-Desktop-{user}"


def _notify_running_instance(key: str) -> bool:
    """Si ya hay una ventana de Big Integra abierta, le pide que pase al frente."""
    socket = QLocalSocket()
    socket.connectToServer(key)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"activate")
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def _close_bootloader_splash() -> None:
    """Cierra la imagen que mostró el .exe mientras se descomprimía (PyInstaller
    Splash). Se llama recién cuando ya está visible la ventana de Qt, así no queda
    ningún momento con la pantalla vacía. Fuera del .exe no existe y no hace nada."""
    try:
        import pyi_splash  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        pyi_splash.close()
    except Exception:
        pass


class _StartupSplash(QWidget):
    """Ventana de inicio con el logo mientras se arma la ventana principal
    (QtWebEngine tarda unos segundos en arrancar)."""

    _BRAND = "#2A6FE0"

    def __init__(self, icon: QIcon) -> None:
        super().__init__(
            None,
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setWindowTitle("Big Integra")
        if not icon.isNull():
            self.setWindowIcon(icon)
        # Fondo transparente + tarjeta con bordes redondeados y sombra.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 26)
        card = QFrame(self)
        card.setObjectName("splashCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(15, 23, 42, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 30, 36, 26)
        layout.setSpacing(10)

        logo = QLabel(card)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = resource_path("app", "ui", "web", "assets", "brand", "bigpdf-icon-v3-192x192.png")
        pixmap = QPixmap(str(logo_path)) if logo_path.exists() else icon.pixmap(192, 192)
        if not pixmap.isNull():
            dpr = self.devicePixelRatioF() or 1.0
            pixmap = pixmap.scaled(
                int(76 * dpr), int(76 * dpr),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap.setDevicePixelRatio(dpr)
            logo.setPixmap(pixmap)

        title = QLabel("Big Integra", card)
        title.setObjectName("splashTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version = QLabel(f"Versión {APP_VERSION}", card)
        version.setObjectName("splashVersion")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label = QLabel("Abriendo…", card)
        self.label.setObjectName("splashStatus")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        progress = QProgressBar(card)
        progress.setRange(0, 0)  # indeterminada
        progress.setTextVisible(False)
        progress.setFixedHeight(4)

        layout.addWidget(logo)
        layout.addSpacing(2)
        layout.addWidget(title)
        layout.addWidget(version)
        layout.addSpacing(10)
        layout.addWidget(self.label)
        layout.addWidget(progress)

        self.setStyleSheet(f"""
            #splashCard {{
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 16px;
            }}
            QLabel {{ font-family: 'Segoe UI', sans-serif; background: transparent; }}
            #splashTitle {{ font-size: 22px; font-weight: 700; color: #0f172a; letter-spacing: 0.5px; }}
            #splashVersion {{ font-size: 11px; color: #94a3b8; }}
            #splashStatus {{ font-size: 13px; color: #475569; }}
            QProgressBar {{ background: #e8effc; border: none; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {self._BRAND}; border-radius: 2px; }}
        """)
        self.setFixedSize(380, 300)
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        # Centrada explícitamente: queda en el mismo lugar que la imagen que mostró
        # el .exe al descomprimirse, así el paso de una a otra no "salta".
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.center().y() - self.height() // 2)


def main() -> int:
    activar_app_id_windows()

    try:
        from app.engine.logging_config import setup_logging
        setup_logging()
        logger.info("Big Integra %s iniciando", APP_VERSION)
    except Exception:
        pass

    # ── HiDPI ───────────────────────────────────────────────────────────────
    # Escalado de Qt activo (por defecto en Qt 6) para que Chromium reciba el DPR
    # real del sistema: en pantallas al 125-150 % la interfaz se ve del tamaño
    # correcto y los canvas nítidos. Antes se forzaba QT_ENABLE_HIGHDPI_SCALING=0,
    # que hacía lo contrario de lo que decía este comentario.
    # Para volver al comportamiento anterior: BIGPDF_DISABLE_HIDPI=1
    if os.environ.get("BIGPDF_DISABLE_HIDPI"):
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
        os.environ.setdefault("QT_SCALE_FACTOR", "1")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Big Integra")
    app.setApplicationDisplayName("Big Integra")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("BigIntegra")
    app.setDesktopFileName(APP_USER_MODEL_ID)
    # La salida es explícita (closeEvent): limpia la sesión antes de salir.
    app.setQuitOnLastWindowClosed(False)

    # Instancia única: abrir Big Integra otra vez trae al frente la ventana existente.
    instance_key = _single_instance_key()
    if _notify_running_instance(instance_key):
        return 0
    QLocalServer.removeServer(instance_key)  # socket viejo de un cierre inesperado
    instance_server = QLocalServer()
    instance_server.listen(instance_key)

    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    splash = _StartupSplash(app_icon)
    splash.show()
    app.processEvents()
    _close_bootloader_splash()

    state: dict = {"window": None}

    def _on_instance_connection() -> None:
        while instance_server.hasPendingConnections():
            conn = instance_server.nextPendingConnection()
            conn.disconnected.connect(conn.deleteLater)
        window = state["window"]
        if window is not None and window.isVisible():
            window.bring_to_front()
        elif splash.isVisible():
            splash.raise_()
            splash.activateWindow()

    instance_server.newConnection.connect(_on_instance_connection)

    def _show_main_window() -> None:
        window = MainWindow()
        if not app_icon.isNull():
            window.setWindowIcon(app_icon)
        state["window"] = window
        icon_path = get_app_icon_path()
        force_windows_taskbar_icon(window, icon_path)
        window.showMaximized()
        splash.close()
        # Reaplicar después de mostrar la ventana: Windows recién termina de asociar
        # el botón de la barra de tareas cuando el HWND ya está visible.
        QTimer.singleShot(250, lambda: force_windows_taskbar_icon(window, icon_path))

    # singleShot(0): primero se pinta la ventana de inicio y recién después se arma
    # la principal (crearla bloquea el hilo de Qt un momento).
    QTimer.singleShot(0, _show_main_window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
