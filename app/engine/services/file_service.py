from pathlib import Path

import fitz

from app.exceptions import BigPDFError


def check_pdf_limits(path: Path, max_pages: int) -> int:
    """Valida contraseña y cantidad máxima de páginas de un PDF."""
    doc = fitz.open(str(path))
    try:
        if doc.needs_pass:
            raise BigPDFError(400, "El PDF está protegido con contraseña y no puede procesarse.")
        pages = doc.page_count
        if pages == 0:
            raise BigPDFError(400, "El PDF no tiene páginas.")
    finally:
        doc.close()

    if pages > max_pages:
        raise BigPDFError(
            400,
            f"El PDF tiene {pages} páginas, supera el límite de {max_pages}.",
        )
    return pages
