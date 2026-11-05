import pytest

from app.engine.services.conversion_service import detect_type, get_page_count


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("informe.pdf", "pdf"),
        ("foto.JPG", "image"),
        ("planilla.xlsx", "office"),
        ("archivo.exe", None),
    ],
)
def test_detect_type(filename, expected):
    assert detect_type(filename) == expected


def test_get_page_count(sample_pdf_path):
    assert get_page_count(sample_pdf_path) == 1


def test_ensure_pdf_from_temp_accepts_leading_garbage(tmp_path, sample_pdf_bytes):
    from app.engine.services.conversion_service import ensure_pdf_from_temp

    tmp = tmp_path / "bom.pdf"
    data = b"\xef\xbb\xbf\r\n" + sample_pdf_bytes
    tmp.write_bytes(data)
    doc_id, out, kind = ensure_pdf_from_temp(tmp, "bom.pdf", len(data))
    assert kind == "pdf"
    assert out.exists()
    out.unlink()


def _damaged_like_old_versions(src, dest):
    """Reproduce el daño de BigPDF <= 1.4.4: recomprime sin normalizar los grises ICC."""
    import fitz
    doc = fitz.open(str(src))
    doc.rewrite_images(
        dpi_threshold=None, dpi_target=0, quality=65, lossy=True, lossless=True,
        bitonal=False, color=True, gray=True, set_to_gray=False,
    )
    doc.save(str(dest), garbage=4, deflate=True)
    doc.close()


def _max_render_diff(a_path, b_path):
    import fitz
    a, b = fitz.open(str(a_path)), fitz.open(str(b_path))
    try:
        worst = 0.0
        for pa, pb in zip(a, b):
            sa = pa.get_pixmap(dpi=40, colorspace=fitz.csGRAY).samples
            sb = pb.get_pixmap(dpi=40, colorspace=fitz.csGRAY).samples
            worst = max(worst, sum(abs(x - y) for x, y in zip(sa, sb)) / len(sa))
        return worst
    finally:
        a.close()
        b.close()


def test_import_repairs_pdf_damaged_by_old_compression(gray_icc_scan_pdf, tmp_path):
    from app.engine.services.conversion_service import ensure_pdf_from_temp

    damaged = tmp_path / "danado.pdf"
    _damaged_like_old_versions(gray_icc_scan_pdf, damaged)
    assert _max_render_diff(gray_icc_scan_pdf, damaged) > 8  # se ve desarmado

    staged = tmp_path / "staged.pdf"
    staged.write_bytes(damaged.read_bytes())
    _, out, _ = ensure_pdf_from_temp(staged, "escaneo.pdf", staged.stat().st_size)
    try:
        assert _max_render_diff(gray_icc_scan_pdf, out) < 3  # reparado al importar
    finally:
        out.unlink(missing_ok=True)


def test_import_leaves_healthy_pdf_untouched(gray_icc_scan_pdf, tmp_path):
    from app.engine.services.conversion_service import ensure_pdf_from_temp

    original = gray_icc_scan_pdf.read_bytes()
    staged = tmp_path / "sano.pdf"
    staged.write_bytes(original)
    _, out, _ = ensure_pdf_from_temp(staged, "sano.pdf", len(original))
    try:
        assert out.read_bytes() == original
    finally:
        out.unlink(missing_ok=True)


def _jpeg(width, height, dpi):
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pix.clear_with(200)
    pix.set_dpi(*dpi)
    return pix.tobytes("jpg")


def test_photo_with_low_dpi_fits_a4_instead_of_huge_page():
    import fitz

    from app.engine.services.conversion_service import convert_image_to_pdf

    # Foto de celular de 72 dpi: antes daba una página de 1,4 x 1 metro.
    _, out = convert_image_to_pdf(_jpeg(4032, 3024, (72, 72)), "foto.jpg")
    with fitz.open(str(out)) as doc:
        rect = doc[0].rect
        assert max(rect.width, rect.height) <= 842.5
        assert abs(rect.width / rect.height - 4032 / 3024) < 0.01
    out.unlink()


def test_image_with_normal_dpi_keeps_its_size():
    import fitz

    from app.engine.services.conversion_service import convert_image_to_pdf

    _, out = convert_image_to_pdf(_jpeg(1240, 1754, (150, 150)), "hoja.jpg")
    with fitz.open(str(out)) as doc:
        assert round(doc[0].rect.width) == 595 and round(doc[0].rect.height) == 842
    out.unlink()


def test_jpg_saved_with_png_extension_is_accepted():
    from app.engine.services.conversion_service import convert_image_to_pdf

    # WhatsApp y otros guardan fotos JPG como ".png": antes se rechazaban.
    _, out = convert_image_to_pdf(_jpeg(100, 100, (96, 96)), "foto.png")
    assert out.exists()
    out.unlink()


def test_truncated_pdf_is_rebuilt_on_import(tmp_path):
    import fitz

    from app.engine.services.conversion_service import ensure_pdf_from_temp

    doc = fitz.open()
    for i in range(5):
        doc.new_page().insert_text((72, 72), f"Hoja {i + 1}")
    data = doc.tobytes()
    doc.close()
    tmp = tmp_path / "cortado.pdf"
    tmp.write_bytes(data[: int(len(data) * 0.8)])
    info = {}
    _, out, _ = ensure_pdf_from_temp(tmp, "cortado.pdf", tmp.stat().st_size, info=info)
    assert info.get("repaired") is True
    with fitz.open(str(out)) as rebuilt:
        assert not rebuilt.is_repaired
        assert rebuilt.page_count >= 1
    out.unlink()
