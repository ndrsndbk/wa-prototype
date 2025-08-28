
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
    Render the loyalty card PNG (1080x1080) with generous spacing.
    - All text 20% smaller than earlier mock, 'LOGO' 50% smaller.
    - Footer text positioned lower (H - 74).
    Returns an in-memory PNG buffer.
    """
    visits = max(0, min(10, int(visits)))

    # Canvas & palette
    W, H = 1080, 1080
    bg  = (0, 0, 0)
    fg  = (255, 255, 255)
    red = (220, 53, 69)

    im = Image.new("RGB", (W, H), bg)
    d  = ImageDraw.Draw(im)

    # Fonts (scaled)
    title_f = _font(int(150 * 0.8), bold=True)  # 20% smaller
    sub_f   = _font(int(64  * 0.8), bold=True)
    foot_f  = _font(int(52  * 0.8), bold=True)
    logo_f  = _font(int(80  * 0.5), bold=True)   # 50% smaller

    def center_text(y, text, font, color=fg):
        left, top, right, bottom = d.textbbox((0, 0), text, font=font)
        w, h = right - left, bottom - top
        d.text(((W - w) // 2, y), text, font=font, fill=color)
        return h

    # Layout constants (roomy, no overlaps)
    MARGIN = int(H * 0.03)
    TITLE_Y      = 56
    LOGO_Y       = 330 + MARGIN   # rings center Y
    SUBTITLE_Y   = 535 + MARGIN
    SUBTITLE_GAP = 70
    GRID_TOP     = SUBTITLE_Y + SUBTITLE_GAP + 100
    ROW_GAP      = 180
    COL_GAP      = 180
    CIRCLE_R     = 72

    # Title
    center_text(TITLE_Y, "COFFEE SHOP", title_f)

    # Logo rings
    cx, cy = W // 2, LOGO_Y
    r_outer, r_inner = 130, 95
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], outline=fg, width=6)
    d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], outline=fg, width=4)

    # "LOGO" text centered inside rings
    lw, lh = d.textbbox((0, 0), "LOGO", font=logo_f)[2:]
    d.text((cx - lw // 2, cy - lh // 2), "LOGO", font=logo_f, fill=fg)

    # Subtitle
    center_text(SUBTITLE_Y, "THANK YOU FOR VISITING TODAY!", sub_f)

    # Grid (2 x 5) centered
    total_width = 4 * COL_GAP
    left_x = (W - total_width) // 2

    def circle_bbox(x, y):
        return [x - CIRCLE_R, y - CIRCLE_R, x + CIRCLE_R, y + CIRCLE_R]

    def draw_empty(x, y):
        d.ellipse(circle_bbox(x, y), outline=fg, width=6)

    # Coffee icon (tinted red) prepared once
    icon_img = None
    if _coffee_src is not None:
        target = int(CIRCLE_R * 1.15)
        icon_gray = _coffee_src.resize((target, target), Image.LANCZOS)
        red_rgba = Image.new("RGBA", icon_gray.size, red + (255,))
        alpha = ImageOps.invert(icon_gray)  # black lines -> alpha via invert
        stamped = Image.new("RGBA", icon_gray.size, (0, 0, 0, 0))
        stamped.paste(red_rgba, (0, 0), alpha)
        icon_img = stamped

    def draw_stamp(x, y):
        # double red ring + coffee icon
        d.ellipse(circle_bbox(x, y), outline=red, width=10)
        d.ellipse(circle_bbox(x, y), outline=red, width=3)
        if icon_img:
            ix, iy = icon_img.size
            im.paste(icon_img, (x - ix // 2, y - iy // 2), icon_img)

    k = 0
    for row in range(2):
        y = GRID_TOP + row * ROW_GAP
        for col in range(5):
            x = left_x + col * COL_GAP
            (draw_stamp if k < visits else draw_empty)(x, y)
            k += 1

    # Footer (nudged lower at H - 74)
    center_text(H - 74, "10 STAMPS = 1 FREE COFFEE", foot_f)

    # Return PNG bytes
    buf = BytesIO()
    im.save(buf, format="PNG")
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
