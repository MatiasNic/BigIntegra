import pytest
from pydantic import ValidationError

from app.engine.models.document_model import (
    CompressRequest,
    OutputGroup,
    WorkspaceExportRequest,
    WorkspacePageConfig,
)


def test_output_group_normalizes_lossy_aliases_to_high():
    group = OutputGroup(name="salida", page_ids=["p1"], compression="low")
    assert group.compression == "high"


def test_output_group_rejects_invalid_compression():
    with pytest.raises(ValidationError):
        OutputGroup(name="salida", page_ids=["p1"], compression="ultra")


def test_compress_request_default_level_valid():
    req = CompressRequest(document_id="doc-1")
    assert req.level == "medium"


def test_compress_request_rejects_invalid_level():
    with pytest.raises(ValidationError):
        CompressRequest(document_id="doc-1", level="ultra")


def test_workspace_export_request_builds_from_pages_and_outputs():
    page = WorkspacePageConfig(page_id="pg1", source_document_id="doc-1", page_num=0)
    output = OutputGroup(name="out", page_ids=["pg1"])
    req = WorkspaceExportRequest(pages=[page], outputs=[output], make_zip=True)
    assert req.pages[0].page_id == "pg1"
    assert req.make_zip is True
