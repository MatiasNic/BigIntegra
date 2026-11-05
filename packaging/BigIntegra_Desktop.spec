# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

# Este .spec vive en packaging/; la raíz del proyecto es el directorio padre.
# build_exe.bat invoca PyInstaller desde la raíz del proyecto.
ROOT = (Path(globals().get("SPECPATH", Path.cwd())) / "..").resolve()
WEB = ROOT / "app" / "ui" / "web"
ICON = WEB / "assets" / "brand" / "bigpdf.ico"
TOOLS = ROOT / "tools"

# Versión única: se lee de app/__init__.py (no hace falta editar este archivo
# en cada release).
_version_ns: dict = {}
exec((ROOT / "app" / "__init__.py").read_text(encoding="utf-8"), _version_ns)
APP_VERSION = _version_ns["__version__"]

datas = [
    (str(WEB), "app/ui/web"),
    # Catálogo de Integra (tipos de documento y requisitos por prestación).
    (str(ROOT / "app" / "engine" / "integra" / "catalog"), "app/engine/integra/catalog"),
]
# tools/ puede traer Ghostscript portátil (tools/ghostscript/...). tools/dev son
# scripts de desarrollo y no van en el ejecutable.
if TOOLS.exists():
    for child in TOOLS.iterdir():
        if child.name == "dev":
            continue
        if child.is_dir() and any(p.is_file() for p in child.rglob("*")):
            datas.append((str(child), f"tools/{child.name}"))
        elif child.is_file():
            datas.append((str(child), "tools"))

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebChannel",
        "PySide6.QtPrintSupport",
        "fitz",
        "pymupdf",
        "app.engine.integra.text_service",
        "app.engine.integra.classifier",
        "app.engine.integra.analyzer",
        "app.engine.integra.service",
        "app.desktop.bridge",
        "app.desktop.workers",
        "app.desktop.export_process",
        "app.desktop.export_process_manager",
        "app.engine.cancellation",
        "app.engine.services.compress_service",
        "app.engine.services.ghostscript_service",
        "app.engine.services.workspace_export_service",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Módulos de la stdlib que la app no usa: achican el ejecutable y lo que se
    # descomprime en cada arranque.
    excludes=[
        "tkinter", "_tkinter", "lib2to3", "pydoc_data", "test", "idlelib",
        # Dependencias OPCIONALES de PyMuPDF/pydantic que la app no usa. Si están
        # instaladas en la PC que compila, PyInstaller las metía igual (~60 MB:
        # numpy, pandas, lxml, Pillow, scipy...).
        "numpy", "pandas", "PIL", "lxml", "scipy", "matplotlib", "IPython",
        "jedi", "pytest", "setuptools", "pkg_resources", "fontTools", "cv2",
        # Módulos de Qt que no usa una app de widgets + WebEngine.
        "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets", "PySide6.QtQuickWidgets", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtPositioning",
        "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtDesigner",
    ],
    noarchive=False,
    optimize=0,
)

# QtWebEngine trae ~50 idiomas de interfaz de Chromium (varios MB); la app está
# en español, alcanza con es/en.
_KEEP_LOCALES = ("es", "es-419", "en-US", "en-GB")


def _keep_data(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    if "qtwebengine_locales/" in dest:
        return Path(dest).stem in _KEEP_LOCALES
    return True


# Plugins QML (Qt Quick 3D, controles, etc.) y DevTools de Chromium: la app usa
# QWebEngineView de widgets, no Qt Quick. Menos MB = arranque más rápido, porque
# el .exe onefile descomprime todo en cada apertura.
_DROP_PREFIXES = ("PySide6/Qt/qml/",)
_DROP_NAMES = ("qtwebengine_devtools_resources.pak",)
_DROP_BINARY_MARKERS = ("Quick3D", "Qt6Charts", "Qt6DataVisualization", "Qt6Multimedia")


def _keep_entry(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    if dest.startswith(_DROP_PREFIXES) or Path(dest).name in _DROP_NAMES:
        return False
    return _keep_data(entry)


def _keep_binary(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    if dest.startswith(_DROP_PREFIXES):
        return False
    return not any(marker in Path(dest).name for marker in _DROP_BINARY_MARKERS)


a.datas = [entry for entry in a.datas if _keep_entry(entry)]
a.binaries = [entry for entry in a.binaries if _keep_binary(entry)]

pyz = PYZ(a.pure)


def _version_info():
    """VERSIONINFO de Windows (Propiedades → Detalles). Un .exe sin metadatos es
    un patrón típico de falsos positivos de antivirus y del aviso de SmartScreen."""
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )
    except Exception:
        return None
    try:
        return _build_version_info(
            FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
        )
    except Exception as exc:  # no frenar el build por los metadatos
        print(f"AVISO: VERSIONINFO no generado ({exc}); se compila sin metadatos.")
        return None


def _build_version_info(
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
):
    parts = [int(x) for x in (APP_VERSION.split(".") + ["0", "0", "0"])[:4] if x.isdigit()]
    parts = (parts + [0, 0, 0, 0])[:4]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(parts), prodvers=tuple(parts)),
        kids=[
            StringFileInfo([StringTable("0C0A04B0", [
                StringStruct("CompanyName", "BigIntegra"),
                StringStruct("FileDescription", "Big Integra Desktop"),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("InternalName", "BigIntegra"),
                StringStruct("OriginalFilename", f"BigIntegra_{APP_VERSION}.exe"),
                StringStruct("ProductName", "Big Integra"),
                StringStruct("ProductVersion", APP_VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0C0A, 1200])]),
        ],
    )


# Imagen inicial que muestra el propio .exe MIENTRAS SE DESCOMPRIME (varios segundos
# en PCs con antivirus), antes de que arranque Python. main.py la cierra apenas
# aparece la ventana "Verificando acceso…" de Qt. Usa Tcl/Tk solo en el arranque;
# si la PC que compila no tiene tkinter, se compila igual sin esta imagen.
_splash_items = []
try:
    splash = Splash(
        str(ROOT / "packaging" / "splash.png"),
        binaries=a.binaries,
        datas=a.datas,
        text_pos=None,          # sin texto dinámico (mostraría nombres de archivos)
        minify_script=True,
        always_on_top=True,
    )
    _splash_items = [splash, splash.binaries]
except (Exception, SystemExit) as exc:  # noqa: BLE001
    print(f"AVISO: imagen de inicio desactivada ({exc}); se compila sin ella.")

exe = EXE(
    pyz,
    a.scripts,
    *_splash_items,
    a.binaries,
    a.datas,
    [],
    name=f"BigIntegra_{APP_VERSION}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ICON)],
    version=_version_info(),
)
