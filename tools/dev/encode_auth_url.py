"""Ofusca una URL de Apps Script para pegarla en ACCESS_SOURCE_URLS_B64.

Uso:
    python tools/dev/encode_auth_url.py "https://script.google.com/macros/s/.../exec"

Aplica el mismo XOR (clave ACCESS_SOURCE_XOR_KEY) + base64 que
app/core/auth/access_control.py revierte en tiempo de ejecución
(_decode_access_url). No es cifrado real: solo evita que la URL quede
en texto plano al mirar el repo por arriba.
"""
from __future__ import annotations

import base64
import sys

sys.path.insert(0, str(__file__.rsplit("tools", 1)[0] or "."))

from app.core.auth.auth_settings import ACCESS_SOURCE_XOR_KEY


def encode_auth_url(url: str, xor_key: str = ACCESS_SOURCE_XOR_KEY) -> str:
    key = xor_key.encode("utf-8") or b"k"
    data = url.encode("utf-8")
    obfuscated = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.b64encode(obfuscated).decode("utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Uso: python {sys.argv[0]} <url>", file=sys.stderr)
        return 1

    encoded = encode_auth_url(sys.argv[1])
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
