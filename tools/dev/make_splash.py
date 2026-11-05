"""Genera packaging/splash.png: la imagen que muestra el .exe mientras se descomprime
(antes de que arranque Python/Qt). Mismo diseño que la ventana "Verificando acceso…".

Solo hace falta correrlo si cambia el logo o el diseño:
    pip install pillow
    python tools/dev/make_splash.py
Usa la fuente Liberation Sans (en Windows se puede apuntar FONT_B/FONT_R a Arial).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]

K = 3                      # supersampling para bordes y texto suaves
W, H = 344, 252            # tamaño de la "tarjeta" de la ventana de inicio de Qt
BRAND = (42, 111, 224)
LOGO = str(ROOT / "app" / "ui" / "web" / "assets" / "brand" / "bigpdf-icon-v3-192x192.png")
FONT_B = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

img = Image.new("RGB", (W * K, H * K), (255, 255, 255))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, W * K - 1, H * K - 1], radius=14 * K, outline=(226, 232, 240), width=K)

logo = Image.open(LOGO).convert("RGBA").resize((76 * K, 76 * K), Image.LANCZOS)
img.paste(logo, ((W * K - logo.width) // 2, 34 * K), logo)

def centered(text, y, font, fill):
    w = d.textlength(text, font=font)
    d.text(((W * K - w) / 2, y), text, font=font, fill=fill)

centered("Big PDF", 124 * K, ImageFont.truetype(FONT_B, 23 * K), (15, 23, 42))
centered("Iniciando…", 172 * K, ImageFont.truetype(FONT_R, 13 * K), (71, 85, 105))
# Barra decorativa (la imagen es estática: no hay animación posible acá).
d.rounded_rectangle([36 * K, 200 * K, (W - 36) * K, 204 * K], radius=2 * K, fill=(232, 239, 252))
d.rounded_rectangle([36 * K, 200 * K, (36 + 90) * K, 204 * K], radius=2 * K, fill=BRAND)

img = img.resize((W, H), Image.LANCZOS)
img.save(str(ROOT / "packaging" / "splash.png"), optimize=True)
print("ok", img.size)
