"""
Flask application for WhatsApp Cloud API stamp‑card prototype.

Adds a dynamic Pillow renderer to match the black 'Coffee Shop' card:
- Big title, center rings logo, "THANK YOU FOR VISITING TODAY!"
- 2x5 circles; first N are stamped red; others are white outlines.

Routes:
  * /webhook (GET)  – verification for Meta
  * /webhook (POST) – handle incoming WhatsApp messages (TEST/SALE)
  * /card/<visits>.png – generated card image
  * /card?n=<visits>  – legacy query param (redirects to PNG)

Env vars:
  WHATSAPP_VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID,
  DATABASE_URL, HOST_URL
"""

import os
from io import BytesIO

import psycopg2
import requests
from flask import Flask, request, send_file, redirect, url_for
from PIL import Image, ImageDraw, ImageFont

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
    """
    Try DejaVu (available on Render's image); fall back to default bitmap.
    """
    try:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold \
               else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

def render_stamp_card(visits: int) -> BytesIO:
    """
    Render the loyalty card PNG that matches the provided design.
    First 'visits' circles (0..10) are stamped in red.
    """
    visits = max(0, min(10, int(visits)))

    W, H = 1024, 1024
    bg  = (0, 0, 0)           # black background
    fg  = (255, 255, 255)     # white lines/text
    red = (220, 53, 69)       # stamp red

    im = Image.new("RGB", (W, H), bg)
    d  = ImageDraw.Draw(im)

    # Fonts
    title_f = _font(120, bold=True)
    sub_f   = _font(52,  bold=True)
    foot_f  = _font(44,  bold=True)

    def center_text(y, text, font, color=fg):
        left, top, right, bottom = d.textbbox((0, 0), text, font=font)
        w, h = right - left, bottom - top
        d.text(((W - w) // 2, y), text, font=font, fill=color)

    # Top title + simple rings "logo"
    center_text(60, "COFFEE SHOP", title_f)

    cx, cy = W // 2, 230
    r_outer, r_inner = 110, 80
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], outline=fg, width=6)
    d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], outline=fg, width=4)

    center_text(350, "THANK YOU FOR VISITING TODAY!", sub_f)

    # Grid of 10 circles (2 rows x 5 cols), centered
    grid_top = 460
    row_gap  = 160
    col_gap  = 160
    circle_r = 60
    total_width = 4 * col_gap
    left_x = (W - total_width) // 2

    def circle_bbox(x, y):
        return [x - circle_r, y - circle_r, x + circle_r, y + circle_r]

    def draw_empty(x, y):
        d.ellipse(circle_bbox(x, y), outline=fg, width=6)

    def draw_stamp(x, y):
        # double ring
        d.ellipse(circle_bbox(x, y), outline=red, width=10)
        d.ellipse(circle_bbox(x, y), outline=red, width=3)
        # simple "cup" glyph
        cup_w, cup_h = 44, 28
        d.rounded_rectangle([x - cup_w // 2, y - cup_h // 2, x + cup_w // 2, y + cup_h // 2],
                            radius=6, outline=red, width=4)
        # handle
        d.arc([x + cup_w // 2 - 6, y - 10, x + cup_w // 2 + 18, y + 14], 300, 100, fill=red, width=4)
        # steam
        d.arc([x - 18, y - 48, x - 2, y - 10], 0, 120, fill=red, width=3)

    k = 0
    for row in range(2):
        y = grid_top + row * row_gap
        for col in range(5):
            x = left_x + col * col_gap
            (draw_stamp if k < visits else draw_empty)(x, y)
            k += 1

    center_text(900, "10 STAMPS = 1 FREE COFFEE", foot_f)

    buf = BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    return buf

def build_card_url(visits: int) -> str:
    """
    Build an absolute URL to the card PNG.
    """
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
    """
    Preferred card endpoint: /card/<visits>.png
    """
    png = render_stamp_card(visits)
    # modest caching for faster fetches by WhatsApp
    return send_file(png, mimetype="image/png", max_age=300)

@app.route("/card")
def card_query():
    """
    Legacy support: /card?n=<visits>  -> redirects to PNG route.
    """
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
