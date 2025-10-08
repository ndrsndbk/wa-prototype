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
    """
    Renders the loyalty card with:
    - Fixed spacing for logo, text, and grid
    - Solid red fill for stamped circles
    - Optional white coffee icon overlay
    """
    visits = max(0, min(10, int(visits)))

    # Canvas & palette
    W, H = 1080, 1080
    BG, FG, RED = (0, 0, 0), (255, 255, 255), (220, 53, 69)
    GAP_AFTER_TITLE = 40
    GAP_AFTER_LOGO  = 40
    GAP_AFTER_THANK = 60
    FOOTER_GAP_BOTTOM = 74

    im = Image.new("RGB", (W, H), BG)
    d  = ImageDraw.Draw(im)

    # Fonts
    title_f = _font(120, bold=True)
    sub_f   = _font(50,  bold=True)
    foot_f  = _font(40,  bold=True)
    logo_f  = _font(40,  bold=True)

    # Title
    title_text = "COFFEE SHOP"
    tbox = d.textbbox((0, 0), title_text, font=title_f)
    t_w, t_h = tbox[2]-tbox[0], tbox[3]-tbox[1]
    y = 56
    d.text(((W - t_w)//2, y), title_text, font=title_f, fill=FG)
    y += t_h + GAP_AFTER_TITLE

    # Concentric logo + centered "LOGO"
    logo_outer_r, logo_inner_r = 100, 80
    logo_center = (W // 2, y + logo_outer_r)
    for r in (logo_outer_r, logo_inner_r):
        d.ellipse([logo_center[0]-r, logo_center[1]-r,
                   logo_center[0]+r, logo_center[1]+r],
                  outline=FG, width=6)
    logo_text = "LOGO"
    lbox = d.textbbox((0, 0), logo_text, font=logo_f)
    lw, lh = lbox[2]-lbox[0], lbox[3]-lbox[1]
    d.text((logo_center[0]-lw//2, logo_center[1]-lh//2), logo_text, font=logo_f, fill=FG)
    y = logo_center[1] + logo_outer_r + GAP_AFTER_LOGO

    # Thank-you text
    thank_text = "THANK YOU FOR VISITING TODAY!"
    sbox = d.textbbox((0, 0), thank_text, font=sub_f)
    sw, sh = sbox[2]-sbox[0], sbox[3]-sbox[1]
    d.text(((W - sw)//2, y), thank_text, font=sub_f, fill=FG)
    y += sh + GAP_AFTER_THANK

    # Grid
    CIRCLE_R, ROW_GAP, COL_GAP = 72, 180, 180
    GRID_TOP = y
    left_x   = (W - 4 * COL_GAP) // 2

    def circle_bbox(cx, cy):
        return [cx - CIRCLE_R, cy - CIRCLE_R, cx + CIRCLE_R, cy + CIRCLE_R]

    def draw_empty(cx, cy):
        d.ellipse(circle_bbox(cx, cy), outline=FG, width=6)

    def draw_stamp(cx, cy):
        # Solid red fill
        d.ellipse(circle_bbox(cx, cy), fill=RED, outline=RED, width=6)
        # Optional white coffee overlay
        if _coffee_src is not None:
            icon_size = int(CIRCLE_R * 1.2)
            icon_gray = _coffee_src.resize((icon_size, icon_size), Image.LANCZOS)
            white_rgba = Image.new("RGBA", icon_gray.size, (255, 255, 255, 255))
            white_icon = Image.new("RGBA", icon_gray.size, (0, 0, 0, 0))
            white_icon.paste(white_rgba, (0, 0), icon_gray)
            im.paste(white_icon, (cx - icon_size//2, cy - icon_size//2), white_icon)

    k = 0
    for row in range(2):
        cy = GRID_TOP + row * ROW_GAP
        for col in range(5):
            cx = left_x + col * COL_GAP
            (draw_stamp if k < visits else draw_empty)(cx, cy)
            k += 1

    # Footer
    foot_text = "10 STAMPS = 1 FREE COFFEE"
    fbox = d.textbbox((0, 0), foot_text, font=foot_f)
    fw = fbox[2]-fbox[0]
    d.text(((W - fw)//2, H - FOOTER_GAP_BOTTOM), foot_text, font=foot_f, fill=FG)

    buf = BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    return buf


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
