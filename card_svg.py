# card_svg.py
from io import BytesIO
from base64 import b64encode
from typing import List
import cairosvg

# Canvas
W, H = 1080, 1080

# Palette
FG  = "#FFFFFF"
BG  = "#000000"
RED = "#DC3545"

# Geometry
TITLE_Y        = 120
LOGO_CX        = W // 2
LOGO_CY        = 300
LOGO_R_OUT     = 100
LOGO_R_IN      = 80

THANK_Y        = 520  # text baseline (we'll place the text slightly above grid)
GRID_TOP_Y     = 700  # center of first row
ROW_GAP        = 180
COL_GAP        = 180
CIRCLE_R       = 72
LEFT_X         = (W - 4 * COL_GAP) // 2
FOOTER_Y       = H - 74

TITLE_TEXT     = "COFFEE SHOP"
THANK_TEXT     = "THANK YOU FOR VISITING TODAY!"
FOOTER_TEXT    = "10 STAMPS = 1 FREE COFFEE"

def _circle(cx: int, cy: int, r: int, fill: str, stroke: str, sw: int) -> str:
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'

def _text(x: int, y: int, s: str, size: int, weight: int = 700, anchor: str = "middle") -> str:
    # Using system sans-serif. If you want perfect consistency, embed a webfont with @font-face.
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-family="DejaVu Sans, Arial, Helvetica, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{FG}">{s}</text>'
    )

def _grid(visits: int) -> str:
    # 10 positions, left-to-right, two rows
    parts: List[str] = []
    k = 0
    for row in range(2):
        cy = GRID_TOP_Y + row * ROW_GAP
        for col in range(5):
            cx = LEFT_X + col * COL_GAP
            if k < visits:
                # solid red stamp
                parts.append(_circle(cx, cy, CIRCLE_R, RED, RED, 6))
            else:
                # empty white outline
                parts.append(_circle(cx, cy, CIRCLE_R, "none", FG, 6))
            k += 1
    return "\n".join(parts)

def build_svg(visits: int) -> str:
    visits = max(0, min(10, int(visits)))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="0" width="{W}" height="{H}" fill="{BG}"/>

  <!-- Title -->
  {_text(W//2, TITLE_Y, TITLE_TEXT, 120)}

  <!-- Concentric logo -->
  {_circle(LOGO_CX, LOGO_CY, LOGO_R_OUT, "none", FG, 6)}
  {_circle(LOGO_CX, LOGO_CY, LOGO_R_IN,  "none", FG, 6)}
  {_text(LOGO_CX, LOGO_CY + 14, "LOGO", 40)}

  <!-- Thank you -->
  {_text(W//2, THANK_Y, THANK_TEXT, 50)}

  <!-- Grid (exact coordinates; won’t move) -->
  {_grid(visits)}

  <!-- Footer -->
  {_text(W//2, FOOTER_Y, FOOTER_TEXT, 40)}
</svg>
"""

def render_card_png(visits: int) -> BytesIO:
    svg = build_svg(visits)
    out = BytesIO()
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=out, output_width=W, output_height=H, background_color=BG)
    out.seek(0)
    return out
