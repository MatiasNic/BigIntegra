import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from app.engine.cancellation import OperationCancelled, raise_if_cancelled

logger = logging.getLogger("bigpdf")


def _resource_roots() -> list[Path]:
    """Raíces de búsqueda para Ghostscript portátil.

    En modo PyInstaller onedir devuelve _MEIPASS (archivos bundled) y el
    directorio del .exe (tools/ externo al lado del ejecutable).
    En modo desarrollo devuelve la raíz del proyecto.
    """
    if getattr(sys, "frozen", False):
        roots: list[Path] = []
        if hasattr(sys, "_MEIPASS"):
            roots.append(Path(sys._MEIPASS))
        roots.append(Path(sys.executable).parent)
        return roots
    return [Path(__file__).resolve().parent.parent.parent.parent]

_gs_cache: Path | None = None
_gs_checked = False

# "high" = "Comprimir PDF" (UI). Uses 130 DPI + JPEGQ 74 + threshold 1.0.
# threshold 1.0 forces downsample on ALL images >= 130 DPI.
# Prev was pure /ebook (no threshold override → threshold defaults to 1.5 = only images
# above 225 DPI downsampled) → virtually nothing changed → caused 2% reduction.
# "high_stronger" = 2nd pass (120 DPI / JPEGQ 70), only when primary gives < 12%.
_LEVEL_PARAMS: dict[str, dict] = {
    "low": {
        "base_preset": "/screen",
        "color_dpi": 120,
        "gray_dpi": 120,
        "mono_dpi": 200,
        "jpeg_q": 82,
        "label": "screen_120dpi_q82",
    },
    "medium": {
        "base_preset": "/screen",
        "color_dpi": 96,
        "gray_dpi": 96,
        "mono_dpi": 150,
        "jpeg_q": 68,
        "label": "screen_96dpi_q68",
    },
    "high": {
        "base_preset": "/ebook",
        "color_dpi": 130,
        "gray_dpi": 130,
        "mono_dpi": 300,
        "jpeg_q": 74,
        "label": "balanced_130dpi_q74",
    },
    "high_stronger": {
        "base_preset": "/ebook",
        "color_dpi": 120,
        "gray_dpi": 120,
        "mono_dpi": 300,
        "jpeg_q": 70,
        "label": "balanced_120dpi_q70",
    },
}


def find_ghostscript() -> Path | None:
    """Locate gswin64c.exe / gs binary. Returns Path or None."""
    env_val = os.environ.get("GHOSTSCRIPT_PATH", "").strip()
    if env_val:
        p = Path(env_val)
        if p.exists():
            return p

    for root in _resource_roots():
        for rel in (
            Path("tools") / "ghostscript" / "gswin64c.exe",
            Path("tools") / "gswin64c.exe",
        ):
            c = root / rel
            if c.exists():
                return c

    win_patterns = [
        r"C:\Program Files\gs\*\bin\gswin64c.exe",
        r"C:\Program Files (x86)\gs\*\bin\gswin64c.exe",
        r"C:\Program Files\gs\*\bin\gswin32c.exe",
        r"C:\Program Files (x86)\gs\*\bin\gswin32c.exe",
    ]
    for pattern in win_patterns:
        matches = glob.glob(pattern)
        if matches:
            return Path(sorted(matches)[-1])

    # PATH check via shutil.which (fast, no subprocess spawn)
    for cmd in ["gswin64c", "gswin32c", "gs"]:
        found = shutil.which(cmd)
        if found:
            return Path(found)

    # subprocess fallback (catches PATH entries shutil.which might miss on some Windows configs)
    for cmd in ["gswin64c", "gswin32c", "gs"]:
        try:
            r = subprocess.run(
                [cmd, "--version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if r.returncode == 0:
                return Path(cmd)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    return None


def is_ghostscript_available() -> bool:
    return _get_gs() is not None


def _get_gs() -> Path | None:
    global _gs_cache, _gs_checked
    if not _gs_checked:
        _gs_cache = find_ghostscript()
        _gs_checked = True
        if _gs_cache:
            logger.info(f"Ghostscript encontrado: {_gs_cache}")
        else:
            logger.warning("Ghostscript no encontrado - se usará fallback PyMuPDF")
    return _gs_cache


def _build_args(gs: Path, params: dict, input_path: Path, output_path: Path) -> list[str]:
    """Build GS subprocess args. All profiles use base_preset + explicit DPI/JPEG overrides.
    threshold=1.0 forces downsample on any image at or above target DPI (not just >1.5x)."""
    base = [
        str(gs),
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        # Sin esto Ghostscript gira por su cuenta las páginas según la dirección
        # del texto (y la verificación descartaba el resultado).
        "-dAutoRotatePages=/None",
    ]

    color_dpi = params["color_dpi"]
    gray_dpi  = params["gray_dpi"]
    mono_dpi  = params["mono_dpi"]
    jpeg_q    = params["jpeg_q"]
    extra = [
        f"-dPDFSETTINGS={params['base_preset']}",
        "-dDownsampleColorImages=true",
        f"-dColorImageResolution={color_dpi}",
        "-dColorImageDownsampleType=/Bicubic",
        "-dColorImageDownsampleThreshold=1.0",
        "-dAutoFilterColorImages=false",
        "-dColorImageFilter=/DCTEncode",
        f"-dJPEGQ={jpeg_q}",
        "-dDownsampleGrayImages=true",
        f"-dGrayImageResolution={gray_dpi}",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleThreshold=1.0",
        "-dAutoFilterGrayImages=false",
        "-dGrayImageFilter=/DCTEncode",
        "-dDownsampleMonoImages=true",
        f"-dMonoImageResolution={mono_dpi}",
        "-dMonoImageDownsampleType=/Subsample",
        "-dMonoImageDownsampleThreshold=1.0",
    ]

    return base + extra + [f"-sOutputFile={output_path}", str(input_path)]


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Finaliza Ghostscript y descendientes sin dejar procesos huérfanos."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass


def compress_with_ghostscript(
    input_path: Path,
    output_path: Path,
    level: str,
    *,
    cancel_check: Callable[[], bool] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    timeout_seconds: int = 120,
) -> dict:
    """Comprime con Ghostscript, con cancelación y señal de actividad.

    El proceso exportador completo vive fuera de la interfaz. Aun así, esta
    función supervisa Ghostscript para terminar también su árbol al cancelar.
    """
    gs = _get_gs()
    if not gs:
        return {"success": False, "reason": "not_found"}

    raise_if_cancelled(cancel_check)
    params = _LEVEL_PARAMS.get(level, _LEVEL_PARAMS["medium"])
    label = params["label"]
    args = _build_args(gs, params, input_path, output_path)
    logger.debug("gs level=%r method=%r args=%r", level, label, args)

    # stdout a DEVNULL y stderr a un archivo temporal: si se usan PIPEs y no se
    # leen mientras GS corre, un PDF dañado que emite muchos avisos llena el buffer
    # (~4 KB en Windows) y GS queda bloqueado hasta el timeout.
    stderr_file = tempfile.TemporaryFile()
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": stderr_file,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(args, **kwargs)
    except (FileNotFoundError, OSError) as exc:
        stderr_file.close()
        logger.error("gs level=%r method=%r OS_ERROR=%s", level, label, exc)
        return {"success": False, "reason": str(exc)}

    started = time.monotonic()
    last_activity = 0.0
    try:
        try:
            while process.poll() is None:
                if cancel_check is not None and cancel_check():
                    _terminate_process_tree(process)
                    raise OperationCancelled("Procesamiento cancelado por el usuario.")

                elapsed = time.monotonic() - started
                if elapsed >= timeout_seconds:
                    _terminate_process_tree(process)
                    logger.error("gs level=%r method=%r TIMEOUT after %ss", level, label, timeout_seconds)
                    return {"success": False, "reason": "timeout"}

                if progress_fn and elapsed - last_activity >= 2.0:
                    last_activity = elapsed
                    try:
                        progress_fn(f"Ghostscript continúa procesando… {int(elapsed)} s")
                    except Exception:
                        pass
                time.sleep(0.2)
            process.wait(timeout=5)
        except OperationCancelled:
            raise
        except Exception as exc:
            _terminate_process_tree(process)
            logger.error("gs supervision error: %s", exc)
            return {"success": False, "reason": str(exc)}

        try:
            stderr_file.seek(0, os.SEEK_END)
            size = stderr_file.tell()
            stderr_file.seek(max(0, size - 2000))
            stderr = stderr_file.read()
        except OSError:
            stderr = b""
    finally:
        stderr_file.close()

    input_bytes = input_path.stat().st_size
    output_exists = output_path.exists()
    output_bytes = output_path.stat().st_size if output_exists else None
    stderr_tail = (stderr or b"").decode("utf-8", errors="ignore")[-500:]

    if process.returncode != 0:
        logger.warning(
            "gs level=%r method=%r rc=%s stderr=%r",
            level, label, process.returncode, stderr_tail,
        )
        return {"success": False, "reason": f"rc={process.returncode}"}

    logger.debug(
        "gs level=%r method=%r ok input_bytes=%s output_bytes=%s",
        level, label, input_bytes, output_bytes,
    )
    if not output_exists or output_bytes == 0:
        return {"success": False, "reason": "empty_output"}
    return {"success": True, "method": label}
