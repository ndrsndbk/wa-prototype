"""
card_renderer.py
- All rendering for the loyalty card image lives here.
- Produces solid red stamps with an optional white coffee overlay (coffee.png).
- Uses strict geometry so the stamp grid never overlaps the thank-you line.
"""

import os
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps, Image

# ---------------------- Tweakable layout constants ----------------------
W, H = 1080, 1080                # canvas
TITLE_Y = 56
LOGO_CENTER_Y = 300              # concentric rings center
THANK_Y_TARGET = 540             # desired TOP of thank-you text
FOOTER_Y = H - 74

# Stamp geometry
CIRCLE_R = 72                    # circle radius
ROW_GAP  = 180                   # center-to-center row spacing
COL_GAP  = 180
LEFT_X   = (W - 4 * COL_GAP) // 2

# Spacing protections
MIN_GAP_BELOW_THANK = 100        # gap between thank-you bottom and TOP of first circles
GRID_TOP_FIXED_MIN  = 740        # never let the first row center be above this

# Colors
BG  = (0, 0, 0)
FG  = (255, 255, 255)
RED = (220, 53, 69)

# ---------------------- Fonts ----------------------
def _font(size: int, bold: bool = False):
    """Try DejaVu (present on most Linux images); fall back to default."""
    try:
        path = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

# ---------------------- Optional stamp icon (white overlay) ----------------------
COFFEE_ICON_PATH = os.getenv("COFFEE_ICON_PATH", "coffee.png")
try:
    _coffee_src = Image.open(COFFEE_ICON_PATH).convert("L")
except Exception:
    _coffee_src = None  # stamps will render fine without an icon


def render_stamp_card(visits: int) -> BytesIO:
    """Render loyalty card with geometry that prevents any overlap."""
    visits = max(0, min(10, int(visits)))

    im = Image.new("RGB", (W, H), BG)
    d  = ImageDraw.Draw(im)

    # Fonts
    title_f = _font(120, bold=True)
    sub_f   = _font(50,  bold=True)
    foot_f  = _font(40,  bold=True)
    logo_f  = _font(40,  bold=True)

    # Title
    title = "COFFEE SHOP"
    tbox = d.textbbox((0, 0), title, font=title_f)
    d.text(((W - (tbox[2]-tbox[0]))//2, TITLE_Y), title, font=title_f, fill=FG)

    # Logo
    logo_outer_r, logo_inner_r = 100, 80
    cx, cy = W // 2, LOGO_CENTER_Y
    for r in (logo_outer_r, logo_inner_r):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=FG, width=6)
    lbox = d.textbbox((0, 0), "LOGO", font=logo_f)
    d.text((cx - (lbox[2]-lbox[0])//2, cy - (lbox[3]-lbox[1])//2), "LOGO", font=logo_f, fill=FG)

    # Thank-you
    thank = "THANK YOU FOR VISITING TODAY!"
    sbox = d.textbbox((0, 0), thank, font=sub_f)
    thank_w, thank_h = sbox[2]-sbox[0], sbox[3]-sbox[1]
    thank_y = max(THANK_Y_TARGET, LOGO_CENTER_Y + logo_outer_r + 40)
    d.text(((W - thank_w)//2, thank_y), thank, font=sub_f, fill=FG)
    thank_bottom = thank_y + thank_h

    # --- Grid vertical placement guards ---
    # Top guard: keep first row's TOP below thank-you by MIN_GAP_BELOW_THANK
    min_center_from_text = thank_bottom + MIN_GAP_BELOW_THANK + CIRCLE_R
    grid_top_center = max(GRID_TOP_FIXED_MIN, min_center_from_text)

    # Bottom guard: keep second row away from the footer by FOOTER_MARGIN_TOP
    FOOTER_MARGIN_TOP = 120  # adjust to taste (smaller number = closer to footer)
    max_top_center_allowed = FOOTER_Y - FOOTER_MARGIN_TOP - CIRCLE_R - ROW_GAP
    grid_top_center = min(grid_top_center, max_top_center_allowed)

    # Horizontal placement
    def circle_bbox(cx, cy):
        return [cx - CIRCLE_R, cy - CIRCLE_R, cx + CIRCLE_R, cy + CIRCLE_R]

    icon_src = _coffee_src

    def draw_empty(x, y):
        d.ellipse(circle_bbox(x, y), outline=FG, width=6)

    def draw_stamp(x, y):
        d.ellipse(circle_bbox(x, y), fill=RED, outline=RED, width=6)
        if icon_src is not None:
            icon_size = int(CIRCLE_R * 1.2)
            icon_gray = icon_src.resize((icon_size, icon_size), Image.LANCZOS)
            white_rgba = Image.new("RGBA", icon_gray.size, (255, 255, 255, 255))
            white_icon = Image.new("RGBA", icon_gray.size, (0, 0, 0, 0))
            white_icon.paste(white_rgba, (0, 0), icon_gray)
            im.paste(white_icon, (x - icon_size//2, y - icon_size//2), white_icon)

    # Draw 2 x 5
    k = 0
    for row in range(2):
        y = grid_top_center + row * ROW_GAP
        for col in range(5):
            x = LEFT_X + col * COL_GAP
            (draw_stamp if k < visits else draw_empty)(x, y)
            k += 1

    # Footer
    foot = "10 STAMPS = 1 FREE COFFEE"
    fbox = d.textbbox((0, 0), foot, font=foot_f)
    d.text(((W - (fbox[2]-fbox[0]))//2, FOOTER_Y), foot, font=foot_f, fill=FG)

    out = BytesIO()
    im.save(out, format="PNG")
    out.seek(0)
    return out

