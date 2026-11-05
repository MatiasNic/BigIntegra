import fitz
import pytest

from app.engine.models.document_model import WorkspacePageConfig
from app.engine.services import workspace_export_service as wes


@pytest.fixture
def session_dirs(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    exports = tmp_path / "exports"
    uploads.mkdir()
    exports.mkdir()
    monkeypatch.setattr(wes, "UPLOADS_DIR", uploads)
    monkeypatch.setattr(wes, "EXPORTS_DIR", exports)
    return uploads, exports


def _shared_image_pdf(path, pages=40):
    """PDF donde todas las páginas comparten la misma imagen (como un logo/fondo)."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 400), False)
    pix.clear_with(90)
    png = pix.tobytes("png")
    doc = fitz.open()
    xref = 0
    for _ in range(pages):
        page = doc.new_page()
        xref = page.insert_image(page.rect, stream=png, xref=xref)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def _pages(doc_id, nums, rotation=0):
    return [
        WorkspacePageConfig(page_id=f"p{n}", source_document_id=doc_id, page_num=n, rotation=rotation)
        for n in nums
    ]


def test_build_pdf_does_not_duplicate_shared_resources(session_dirs):
    uploads, _ = session_dirs
    src = uploads / "doc.pdf"
    _shared_image_pdf(src)

    _, out, size, size_before = wes.build_pdf(_pages("doc", range(40)))

    # Antes (insert_pdf con final=True) el resultado crecía ~25x.
    assert size < src.stat().st_size * 1.5
    assert size_before < src.stat().st_size * 1.5
    with fitz.open(str(out)) as doc:
        assert doc.page_count == 40


def test_build_pdf_rotation_is_relative_to_page_rotation(session_dirs):
    uploads, _ = session_dirs
    doc = fitz.open()
    doc.new_page()
    doc[0].set_rotation(90)
    doc.save(str(uploads / "rot.pdf"))
    doc.close()

    results = {}
    for delta in (0, 90, 180, 270):
        _, out, _, _ = wes.build_pdf(_pages("rot", [0], rotation=delta), name=f"r{delta}")
        with fitz.open(str(out)) as d:
            results[delta] = d[0].rotation
    assert results == {0: 90, 90: 180, 180: 270, 270: 0}


def test_build_pdf_fails_when_pages_are_missing(session_dirs, sample_pdf_bytes):
    uploads, exports = session_dirs
    (uploads / "ok.pdf").write_bytes(sample_pdf_bytes)
    pages = _pages("ok", [0]) + _pages("gone", [0])

    with pytest.raises(ValueError, match="Faltan 1 archivo"):
        wes.build_pdf(pages)
    with pytest.raises(ValueError, match="no existen"):
        wes.build_pdf(_pages("ok", [0, 5]))
    assert not list(exports.glob("tmp_*.pdf"))


def test_page_config_rejects_negative_page_and_bad_rotation():
    with pytest.raises(ValueError):
        WorkspacePageConfig(page_id="a", source_document_id="d", page_num=-1)
    with pytest.raises(ValueError):
        WorkspacePageConfig(page_id="a", source_document_id="d", page_num=0, rotation=45)
    assert WorkspacePageConfig(page_id="a", source_document_id="d", page_num=0, rotation=450).rotation == 90


def test_build_zip_names_are_unique_case_insensitively(session_dirs, sample_pdf_bytes, monkeypatch):
    _, exports = session_dirs
    monkeypatch.setattr(wes, "set_export", lambda *a, **k: None)
    paths = []
    for i in range(3):
        p = exports / f"f{i}.pdf"
        p.write_bytes(sample_pdf_bytes)
        paths.append(p)

    wes.build_zip(paths, ["Informe.pdf", "informe.pdf", "INFORME"])

    import zipfile
    (zip_path,) = exports.glob("*.zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = [n.lower() for n in zf.namelist()]
    assert len(set(names)) == 3


def test_sanitize_filename_avoids_windows_reserved_names():
    assert wes.sanitize_filename("CON") == "_CON.pdf"
    assert wes.sanitize_filename("nul.pdf") == "_nul.pdf"
    assert wes.sanitize_filename("Contrato") == "Contrato.pdf"


def test_build_pdf_removes_output_when_cancelled_at_the_end(session_dirs, sample_pdf_bytes, monkeypatch):
    from app.engine.cancellation import OperationCancelled

    uploads, exports = session_dirs
    (uploads / "doc.pdf").write_bytes(sample_pdf_bytes)

    def compress_then_cancel(src, dest, *args, **kwargs):
        # Simula la carrera: el resultado ya quedó en `dest` y recién ahí llega la cancelación.
        dest.write_bytes(src.read_bytes())
        raise OperationCancelled("cancelado")

    monkeypatch.setattr(wes, "compress_pdf_file", compress_then_cancel)
    with pytest.raises(OperationCancelled):
        wes.build_pdf(_pages("doc", [0]), compression="high", name="Final")
    assert list(exports.iterdir()) == []


def test_sanitize_filename_keeps_words_separated():
    # Antes "Informe 2024/05" se guardaba como "05.pdf" (el "/" cortaba el nombre).
    assert wes.sanitize_filename("Informe 2024/05") == "Informe 2024-05.pdf"
    assert wes.sanitize_filename("Nota: aprobada") == "Nota - aprobada.pdf"
    assert wes.sanitize_download_filename("Expte 12/2024.zip", ".zip") == "Expte 12-2024.zip"
    assert wes.sanitize_download_filename("Informe v1.2", ".pdf") == "Informe v1.2.pdf"


def _rich_pdf(path):
    """4 páginas con índice, enlace interno, numeración romana, metadatos, adjunto
    y una capa oculta en la página 3."""
    doc = fitz.open()
    for i in range(4):
        doc.new_page().insert_text((72, 100), f"Pagina {i + 1}", fontsize=20)
    doc.set_toc([[1, "Capitulo 1", 1], [2, "Seccion 1.1", 2], [1, "Capitulo 2", 3], [1, "Anexo", 4]])
    doc[0].insert_link({
        "kind": fitz.LINK_GOTO, "from": fitz.Rect(72, 200, 300, 230), "page": 2, "to": fitz.Point(0, 0),
    })
    doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 250, 300, 280), "uri": "https://example.com"})
    doc.set_metadata({"title": "Titulo original", "author": "Autora"})
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1}])
    doc.embfile_add("datos.csv", b"a,b\n1,2\n", filename="datos.csv")
    ocg = doc.add_ocg("Borrador", on=False)
    doc[2].draw_rect(fitz.Rect(50, 300, 400, 500), color=(0, 0, 0), fill=(0, 0, 0), oc=ocg)
    doc.save(str(path))
    doc.close()


def _hidden_box_visible(page) -> bool:
    pix = page.get_pixmap(clip=fitz.Rect(60, 310, 390, 490))
    return min(pix.samples) < 128


def test_build_pdf_keeps_document_structure(session_dirs):
    uploads, _ = session_dirs
    _rich_pdf(uploads / "rich.pdf")
    # Se elimina la página 2 y la 4 pasa al principio: 4, 1, 3.
    _, out, _, _ = wes.build_pdf(_pages("rich", [3, 0, 2]), name="Rich")
    with fitz.open(str(out)) as doc:
        toc = doc.get_toc()
        assert [(t[1], t[2]) for t in toc] == [("Capitulo 1", 2), ("Capitulo 2", 3), ("Anexo", 1)]
        links = doc[1].get_links()  # la ex página 1
        assert any(link["kind"] == fitz.LINK_GOTO and link["page"] == 2 for link in links)
        assert any(link.get("uri") == "https://example.com" for link in links)
        assert [p.get_label() for p in doc] == ["iv", "i", "iii"]
        assert doc.metadata["title"] == "Titulo original"
        assert doc.embfile_names() == ["datos.csv"]
        assert not _hidden_box_visible(doc[2]), "la capa oculta no debe aparecer al exportar"


def test_build_pdf_labels_and_layers_with_several_sources(session_dirs, sample_pdf_bytes):
    uploads, _ = session_dirs
    _rich_pdf(uploads / "rich.pdf")
    (uploads / "plain.pdf").write_bytes(sample_pdf_bytes)
    pages = _pages("plain", [0]) + [
        WorkspacePageConfig(page_id="r2", source_document_id="rich", page_num=2, rotation=0),
    ]
    _, out, _, _ = wes.build_pdf(pages, name="Mix")
    with fitz.open(str(out)) as doc:
        assert [p.get_label() for p in doc] == ["1", "iii"]
        assert not _hidden_box_visible(doc[1])
        assert doc.metadata["title"] != "Titulo original"  # dos orígenes: no se copia el título
