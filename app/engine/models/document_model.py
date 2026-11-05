
from pydantic import BaseModel, Field, field_validator

_VALID_COMPRESSION = {"none", "auto", "low", "medium", "high", "compress", "compressed"}


class WorkspacePageConfig(BaseModel):
    page_id: str
    source_document_id: str
    page_num: int = Field(ge=0)
    rotation: int = 0
    deleted: bool = False
    order: int = 0

    @field_validator("rotation")
    @classmethod
    def _check_rotation(cls, value: int) -> int:
        if value % 90 != 0:
            raise ValueError(f"Rotación inválida: {value}. Debe ser múltiplo de 90.")
        return value % 360


class OutputGroup(BaseModel):
    name: str
    page_ids: list[str]
    compression: str = "none"

    @field_validator("compression")
    @classmethod
    def _check_compression(cls, value: str) -> str:
        if value not in _VALID_COMPRESSION:
            raise ValueError(
                f"Compresión inválida: '{value}'. Valores: {sorted(_VALID_COMPRESSION)}"
            )
        if value in ("compress", "compressed", "low", "medium", "auto"):
            return "high"
        return value


class WorkspaceExportRequest(BaseModel):
    pages: list[WorkspacePageConfig]
    outputs: list[OutputGroup]
    make_zip: bool = False


class CompressRequest(BaseModel):
    document_id: str
    level: str = "medium"

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        if value not in _VALID_COMPRESSION:
            raise ValueError(
                f"Nivel inválido: '{value}'. Valores: {sorted(_VALID_COMPRESSION)}"
            )
        return value
