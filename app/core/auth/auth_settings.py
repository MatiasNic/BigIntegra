"""Configuración de acceso global para BigPDF Desktop.

PASO OBLIGATORIO:
1. Publicá el Apps Script como Web App (acceso: "Cualquier usuario").
2. Copiá la URL /exec.
3. Ejecutá: python tools/dev/encode_auth_url.py "https://script.google.com/macros/s/.../exec"
4. Pegá el resultado en ACCESS_SOURCE_URLS_B64.

Para pruebas locales, creá la variable de entorno:
    BIGPDF_ACCESS_URL=https://script.google.com/macros/s/.../exec
"""
from __future__ import annotations

from app import __version__ as APP_VERSION

APP_TITLE = f"B I G P D F - v. {APP_VERSION}"
APP_ID = "BIGPDF"

ACCESS_TIMEOUT_SECONDS = 8    # por operación de socket
ACCESS_DEADLINE_SECONDS = 12  # tope total de la verificación al arrancar
# Arranque rápido: si la última validación ONLINE exitosa fue hace menos de esto,
# la app abre enseguida y consulta la planilla en segundo plano (si deniega, se cierra).
RECENT_VALIDATION_HOURS = 24

# URL ofuscada del Apps Script (generada por tools/dev/encode_auth_url.py)
ACCESS_SOURCE_URLS_B64 = [
    "Ch0TABdcAkMcBBsHEh1JFwsJSgAKSQoBD0YKEQcUQh9AFEYvKQ8eEwYcFDgeNigMMR4+GSlSRQEGBh4EDl40FQszGRYWKFgANAETA0kUSjsYUyc9LhwfHREcGF45KgEKGCoDKR4KX1krEVsWFUYCCAEF"
]

# Clave XOR usada para ofuscar la URL (debe coincidir con encode_auth_url.py)
ACCESS_SOURCE_XOR_KEY = "bigpdf-login"
