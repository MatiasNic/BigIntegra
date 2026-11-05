import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import fitz

from app.config import MAX_FILE_SIZE, UPLOADS_DIR, WORKING_DIR, ensure_storage_dirs
from app.engine.services.repair_service import rebuild_if_damaged, repair_mismatched_gray_jpegs

logger = logging.getLogger("bigpdf")

# 1 instancia de soffice a la vez: perfiles concurrentes corrompen la conversión.
_libreoffice_lock = threading.Lock()

LIBREOFFICE_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    r"/usr/bin/soffice",
    r"/usr/local/bin/soffice",
]

ACCEPTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".doc": "office", ".docx": "office",
    ".xls": "office", ".xlsx": "office",
    ".ppt": "office", ".pptx": "office",
}

LIBREOFFICE_TIMEOUT_SECONDS = 120

# La especificación PDF tolera bytes basura (BOM, CRLF, cabeceras de mail) antes
# de "%PDF" dentro del primer KB; MuPDF los abre sin problema.
_PDF_HEADER_WINDOW = 1024


def _looks_like_pdf(head: bytes) -> bool:
    return b"%PDF" in head[:_PDF_HEADER_WINDOW]


IMG_FILETYPES = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png"}

# Tamaño de página de una imagen: se respeta el que dan sus DPI, salvo que salga
# más grande que un A3 (fotos de celular a 72 dpi daban páginas de 1,4 m) o
# deformado (DPI distintos en ancho y alto): ahí se ajusta a una hoja A4.
_A4_PT = (595.0, 842.0)
_MAX_IMAGE_PAGE_PT = 1191.0  # lado mayor de un A3


def find_libreoffice() -> str | None:
    lo = shutil.which("soffice")
    if lo:
        return lo
    for p in LIBREOFFICE_PATHS:
        if Path(p).exists():
            return p
    return None


def detect_type(filename: str) -> str | None:
    return ACCEPTED_EXTENSIONS.get(Path(filename).suffix.lower())


def convert_image_to_pdf(content: bytes, filename: str) -> tuple[str, Path]:
    ensure_storage_dirs()
    ext = Path(filename).suffix.lower()
    filetype = IMG_FILETYPES.get(ext)
    if not filetype:
        raise ValueError(f"Formato de imagen no admitido: {filename}")

    # El tipo real lo dice el contenido, no la extensión: WhatsApp y otros guardan
    # fotos JPG con extensión .png (y al revés). Antes se rechazaban.
    if content.startswith(b"\xff\xd8"):
        filetype = "jpg"
    elif content.startswith(b"\x89PNG"):
        filetype = "png"
    else:
        raise ValueError(f"{filename} no es una imagen válida.")

    doc_id = str(uuid.uuid4())
    try:
        img_doc = fitz.open(stream=content, filetype=filetype)
        pdfbytes = img_doc.convert_to_pdf()
        img_doc.close()
        pdfbytes = _fit_image_page(pdfbytes)
    except Exception as e:
        logger.warning(f"fitz convert_to_pdf falló para {filename}: {e}")
        raise ValueError(f"No se pudo convertir '{filename}' como imagen.")

    ensure_storage_dirs()
    out_path = UPLOADS_DIR / f"{doc_id}.pdf"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pdfbytes)
    return doc_id, out_path


def _fit_image_page(pdfbytes: bytes) -> bytes:
    """Ajusta a A4 la página de una imagen si sus DPI la dejan enorme o deformada."""
    with fitz.open("pdf", pdfbytes) as doc:
        page = doc[0]
        width, height = page.rect.width, page.rect.height
        infos = page.get_image_info()
        if not infos or width <= 0 or height <= 0:
            return pdfbytes
        info = infos[0]
        px_w, px_h = info["width"], info["height"]
        a, b = info["transform"][0], info["transform"][1]
        if abs(b) > abs(a):  # imagen girada por EXIF: se ve con ancho y alto cambiados
            px_w, px_h = px_h, px_w
        if px_w <= 0 or px_h <= 0:
            return pdfbytes
        deformed = abs((width / height) / (px_w / px_h) - 1) > 0.02
        if not deformed and max(width, height) <= _MAX_IMAGE_PAGE_PT:
            return pdfbytes
        short, long_ = _A4_PT
        box_w, box_h = (long_, short) if px_w > px_h else (short, long_)
        scale = min(box_w / px_w, box_h / px_h)
        out = fitz.open()
        new_page = out.new_page(width=px_w * scale, height=px_h * scale)
        new_page.show_pdf_page(new_page.rect, doc, 0, keep_proportion=False)
        data = out.tobytes(garbage=1, deflate=True)
        out.close()
        return data


def convert_office_to_pdf(content: bytes, filename: str) -> tuple[str, Path]:
    ensure_storage_dirs()
    tmp_id = str(uuid.uuid4())
    tmp_input = UPLOADS_DIR / f"_tmp_{tmp_id}{Path(filename).suffix}"
    tmp_input.parent.mkdir(parents=True, exist_ok=True)
    tmp_input.write_bytes(content)
    return convert_office_to_pdf_from_path(tmp_input, filename)


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Mata soffice y sus hijos: en Windows matar soffice.exe puede dejar soffice.bin vivo."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=8, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _run_soffice(args: list[str], filename: str) -> subprocess.CompletedProcess:
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(args, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=LIBREOFFICE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=5)
        except Exception:
            pass
        logger.error("soffice TIMEOUT convirtiendo %s", filename)
        raise RuntimeError(
            f"La conversión de {filename} tardó demasiado y se canceló. "
            "Probá abrirlo en Office, guardarlo como PDF e importarlo."
        ) from None
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def convert_office_to_pdf_from_path(tmp_input: Path, filename: str) -> tuple[str, Path]:
    """Como convert_office_to_pdf, pero recibe un archivo ya en disco (subida en streaming)
    en vez de bytes en memoria. Usa perfil de LibreOffice descartable por job y serializa
    las conversiones (1 soffice a la vez) para evitar perfiles compartidos corruptos."""
    ensure_storage_dirs()
    lo = find_libreoffice()
    if not lo:
        tmp_input.unlink(missing_ok=True)
        raise RuntimeError(
            "Para convertir Word, Excel o PowerPoint necesitás LibreOffice instalado. "
            "Descargalo en https://www.libreoffice.org/"
        )

    # Perfil descartable dentro de la sesión: si la app muere, se purga con ella.
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix="bigpdf_lo_profile_", dir=str(WORKING_DIR)))
    try:
        with _libreoffice_lock:
            result = _run_soffice(
                [
                    lo, "--headless", "--norestore",
                    # as_uri() codifica espacios y acentos (C:/Users/José Pérez/...);
                    # sin codificar, soffice se colgaba hasta el timeout.
                    f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                    "--convert-to", "pdf", "--outdir", str(UPLOADS_DIR), str(tmp_input),
                ],
                filename,
            )
    finally:
        tmp_input.unlink(missing_ok=True)
        shutil.rmtree(profile_dir, ignore_errors=True)

    if result.returncode != 0:
        logger.error(
            "soffice falló convirtiendo %s: rc=%s stderr=%r",
            filename, result.returncode, (result.stderr or "")[-500:],
        )
        raise RuntimeError(f"Error al convertir {filename}.")

    raw_out = UPLOADS_DIR / f"{tmp_input.stem}.pdf"
    if not raw_out.exists():
        logger.error(f"soffice no generó salida esperada para {filename} (buscado {raw_out.name})")
        raise RuntimeError(f"La conversión de {filename} no generó un PDF.")

    doc_id = str(uuid.uuid4())
    ensure_storage_dirs()
    final = UPLOADS_DIR / f"{doc_id}.pdf"
    final.parent.mkdir(parents=True, exist_ok=True)
    raw_out.rename(final)
    return doc_id, final


def ensure_pdf(content: bytes, filename: str) -> tuple[str, Path, str]:
    """Convert any supported file to PDF. Returns (doc_id, pdf_path, original_type)."""
    ensure_storage_dirs()
    file_type = detect_type(filename)

    if file_type is None:
        raise ValueError(f"Formato no admitido: {filename}")

    if len(content) > MAX_FILE_SIZE:
        raise ValueError(f"{filename} supera el límite de {MAX_FILE_SIZE // (1024 ** 3)} GB.")

    if file_type == "pdf":
        if not _looks_like_pdf(content[:_PDF_HEADER_WINDOW]):
            raise ValueError(f"{filename} no es un PDF válido.")
        doc_id = str(uuid.uuid4())
        ensure_storage_dirs()
        out = UPLOADS_DIR / f"{doc_id}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)
        repair_mismatched_gray_jpegs(out)
        rebuild_if_damaged(out)
        return doc_id, out, "pdf"

    elif file_type == "image":
        doc_id, out = convert_image_to_pdf(content, filename)
        return doc_id, out, "image"

    elif file_type == "office":
        doc_id, out = convert_office_to_pdf(content, filename)
        return doc_id, out, "office"

    raise ValueError(f"Tipo no soportado: {filename}")


def ensure_pdf_from_temp(
    tmp_path: Path, filename: str, size: int, info: dict | None = None,
) -> tuple[str, Path, str]:
    """Como ensure_pdf, pero recibe un archivo ya guardado en disco (subida en streaming
    vía file_service.stream_to_temp) en vez de bytes completos en memoria. Para PDFs, solo
    se mueve el archivo (nunca se carga el contenido en RAM).

    ``info`` (opcional) recibe ``repaired=True`` si el PDF venía dañado y se reconstruyó.
    """
    ensure_storage_dirs()
    file_type = detect_type(filename)

    if file_type is None:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"Formato no admitido: {filename}")

    if size > MAX_FILE_SIZE:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"{filename} supera el límite de {MAX_FILE_SIZE // (1024 ** 3)} GB.")

    if file_type == "pdf":
        with open(tmp_path, "rb") as f:
            head = f.read(_PDF_HEADER_WINDOW)
        if not _looks_like_pdf(head):
            tmp_path.unlink(missing_ok=True)
            raise ValueError(f"{filename} no es un PDF válido.")
        doc_id = str(uuid.uuid4())
        ensure_storage_dirs()
        out = UPLOADS_DIR / f"{doc_id}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.replace(out)
        # PDFs dañados por la compresión de versiones anteriores se corrigen solos.
        repair_mismatched_gray_jpegs(out)
        if rebuild_if_damaged(out) and info is not None:
            info["repaired"] = True
        return doc_id, out, "pdf"

    elif file_type == "image":
        content = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)
        doc_id, out = convert_image_to_pdf(content, filename)
        return doc_id, out, "image"

    elif file_type == "office":
        doc_id, out = convert_office_to_pdf_from_path(tmp_path, filename)
        return doc_id, out, "office"

    tmp_path.unlink(missing_ok=True)
    raise ValueError(f"Tipo no soportado: {filename}")


def get_page_count(pdf_path: Path) -> int:
    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count
