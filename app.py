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
from PIL import Image, ImageDraw, ImageFont, ImageOps

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

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try DejaVu (available on most Linux images); fall back to default."""
    try:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold \
               else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

# Optional coffee icon (black lines on white). You can provide your own PNG via env.
COFFEE_ICON_PATH = os.getenv("COFFEE_ICON_PATH", "coffee.png")
try:
    _coffee_src = Image.open(COFFEE_ICON_PATH).convert("L")
except Exception:
    _coffee_src = None

def render_stamp_card(visits: int) -> BytesIO:
    """
    Render the loyalty card as a PNG (1080x1080) in the improved designer style.
    Only aesthetics change vs. the original; routes/behavior remain identical.
    """
    # Clamp
    visits = max(0, min(10, int(visits)))

    # Canvas
    W, H = 1080, 1080
    img = Image.new("RGBA", (W, H), (0,0,0,0))
    d = ImageDraw.Draw(img, "RGBA")

    # Palette (coffee-inspired)
    bg_top     = (43, 30, 24)
    bg_bottom  = (26, 18, 15)
    cream      = (238, 231, 220)
    accent     = (174, 120, 84)
    accent_dk  = (132, 89, 62)
    ring       = (170, 164, 158)

    # Helpers
    def _grad(draw, box, top, bottom):
        x0, y0, x1, y1 = box
        h = y1 - y0
        for i in range(h):
            t = i / max(h-1, 1)
            r = int(top[0] + (bottom[0]-top[0]) * t)
            g = int(top[1] + (bottom[1]-top[1]) * t)
            b = int(top[2] + (bottom[2]-top[2]) * t)
            draw.line([(x0, y0+i), (x1, y0+i)], fill=(r,g,b))

    def _center_text(y, text, font, fill=cream):
        left, top, right, bottom = d.textbbox((0, 0), text, font=font)
        w, h = right - left, bottom - top
        d.text(((W - w)//2, y), text, font=font, fill=fill)
        return h

    def _bean(draw, center, w, h, color):
        cx, cy = center
        bbox = [cx - w//2, cy - h//2, cx + w//2, cy + h//2]
        draw.ellipse(bbox, fill=color)
        inset = int(min(w, h) * 0.18)
        arc_box = [bbox[0]+inset, bbox[1]+inset, bbox[2]-inset, bbox[3]-inset]
        draw.arc(arc_box, start=110, end=290, fill=(255,255,255,180), width=max(1, w//16))

    # Background gradient
    _grad(d, (0,0,W,H), bg_top, bg_bottom)

    # Fonts
    title_f = _font(120, bold=True)
    body_f  = _font(48, bold=True)
    small_f = _font(52, bold=True)

    # Title
    _center_text(60, "COFFEE SHOP", title_f, cream)

    # Logo badge with soft shadow
    r = 120
    cx, cy = W//2, 330
    shadow = Image.new("RGBA", (W,H), (0,0,0,0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse([cx-r, cy-r+8, cx+r, cy+r+8], fill=(0,0,0,140))
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))
    img.alpha_composite(shadow)
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=cream)
    # "LOGO" label
    logo_f = _font(64, bold=True)
    lw, lh = d.textbbox((0,0), "LOGO", font=logo_f)[2:]
    d.text((cx - lw//2, cy - lh//2), "LOGO", font=logo_f, fill=bg_bottom)

    # Subtitle (keep original copy)
    _center_text(480, "THANK YOU FOR VISITING TODAY!", body_f, cream)

    # Stamp grid (2x5)
    cols, rows = 5, 2
    dia = 140
    start_y = 610
    row_gap = 150
    col_gap = (W - 2*120 - cols*dia) // (cols - 1)
    start_x = (W - (cols*dia + (cols-1)*col_gap)) // 2
    centers = []
    for r_i in range(rows):
        y = start_y + r_i * row_gap
        for c_i in range(cols):
            x = start_x + c_i * (dia + col_gap) + dia // 2
            centers.append((x, y))

    for idx, (x, y) in enumerate(centers):
        sh = Image.new("RGBA", (W,H), (0,0,0,0))
        sdraw = ImageDraw.Draw(sh)
        sdraw.ellipse([x - dia//2, y - dia//2 + 6, x + dia//2, y + dia//2 + 6], fill=(0,0,0,140))
        sh = sh.filter(ImageFilter.GaussianBlur(10))
        img.alpha_composite(sh)
        if idx < visits:
            d.ellipse([x - dia//2, y - dia//2, x + dia//2, y + dia//2], fill=accent, outline=accent_dk, width=6)
            _bean(d, (x, y), int(dia*0.38), int(dia*0.58), accent_dk)
        else:
            d.ellipse([x - dia//2, y - dia//2, x + dia//2, y + dia//2], outline=ring, width=10)

    # Footer rounded banner
    banner_w, banner_h = 780, 90
    bx = (W - banner_w)//2
    by = 900
    sh = Image.new("RGBA", (W,H), (0,0,0,0))
    sdraw = ImageDraw.Draw(sh)
    sdraw.rounded_rectangle([bx, by+8, bx+banner_w, by+banner_h+8], radius=28, fill=(0,0,0,140))
    sh = sh.filter(ImageFilter.GaussianBlur(12))
    img.alpha_composite(sh)
    d.rounded_rectangle([bx, by, bx+banner_w, by+banner_h], radius=28, fill=accent)
    _center_text(by + banner_h//2 - small_f.size//10, "10 STAMPS = 1 FREE COFFEE", small_f, cream)

    # Return PNG
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

