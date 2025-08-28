"""
Flask application for WhatsApp Cloud API stamp-card prototype.

Adds a dynamic Pillow renderer to match the black 'Coffee Shop' card:
- Big title, center rings logo with 'LOGO' text, clear spacing
- 2x5 circles; first N are stamped red with a coffee icon; others white outlines.

Routes:
  * /webhook (GET)  – verification for Meta
  * /webhook (POST) – handle incoming WhatsApp messages (TEST/SALE)
  * /card/<visits>.png – generated card image
  * /card?n=<visits>  – legacy query param (redirects to PNG)

Env vars:
  WHATSAPP_VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID,
  DATABASE_URL, HOST_URL, COFFEE_ICON_PATH (optional, default 'coffee.png')
"""

import os
from io import BytesIO

import psycopg2
import requests
from flask import Flask, request, send_file, redirect, url_for
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter


# ------------------------------------------------------------------------------
# Flask + config
# ------------------------------------------------------------------------------

app = Flask(__name__)

VERIFY_TOKEN   = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
DATABASE_URL   = os.getenv("DATABASE_URL")
HOST_URL       = os.getenv("HOST_URL")  # e.g. https://your-app.onrender.com

conn = psycopg2.connect(DATABASE_URL) if DATABASE_URL else None


# ------------------------------------------------------------------------------
# Helpers: fonts, card rendering, WhatsApp senders, URL build
# ------------------------------------------------------------------------------


# ---- Font loader (Montserrat / Open Sans with safe fallbacks) ----
_FONT_CACHE = {}

FONT_CANDIDATES = {
    "bold": [
        "./fonts/Montserrat-ExtraBold.ttf",
        "./fonts/Montserrat-Bold.ttf",
        "./fonts/OpenSans-SemiBold.ttf",
        "Montserrat-ExtraBold.ttf",
        "Montserrat-Bold.ttf",
        "OpenSans-SemiBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "regular": [
        "./fonts/OpenSans-Regular.ttf",
        "./fonts/Montserrat-Regular.ttf",
        "OpenSans-Regular.ttf",
        "Montserrat-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "regular", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    paths = FONT_CANDIDATES["bold" if bold else "regular"]
    for p in paths:
        try:
            f = ImageFont.truetype(p, size=size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    # Final fallback – bitmap font
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f

# Optional coffee icon (black lines on white). You can provide your own PNG via env.
COFFEE_ICON_PATH = os.getenv("COFFEE_ICON_PATH", "coffee.png")
try:
    _coffee_src = Image.open(COFFEE_ICON_PATH).convert("L")
except Exception:
    _coffee_src = None



def render_stamp_card(visits: int) -> BytesIO:
    """
    Render the loyalty card as a PNG (1080x1080) in the premium designer style.
    - Warm coffee gradient + subtle paper grain + soft vignette
    - Clean empty rings (no inner shadow)
    - Filled stamps have soft drop shadow + bean icon
    - Dynamic footer bar width so text is always fully covered
    Backward-compatible with older Pillow: falls back if rounded/textbbox/blur are missing.
    """
    # Clamp visits (0..10)
    visits = max(0, min(10, int(visits)))

    # Canvas
    W, H = 1080, 1080
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    # Palette tuned to mock
    bg_top     = (46, 33, 27)    # brown top
    bg_bottom  = (30, 21, 17)    # brown bottom
    cream      = (241, 233, 222) # warm cream (text/badge)
    accent     = (169, 119, 86)  # filled stamp + footer
    accent_dk  = (120, 83, 60)   # stamp outline + bean
    ring       = (155, 147, 139) # empty ring outline

    # ---------- helpers ----------
    def _grad(draw, box, top, bottom):
        x0, y0, x1, y1 = box
        h = y1 - y0
        for i in range(h):
            t = i / max(h - 1, 1)
            r = int(top[0] + (bottom[0] - top[0]) * t)
            g = int(top[1] + (bottom[1] - top[1]) * t)
            b = int(top[2] + (bottom[2] - top[2]) * t)
            draw.line([(x0, y0 + i), (x1, y0 + i)], fill=(r, g, b))

    def _measure(text, font):
        try:
            L, T, R, B = d.textbbox((0, 0), text, font=font)
            return R - L, B - T
        except Exception:
            return d.textsize(text, font=font)

    def _bean(draw, center, w, h, color):
        cx, cy = center
        bbox = [cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2]
        draw.ellipse(bbox, fill=color)
        inset = int(min(w, h) * 0.18)
        arc_box = [bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset]
        draw.arc(arc_box, start=110, end=290, fill=(255, 255, 255, 180), width=max(1, w // 16))

    def _rounded_rect(draw, xy, radius, fill):
        # Try native rounded_rectangle; otherwise draw via mask
        if hasattr(draw, "rounded_rectangle"):
            draw.rounded_rectangle(xy, radius=radius, fill=fill); return
        x0, y0, x1, y1 = xy
        w, h = x1 - x0, y1 - y0
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        try:
            md.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
        except Exception:
            md.rectangle((radius, 0, w - radius, h), fill=255)
            md.rectangle((0, radius, w, h - radius), fill=255)
            for cx, cy in [(radius, radius), (w - radius, radius), (radius, h - radius), (w - radius, h - radius)]:
                md.pieslice((cx - radius, cy - radius, cx + radius, cy + radius), 0, 360, fill=255)
        rect = Image.new("RGBA", (w, h), fill)
        img.alpha_composite(rect, dest=(x0, y0), source=rect, mask=mask)

    # ---------- background ----------
    _grad(d, (0, 0, W, H), bg_top, bg_bottom)

    # Subtle paper grain
    try:
        noise = Image.effect_noise((W, H), 32)
        grain = Image.merge("RGBA", (noise, noise, noise, Image.new("L", (W, H), 18)))
        img = Image.alpha_composite(img, grain)
    except Exception:
        pass

    # Soft vignette
    try:
        vign = Image.new("L", (W, H), 0)
        vg = ImageDraw.Draw(vign)
        max_r = int(max(W, H) * 1.05)
        min_r = int(max(W, H) * 0.60)
        for r in range(min_r, max_r, 6):
            a = int(255 * (r - min_r) / max(1, (max_r - min_r)))
            vg.ellipse((W//2 - r, H//2 - r, W//2 + r, H//2 + r), outline=a)
        vign = vign.filter(ImageFilter.GaussianBlur(60))
        img = Image.composite(Image.new("RGBA", (W, H), (0, 0, 0, 80)), img, vign)
    except Exception:
        pass

    d = ImageDraw.Draw(img, "RGBA")

    # Fonts
    title_f = _font(122, bold=True)
    tag_f   = _font(50,  bold=True)
    small_f = _font(50,  bold=True)
    logo_f  = _font(62,  bold=True)

    # Title
    ttw, tth = _measure("COFFEE SHOP", title_f)
    d.text(((W - ttw)//2, 90), "COFFEE SHOP", font=title_f, fill=cream)

    # Logo badge + soft shadow
    r = 118
    cx, cy = W // 2, 340
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(sh)
    sdraw.ellipse([cx - r, cy - r + 10, cx + r, cy + r + 10], fill=(0, 0, 0, 160))
    try:
        sh = sh.filter(ImageFilter.GaussianBlur(18))
    except Exception:
        pass
    img = Image.alpha_composite(img, sh)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=cream)
    lw, lh = _measure("LOGO", logo_f)
    d.text((cx - lw // 2, cy - lh // 2), "LOGO", font=logo_f, fill=bg_bottom)

    # Tagline (from mock)
    tag = "YOUR COFFEE JOURNEY STARTS HERE"
    tw, th = _measure(tag, tag_f)
    d.text(((W - tw)//2, 510), tag, font=tag_f, fill=cream)

    # ---------- stamps ----------
    cols, rows = 5, 2
    dia = 138
    start_y = 645
    row_gap = 150
    col_gap = (W - 2 * 120 - cols * dia) // (cols - 1)
    start_x = (W - (cols * dia + (cols - 1) * col_gap)) // 2

    centers = []
    for r_i in range(rows):
        y = start_y + r_i * row_gap
        for c_i in range(cols):
            x = start_x + c_i * (dia + col_gap) + dia // 2
            centers.append((x, y))

    for idx, (x, y) in enumerate(centers):
        if idx < visits:
            # Soft shadow ONLY for filled stamps
            sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sdraw = ImageDraw.Draw(sh)
            sdraw.ellipse([x - dia//2, y - dia//2 + 7, x + dia//2, y + dia//2 + 7], fill=(0, 0, 0, 140))
            try:
                sh = sh.filter(ImageFilter.GaussianBlur(12))
            except Exception:
                pass
            img = Image.alpha_composite(img, sh)

            d.ellipse([x - dia // 2, y - dia // 2, x + dia // 2, y + dia // 2],
                      fill=accent, outline=accent_dk, width=6)
            _bean(d, (x, y), int(dia * 0.40), int(dia * 0.60), accent_dk)
        else:
            d.ellipse([x - dia // 2, y - dia // 2, x + dia // 2, y + dia // 2],
                      outline=ring, width=9)

    # ---------- footer (dynamic width) ----------
    text = "10 STAMPS = 1 FREE COFFEE"
    tw, th = _measure(text, small_f)
    pad_x, pad_y = 44, 18
    banner_w = min(W - 160, tw + 2 * pad_x)
    banner_h = th + 2 * pad_y
    bx = (W - banner_w) // 2
    by = 925

    # Footer shadow
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(sh)
    try:
        sdraw.rounded_rectangle([bx, by + 10, bx + banner_w, by + banner_h + 10], radius=28, fill=(0, 0, 0, 150))
    except Exception:
        sdraw.rectangle([bx, by + 10, bx + banner_w, by + banner_h + 10], fill=(0, 0, 0, 150))
    try:
        sh = sh.filter(ImageFilter.GaussianBlur(16))
    except Exception:
        pass
    img = Image.alpha_composite(img, sh)

    # Footer bar
    d = ImageDraw.Draw(img, "RGBA")
    try:
        d.rounded_rectangle([bx, by, bx + banner_w, by + banner_h], radius=28, fill=accent)
    except Exception:
        _rounded_rect(d, [bx, by, bx + banner_w, by + banner_h], radius=28, fill=accent)
    d.text(((W - tw)//2, by + (banner_h - th)//2), text, font=small_f, fill=cream)

    # Encode PNG (your route expects .png)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf



def build_card_url(visits: int) -> str:
    base = (HOST_URL or request.url_root).rstrip("/")
    return f"{base}/card/{int(visits)}.png"

def send_text(to: str, body: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        raise RuntimeError("WhatsApp token or phone number ID not configured")
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        if resp.status_code != 200:
            print("WhatsApp API (text) error:", resp.status_code, resp.text[:400])
    except Exception as exc:
        print("Error sending text:", exc)

def send_image(to: str, link: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        raise RuntimeError("WhatsApp token or phone number ID not configured")
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": link}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        if resp.status_code != 200:
            print("WhatsApp API (image) error:", resp.status_code, resp.text[:400])
    except Exception as exc:
        print("Error sending image:", exc)


# ------------------------------------------------------------------------------
# Webhook handlers
# ------------------------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.get_json(silent=True) or {}
    try:
        entry   = (data.get("entry") or [None])[0] or {}
        changes = (entry.get("changes") or [None])[0] or {}
        value   = changes.get("value") or {}
        message = (value.get("messages") or [None])[0]
        if not message:
            return "ignored", 200

        from_number = message.get("from")
        text = ((message.get("text") or {}).get("body") or "").strip().upper()

        if text == "TEST":
            send_image(from_number, build_card_url(0))
            send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
            return "ok", 200

        if text == "SALE":
            if conn is None:
                raise RuntimeError("Database connection is not configured")

            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO customers (customer_id, number_of_visits, last_visit_at)
                        VALUES (%s, 1, NOW())
                        ON CONFLICT (customer_id)
                        DO UPDATE SET number_of_visits = customers.number_of_visits + 1,
                                      last_visit_at = NOW()
                        RETURNING number_of_visits
                    """, (from_number,))
                    visits = cur.fetchone()[0]

            visits = max(1, min(10, visits))
            send_image(from_number, build_card_url(visits))
            if visits >= 10:
                send_text(from_number, "🎉 Free coffee unlocked! Show this to the barista.")
            else:
                send_text(from_number, f"Thanks for your visit! You now have {visits} stamp(s).")
            return "ok", 200

    except Exception as exc:
        print("Error handling webhook:", exc)

    return "ok", 200


# ------------------------------------------------------------------------------
# Image endpoints
# ------------------------------------------------------------------------------

@app.route("/card/<int:visits>.png")
def card_png(visits: int):
    """Preferred card endpoint: /card/<visits>.png"""
    png = render_stamp_card(visits)
    return send_file(png, mimetype="image/png", max_age=300)

@app.route("/card")
def card_query():
    """Legacy: /card?n=<visits> -> redirect to PNG route."""
    try:
        n = int(request.args.get("n", 0))
    except ValueError:
        n = 0
    return redirect(url_for("card_png", visits=max(0, min(10, n))), code=302)


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

