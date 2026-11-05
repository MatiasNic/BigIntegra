from pathlib import Path

import pymupdf
import pytest

FORMULARIOS = Path(__file__).resolve().parents[1] / "fixtures" / "formularios_2024"


def make_pdf(path: Path, pages: list[str]) -> Path:
    """PDF digital con una página por texto (líneas separadas por \\n)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=595, height=842)
        y = 60
        for line in text.splitlines():
            page.insert_text((40, y), line, fontsize=10)
            y += 14
    doc.save(path)
    doc.close()
    return path


def rasterize(src: Path, dst: Path, dpi: int = 150) -> Path:
    """Simula un escaneo: cada página pasa a ser una imagen sin capa de texto."""
    out = pymupdf.open()
    with pymupdf.open(src) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            img = pymupdf.open("png", pix.tobytes("png"))
            out.insert_pdf(pymupdf.open("pdf", img.convert_to_pdf()))
    out.save(dst)
    out.close()
    return dst


@pytest.fixture
def formularios() -> Path:
    return FORMULARIOS
