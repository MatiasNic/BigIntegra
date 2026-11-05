import fitz
import pytest


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """PDF mínimo de una página con texto, generado en memoria."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((20, 20), "BigPDF test page")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def sample_pdf_path(tmp_path, sample_pdf_bytes):
    path = tmp_path / "sample.pdf"
    path.write_bytes(sample_pdf_bytes)
    return path


def _s15(value):
    import struct
    return struct.pack(">i", round(value * 65536))


def gray_icc_profile() -> bytes:
    """Perfil ICC v2 mínimo de escala de grises (gamma 2.2), válido para MuPDF/lcms."""
    import struct
    desc_txt = b"Gray 2.2\x00"
    desc = b"desc" + b"\0" * 4 + struct.pack(">I", len(desc_txt)) + desc_txt
    desc += struct.pack(">II", 0, 0) + struct.pack(">HB", 0, 0) + b"\0" * 67
    wtpt = b"XYZ " + b"\0" * 4 + _s15(0.9642) + _s15(1.0) + _s15(0.8249)
    ktrc = b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", 0x0233) + b"\0\0"
    cprt = b"text" + b"\0" * 4 + b"No copyright\x00"
    tags = [(b"desc", desc), (b"wtpt", wtpt), (b"kTRC", ktrc), (b"cprt", cprt)]
    offset = 128 + 4 + 12 * len(tags)
    table, data = b"", b""
    for sig, body in tags:
        body += b"\0" * (-len(body) % 4)
        table += sig + struct.pack(">II", offset + len(data), len(body))
        data += body
    header = struct.pack(">I", offset + len(data)) + b"\0" * 4 + struct.pack(">I", 0x02100000)
    header += b"mntr" + b"GRAY" + b"XYZ " + b"\0" * 12 + b"acsp" + b"\0" * 24
    header += struct.pack(">I", 0) + _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"\0" * 48
    return header + struct.pack(">I", len(tags)) + table + data


@pytest.fixture
def gray_icc_scan_pdf(tmp_path):
    """Escaneo en grises (JPEG de 1 canal) con perfil ICC de 1 canal: el caso que
    rewrite_images() de PyMuPDF dejaba desarmado al comprimir."""
    # Con "ruido" de papel, como un escaneo real: si la imagen es demasiado limpia,
    # rewrite_images() no la recodifica (no gana tamaño) y el error no aparece.
    import random
    rng = random.Random(7)
    width, height = 700, 980
    data = bytearray(width * height)
    for y in range(height):
        row = y * width
        is_line = (y % 45) < 6
        for x in range(width):
            data[row + x] = 40 if is_line and 60 <= x < 640 else 245 - rng.randrange(25)
    pix = fitz.Pixmap(fitz.csGRAY, width, height, bytes(data), False)
    jpeg = pix.tobytes("jpg", jpg_quality=92)

    doc = fitz.open()
    pages = []
    for _ in range(3):
        page = doc.new_page(width=595, height=842)
        pages.append(page.insert_image(page.rect, stream=jpeg))
    icc = doc.get_new_xref()
    doc.update_object(icc, "<< /N 1 /Alternate /DeviceGray >>")
    doc.update_stream(icc, gray_icc_profile())
    colorspace = doc.get_new_xref()
    doc.update_object(colorspace, f"[ /ICCBased {icc} 0 R ]")
    for xref in set(pages):
        doc.xref_set_key(xref, "ColorSpace", f"{colorspace} 0 R")
    path = tmp_path / "escaneo_gris_icc.pdf"
    doc.save(str(path))
    doc.close()
    return path
