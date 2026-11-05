"""Excepción de dominio para BigPDF (desktop, sin FastAPI)."""


class BigPDFError(Exception):
    def __init__(self, status_code_or_detail=400, detail: str = "") -> None:
        # Compatibilidad con fastapi.HTTPException(status_code, detail) posicional.
        if isinstance(status_code_or_detail, int):
            self.status_code = status_code_or_detail
            self.detail = detail
        else:
            self.detail = str(status_code_or_detail)
            self.status_code = 400
        super().__init__(self.detail)
