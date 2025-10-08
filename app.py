"""
Flask app for WhatsApp Cloud API loyalty card prototype.
- Uses supabase-py (HTTP) instead of psycopg2 (TCP)
- SALE: record a visit + send updated card
- SURVEY: start 3-question onboarding flow (handled in qa_handler.py)
- REPORT: owner summary
- Robust Supabase selects (no NoneType .data issues)
- Image: solid red stamped circles + optional white coffee overlay
"""

import os
import datetime
from io import BytesIO

from flask import Flask, request, send_file, redirect, url_for
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, Image
from supabase import create_client

app = Flask(__name__)

# ───────────────────────────
# Environment
# ───────────────────────────
VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
HOST_URL        = os.getenv("HOST_URL")  # e.g. https://your-app.onrender.com

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
try:
    sb.postgrest.schema = "public"
except Exception:
    pass

# SURVEY flow lives in qa_handler.py
from qa_handler import start_profile_flow, handle_profile_answer

# ───────────────────────────
# Fonts
# ───────────────────────────
def _font(size: int, bold: bool = False):
    try:
        path = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

# ───────────────────────────
# Card rendering
# ───────────────────────────
COFFEE_ICON_PATH = os.getenv("COFFEE_ICON_PATH", "coffee.png")
try:
    _coffee_src = Image.open(COFFEE_ICON_PATH).convert("L")
except Exception:
    _coffee_src = None  # stamps still render without icon

def render_stamp_card(visits: int) -> BytesIO:
    """Render loyalty card with geometry that prevents any overlap."""
    visits = max(0, min(10, int(visits)))

    # Canvas & palette
    W, H = 1080, 1080
    BG, FG, RED = (0, 0, 0), (255, 255, 255), (220, 53, 69)

    # Anchors
    TITLE_Y         = 56
    LOGO_CENTER_Y   = 300
    THANK_Y_TARGET  = 520          # desired top of thank-you text
    FOOTER_Y        = H - 74

    # Grid geometry
    CIRCLE_R = 72                   # circle radius
    ROW_GAP  = 180                  # center-to-center row spacing
    COL_GAP  = 180
    LEFT_X   = (W - 4 * COL_GAP) // 2
    MIN_GAP_BELOW_THANK = 80        # empty pixels between thank-you bottom and TOP of first circles
    GRID_TOP_FIXED_MIN  = 720       # also never let centers be above this

    im = Image.new("RGB", (W, H), BG)
    d  = ImageDraw.Draw(im)

    # Fonts
    title_f = _font(120, bold=True)
    sub_f   = _font(50,  bold=True)
    foot_f  = _font(40,  bold=True)
    logo_f  = _font(40,  bold=True)

    # Title
    title = "COFFEE SHOP"
    tw, th = d.textbbox((0, 0), title, font=title_f)[2:]
    d.text(((W - tw)//2, TITLE_Y), title, font=title_f, fill=FG)

    # Concentric logo + centered LOGO
    logo_outer_r, logo_inner_r = 100, 80
    cx, cy = W // 2, LOGO_CENTER_Y
    for r in (logo_outer_r, logo_inner_r):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=FG, width=6)
    lbox = d.textbbox((0, 0), "LOGO", font=logo_f)
    d.text((cx - (lbox[2]-lbox[0])//2, cy - (lbox[3]-lbox[1])//2), "LOGO", font=logo_f, fill=FG)

    # Thank-you (ensure it's below the logo)
    thank = "THANK YOU FOR VISITING TODAY!"
    sw, sh = d.textbbox((0, 0), thank, font=sub_f)[2:]
    THANK_Y = max(THANK_Y_TARGET, LOGO_CENTER_Y + logo_outer_r + 40)  # top of text
    d.text(((W - sw)//2, THANK_Y), thank, font=sub_f, fill=FG)
    thank_bottom = THANK_Y + sh

    # Compute the first row center Y so that the circle TOP is far enough below the text
    # circle_top = GRID_TOP_CENTER - CIRCLE_R  >=  thank_bottom + MIN_GAP_BELOW_THANK
    min_grid_center_from_text = thank_bottom + MIN_GAP_BELOW_THANK + CIRCLE_R
    GRID_TOP_CENTER = max(GRID_TOP_FIXED_MIN, min_grid_center_from_text)

    def circle_bbox(cx, cy):
        return [cx - CIRCLE_R, cy - CIRCLE_R, cx + CIRCLE_R, cy + CIRCLE_R]

    # Optional white coffee overlay
    icon_src = _coffee_src if '_coffee_src' in globals() else None

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

    # Draw 2 rows × 5 cols
    k = 0
    for row in range(2):
        y = GRID_TOP_CENTER + row * ROW_GAP
        for col in range(5):
            x = LEFT_X + col * COL_GAP
            (draw_stamp if k < visits else draw_empty)(x, y)
            k += 1

    # Footer
    foot = "10 STAMPS = 1 FREE COFFEE"
    fw = d.textbbox((0, 0), foot, font=foot_f)[2]
    d.text(((W - fw)//2, FOOTER_Y), foot, font=foot_f, fill=FG)

    out = BytesIO()
    im.save(out, format="PNG")
    out.seek(0)
    return out


# ───────────────────────────
# WhatsApp helpers
# ───────────────────────────
def build_card_url(visits: int) -> str:
    base = (HOST_URL or request.url_root).rstrip("/")
    return f"{base}/card/{int(visits)}.png"

def send_text(to: str, body: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=12)
    except Exception as e:
        print("send_text error:", e)

def send_image(to: str, link: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": link}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=12)
    except Exception as e:
        print("send_image error:", e)

# ───────────────────────────
# Supabase helpers
# ───────────────────────────
def fetch_single_customer(sb, customer_id: str):
    """Return single customer dict or None; safe even when no rows."""
    try:
        resp = (
            sb.table("customers")
              .select("customer_id, number_of_visits, last_visit_at")
              .eq("customer_id", customer_id)
              .limit(1)
              .execute()
        )
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print("fetch_single_customer error:", e)
        return None

# ───────────────────────────
# Routes
# ───────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

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
        text = ((message.get("text") or {}).get("body") or "").strip()
        token = text.upper()

        # ── Commands
        if token == "TEST":
            send_image(from_number, build_card_url(0))
            send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
            return "ok", 200

        if token == "SALE":
            # record a visit
            row = fetch_single_customer(sb, from_number)
            visits = int(row.get("number_of_visits", 0)) + 1 if row else 1

            try:
                sb.table("customers").upsert({
                    "customer_id": from_number,
                    "number_of_visits": visits,
                    "last_visit_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                }).execute()
            except Exception as e:
                print("customers upsert error:", e)
                send_text(from_number, "⚠️ Sorry, I couldn't record your visit. Please try again.")
                return "ok", 200

            visits = max(1, min(10, visits))
            send_image(from_number, build_card_url(visits))
            if visits >= 10:
                send_text(from_number, "🎉 Free coffee unlocked! Show this to the barista.")
            else:
                send_text(from_number, f"Thanks for your visit! You now have {visits} stamp(s).")
            return "ok", 200

        if token == "SURVEY":
            # start the 3-question flow (birthday, flavor, promo)
            start_profile_flow(sb, from_number, send_text)
            return "ok", 200

        if token == "REPORT":
            # Active customers last 7 days + growth vs total
            all_rows = sb.table("customers").select("customer_id").execute().data or []
            total_customers = len(all_rows)

            seven_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
            active_rows = (
                sb.table("customers")
                  .select("customer_id")
                  .gte("last_visit_at", seven_days_ago)
                  .execute()
                  .data or []
            )
            active_count = len(active_rows)
            growth_pct = (active_count / total_customers * 100) if total_customers > 0 else 0.0

            report_text = (
                "📊 *Weekly Report*\n\n"
                f"Active customers (last 7 days): {active_count}\n"
                f"Growth vs total: {growth_pct:.1f}%\n\n"
                "Here's the link to your dashboard:\n"
                "https://wa-prototype-dashboard-1.streamlit.app/"
            )
            send_text(from_number, report_text)
            return "ok", 200

        # If not a command: see if a SURVEY flow is active and treat as answer
        handled = handle_profile_answer(sb, from_number, text, send_text)
        if handled:
            return "ok", 200

    except Exception as exc:
        print("Webhook error:", exc)

    return "ok", 200

# ───────────────────────────
# Card endpoints
# ───────────────────────────
@app.route("/card/<int:visits>.png")
def card_png(visits: int):
    return send_file(render_stamp_card(visits), mimetype="image/png", max_age=300)

@app.route("/card")
def card_query():
    try:
        n = int(request.args.get("n", 0))
    except ValueError:
        n = 0
    return redirect(url_for("card_png", visits=max(0, min(10, n))), code=302)

# ───────────────────────────
# Main
# ───────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
