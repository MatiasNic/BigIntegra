import fitz

from app.engine.services.compress_service import compress_pdf_file


def test_compress_pdf_file_level_none_copies_original(sample_pdf_path, tmp_path):
    dest = tmp_path / "out.pdf"
    metrics = compress_pdf_file(sample_pdf_path, dest, level="none")

    assert dest.exists()
    assert metrics["engine"] == "none"
    assert metrics["saved_bytes"] == 0
    assert dest.read_bytes() == sample_pdf_path.read_bytes()


def test_compress_pdf_file_auto_preserves_page_count(sample_pdf_path, tmp_path):
    dest = tmp_path / "out_auto.pdf"
    compress_pdf_file(sample_pdf_path, dest, level="auto")

    doc = fitz.open(str(dest))
    try:
        assert doc.page_count == 1
    finally:
        doc.close()


def _scan_like_pdf(path, pages=3):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 900, 1200), False)
    import random
    random.seed(1)
    pix.set_rect(pix.irect, (200, 200, 200))
    for _ in range(3000):
        x, y = random.randrange(880), random.randrange(1180)
        pix.set_rect(fitz.IRect(x, y, x + 20, y + 20), (random.randrange(255),) * 3)
    png = pix.tobytes("png")
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=png)
    doc.save(str(path))
    doc.close()


def test_compress_cleans_temp_files_when_cancelled(tmp_path):
    from app.engine.cancellation import OperationCancelled

    src = tmp_path / "scan.pdf"
    _scan_like_pdf(src)
    out_dir = tmp_path / "out"
    calls = {"n": 0}

    def cancel_after_some_checks():
        calls["n"] += 1
        return calls["n"] > 6

    import pytest
    with pytest.raises(OperationCancelled):
        compress_pdf_file(src, out_dir / "res.pdf", level="auto", cancel_check=cancel_after_some_checks)
    assert not list(out_dir.glob("*.tmp.pdf"))


def test_xref_stream_length_resolves_indirect_length(tmp_path):
    from app.engine.services.compress_service import _xref_stream_length

    doc = fitz.open()
    length_xref = doc.get_new_xref()
    doc.update_object(length_xref, "3000")
    stream_xref = doc.get_new_xref()
    doc.update_object(stream_xref, f"<< /Length {length_xref} 0 R >>")
    assert _xref_stream_length(doc, stream_xref) == 3000
    doc.close()


def _mean_render_diff(path_a, path_b):
    a, b = fitz.open(str(path_a)), fitz.open(str(path_b))
    try:
        diffs = []
        for pa, pb in zip(a, b):
            sa = pa.get_pixmap(dpi=40, colorspace=fitz.csGRAY).samples
            sb = pb.get_pixmap(dpi=40, colorspace=fitz.csGRAY).samples
            diffs.append(sum(abs(x - y) for x, y in zip(sa, sb)) / len(sa))
        return max(diffs)
    finally:
        a.close()
        b.close()


def test_gray_icc_scan_is_not_garbled(gray_icc_scan_pdf, tmp_path):
    """Regresión: escaneos en grises con perfil ICC salían desarmados al comprimir
    (imagen ampliada, corrida y repetida) y el resultado se daba por bueno."""
    dest = tmp_path / "comprimido.pdf"
    compress_pdf_file(gray_icc_scan_pdf, dest, level="auto")
    assert _mean_render_diff(gray_icc_scan_pdf, dest) < 3.0


def test_visual_check_rejects_damaged_result(gray_icc_scan_pdf, tmp_path):
    from app.engine.services.compress_service import _check_visual_integrity

    assert _check_visual_integrity(gray_icc_scan_pdf, gray_icc_scan_pdf) is None

    damaged = tmp_path / "danado.pdf"
    doc = fitz.open(str(gray_icc_scan_pdf))
    doc[1].draw_rect(fitz.Rect(0, 0, 595, 420), fill=(0, 0, 0))  # media página tapada
    doc.save(str(damaged))
    doc.close()
    problem = _check_visual_integrity(gray_icc_scan_pdf, damaged)
    assert problem and "página 2" in problem


def test_damaged_candidate_is_discarded(gray_icc_scan_pdf, tmp_path, monkeypatch):
    """Si un motor devuelve un PDF dañado, se descarta y se entrega el original
    intacto en vez de un archivo roto."""
    import app.engine.services.compress_service as cs

    monkeypatch.setattr(cs, "_normalize_gray_icc_images", lambda doc: 0)  # vuelve el bug
    dest = tmp_path / "res.pdf"
    metrics = cs.compress_pdf_file(gray_icc_scan_pdf, dest, level="auto")
    assert metrics["method"] != "pymupdf_rewrite_scan_q65"
    assert _mean_render_diff(gray_icc_scan_pdf, dest) < 3.0


def _form_pdf(path):
    """PDF con un campo de formulario completado y una imagen grande (para que
    haya algo que comprimir)."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 900, 900), False)
    for y in range(0, 900, 3):
        for x in range(0, 900, 7):
            pix.set_pixel(x, y, ((x * 7) % 255, (y * 3) % 255, (x + y) % 255))
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(50, 300, 550, 800), pixmap=pix)
    widget = fitz.Widget()
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "nombre"
    widget.field_value = "Juan Perez"
    widget.rect = fitz.Rect(72, 100, 300, 130)
    page.add_widget(widget)
    doc.save(str(path))
    doc.close()


def test_compression_keeps_form_fields(tmp_path):
    src = tmp_path / "form.pdf"
    _form_pdf(src)
    out = tmp_path / "out.pdf"
    compress_pdf_file(src, out, "high")
    with fitz.open(str(out)) as doc:
        values = {w.field_name: w.field_value for w in doc[0].widgets()}
    assert values == {"nombre": "Juan Perez"}


def test_structure_check_rejects_lost_form_fields(tmp_path):
    from app.engine.services.compress_service import _check_visual_integrity

    src = tmp_path / "form.pdf"
    _form_pdf(src)
    flattened = tmp_path / "flat.pdf"
    with fitz.open(str(src)) as doc:
        page = doc[0]
        widget = page.first_widget
        page.delete_widget(widget)
        doc.save(str(flattened))
    assert "formularios" in (_check_visual_integrity(src, flattened) or "")


def test_color_check_rejects_changed_hue(tmp_path):
    from app.engine.services.compress_service import _check_visual_integrity

    def page_with(color, path):
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(50, 50, 550, 750), color=None, fill=color)
        doc.save(str(path))
        doc.close()

    # Mismo brillo, otro tono (como una tinta plana mal convertida): el control
    # en grises no lo ve, el de color sí.
    page_with((0.8, 0.3, 0.3), tmp_path / "a.pdf")
    page_with((0.3, 0.55, 0.45), tmp_path / "b.pdf")
    problem = _check_visual_integrity(tmp_path / "a.pdf", tmp_path / "b.pdf")
    assert problem and "colores" in problem
