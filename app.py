
"""
Flask application for WhatsApp Cloud API stamp-card prototype (+ Flows support).

Adds a dynamic Pillow renderer to match the black 'Coffee Shop' card:
- Big title, center rings logo with 'LOGO' text, clear spacing
- 2x5 circles; first N are stamped red with a coffee icon; others white outlines.

New in this version:
  • Flow-enabled template sender (send_template_with_flow)
  • Flow submission capture (save_flow_submission) that upserts into customer_profiles
  • Webhook branch:
      - "STAMP"  -> send the approved template that contains a Flow button
      - Flow submission -> parse fields (birthday, preferred_promo, favorite_flavor, marketing_opt_in)

Routes:
  * /webhook (GET)  – verification for Meta
  * /webhook (POST) – handle incoming WhatsApp messages (TEST/SALE/STAMP + Flow replies)
  * /card/<visits>.png – generated card image
  * /card?n=<visits>  – legacy query param (redirects to PNG)

Env vars:
  WHATSAPP_VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID,
  DATABASE_URL, HOST_URL, COFFEE_ICON_PATH (optional, default 'coffee.png')

New env vars for Flow template:
  FLOW_TEMPLATE_NAME  (approved template name that has the Flow button attached)
  FLOW_TEMPLATE_LANG  (e.g., 'en')
"""

import os
import json
from io import BytesIO
from typing import Any, Dict, Optional

import psycopg2
import requests
from flask import Flask, request, send_file, redirect, url_for
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ------------------------------------------------------------------------------
# Flask + config
# ------------------------------------------------------------------------------

app = Flask(__name__)

VERIFY_TOKEN     = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN   = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID  = os.getenv("PHONE_NUMBER_ID")
DATABASE_URL     = os.getenv("DATABASE_URL")
HOST_URL         = os.getenv("HOST_URL")  # e.g. https://your-app.onrender.com

# Flow template env
FLOW_TEMPLATE_NAME = os.getenv("FLOW_TEMPLATE_NAME")
FLOW_TEMPLATE_LANG = os.getenv("FLOW_TEMPLATE_LANG", "en")

conn = psycopg2.connect(DATABASE_URL) if DATABASE_URL else None


# ------------------------------------------------------------------------------
# Helpers: fonts, card rendering, WhatsApp senders, URL build
# ------------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try DejaVu (available on most Linux images); fall back to default."""
    try:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
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

def _wa_post(payload: Dict[str, Any]) -> Optional[requests.Response]:
    """Low-level POST to WhatsApp Graph API."""
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        raise RuntimeError("WhatsApp token or phone number ID not configured")
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            print("WhatsApp API error:", resp.status_code, resp.text[:500])
        return resp
    except Exception as exc:
        print("WhatsApp POST error:", exc)
        return None

def send_text(to: str, body: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    _wa_post(payload)

def send_image(to: str, link: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": link},
    }
    _wa_post(payload)

def send_template_with_flow(to: str) -> None:
    """Send the approved template that contains a Flow button.
    The button-to-Flow wiring is defined inside the approved template in Meta.
    You only need to specify the name + language here.
    """
    if not FLOW_TEMPLATE_NAME:
        raise RuntimeError("FLOW_TEMPLATE_NAME not configured")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": FLOW_TEMPLATE_NAME,
            "language": {"code": FLOW_TEMPLATE_LANG or "en"},
            # If your template requires header/body variables, add components here.
            # "components": [ ... ]
        },
    }
    _wa_post(payload)


# ------------------------------------------------------------------------------
# Database helpers (profiles)
# ------------------------------------------------------------------------------

def save_flow_submission(customer_id: str, response_data: Dict[str, Any]) -> None:
    """Upsert customer profile fields captured by the Flow.
    Expected keys (case-insensitive): birthday, preferred_promo, favorite_flavor, marketing_opt_in
    """
    if conn is None:
        print("DB not configured; skipping profile upsert")
        return

    # Normalize keys defensively
    norm = { (k or "").strip().lower(): v for k, v in (response_data or {}).items() }

    birthday         = norm.get("birthday") or norm.get("date_of_birth") or None
    preferred_promo  = norm.get("preferred_promo") or norm.get("promo_preference") or None
    favorite_flavor  = norm.get("favorite_flavor") or norm.get("favourite_flavour") or None
    marketing_opt_in = norm.get("marketing_opt_in") or norm.get("opt_in") or norm.get("consent")

    # Convert booleans where possible
    def to_bool(x):
        if isinstance(x, bool):
            return x
        if isinstance(x, (int, float)) and x in (0,1):
            return bool(x)
        if isinstance(x, str):
            t = x.strip().lower()
            if t in ("yes","true","y","1","on","agree","consent"):
                return True
            if t in ("no","false","n","0","off","disagree"):
                return False
        return None

    marketing_opt_in = to_bool(marketing_opt_in)

    with conn:
        with conn.cursor() as cur:
            # Ensure table exists (idempotent)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_profiles (
                  customer_id TEXT PRIMARY KEY,
                  birthday DATE,
                  preferred_promo TEXT,
                  favorite_flavor TEXT,
                  marketing_opt_in BOOLEAN DEFAULT FALSE,
                  updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Upsert
            cur.execute("""
                INSERT INTO customer_profiles
                  (customer_id, birthday, preferred_promo, favorite_flavor, marketing_opt_in, updated_at)
                VALUES
                  (%s, %s, %s, %s, COALESCE(%s, FALSE), NOW())
                ON CONFLICT (customer_id)
                DO UPDATE SET
                  birthday = EXCLUDED.birthday,
                  preferred_promo = EXCLUDED.preferred_promo,
                  favorite_flavor = EXCLUDED.favorite_flavor,
                  marketing_opt_in = EXCLUDED.marketing_opt_in,
                  updated_at = NOW()
            """, (customer_id, birthday, preferred_promo, favorite_flavor, marketing_opt_in))

    print(f"Saved Flow submission for {customer_id}: "
          f"birthday={birthday}, preferred_promo={preferred_promo}, "
          f"favorite_flavor={favorite_flavor}, marketing_opt_in={marketing_opt_in}")


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


def _extract_flow_response(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Try to extract a Flow submission payload from the incoming message.
    Meta uses 'interactive' with type 'nfm_reply' for Flows; the data often lives in
    interactive.nfm_reply.response_json (string) or .response (object).
    We'll support both shapes defensively.
    """
    interactive = message.get("interactive") or {}
    if not interactive:
        return None

    # nfm_reply path (Flows)
    nfm = interactive.get("nfm_reply")
    if nfm:
        # response_json may be a JSON string
        resp_json = nfm.get("response_json")
        if isinstance(resp_json, str):
            try:
                return json.loads(resp_json)
            except Exception:
                pass
        # or response dict may already be structured
        resp_obj = nfm.get("response") or nfm.get("data") or None
        if isinstance(resp_obj, dict):
            return resp_obj

    # Fallback: some older shapes (unlikely, but handle gracefully)
    if interactive.get("type") in ("flow","nfm_reply"):
        payload = interactive.get("payload") or interactive.get("response") or None
        if isinstance(payload, dict):
            return payload

    return None


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.get_json(silent=True) or {}
    try:
        entry   = (data.get("entry") or [None])[0] or {}
        changes = (entry.get("changes") or [None])[0] or {}
        value   = changes.get("value") or {}

        # 1) Flow completion messages (arrive as 'messages' with interactive.nfm_reply)
        messages = value.get("messages") or []
        if messages:
            message = messages[0]
            from_number = message.get("from")
            msg_type    = message.get("type")

            # If it's a Flow submission, capture it first.
            flow_response = _extract_flow_response(message)
            if flow_response and from_number:
                save_flow_submission(from_number, flow_response)
                # Optionally acknowledge the submission
                send_text(from_number, "✅ Got it—your preferences are saved.")
                return "ok", 200

            # Otherwise we handle plain text triggers
            text = ((message.get("text") or {}).get("body") or "").strip().upper()

            if text == "TEST":
                send_image(from_number, build_card_url(0))
                send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
                return "ok", 200

            if text == "STAMP":
                # Send the template that includes the Flow button
                send_template_with_flow(from_number)
                send_text(from_number, "Tap the button to update your profile and unlock better rewards.")
                return "ok", 200

            if text == "SALE":
                if conn is None:
                    raise RuntimeError("Database connection is not configured")

                with conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS customers (
                                customer_id TEXT PRIMARY KEY,
                                number_of_visits INT DEFAULT 0,
                                last_visit_at TIMESTAMPTZ
                            )
                        """)
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

                # Try to personalize the thank-you if profile exists
                personalized = None
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT favorite_flavor, preferred_promo FROM customer_profiles WHERE customer_id=%s",
                                (from_number,),
                            )
                            row = cur.fetchone()
                            if row:
                                flavor, promo = row
                                if flavor:
                                    personalized = f"Thanks! You now have {visits} stamp(s). ☕️ P.S. We'll note your {flavor} preference."
                                elif promo:
                                    personalized = f"Thanks! You now have {visits} stamp(s). 📣 We'll tailor more {promo}-style promos for you."
                except Exception as _:
                    pass

                send_text(from_number, personalized or f"Thanks for your visit! You now have {visits} stamp(s).")
                if visits >= 10:
                    send_text(from_number, "🎉 Free coffee unlocked! Show this to the barista.")
                return "ok", 200

        # 2) Status updates (delivery/read/etc.) can be ignored safely
        statuses = value.get("statuses") or []
        if statuses:
            # no-op, but log the first one for visibility
            s0 = statuses[0]
            print("Status:", s0.get("status"), "for", s0.get("recipient_id"))
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
    return send_file(png, mimetype="image/png", cache_timeout=300)

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
