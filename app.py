"""
Flask app for WhatsApp Cloud API loyalty card prototype.
- Uses supabase-py (HTTP) instead of psycopg2 (TCP)
- Image fixes: concentric logo rings, centered "LOGO", lowered grid
- Stamped circles: solid red + white coffee icon overlay
- New "REPORT" trigger for weekly owner summary
"""
import os
import datetime
from io import BytesIO

from flask import Flask, request, send_file, redirect, url_for
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from supabase import create_client

app = Flask(__name__)

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------
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

# ------------------------------------------------------------------------------
# ========================= BEGIN: IMAGE RENDERING BLOCK =========================
# ------------------------------------------------------------------------------
def _font(size: int, bold: bool = False):
    """Try DejaVu (present on most Linux images); fall back to default."""
    try:
        path = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

# Optional stamp icon (grayscale source). If missing, stamps still render.
COFFEE_ICON_PATH = os.getenv("COFFEE_ICON_PATH", "coffee.png")
try:
    _coffee_src = Image.open(COFFEE_ICON_PATH).convert("L")
except Exception:
    _coffee_src = None  # stamps will be solid red with no icon overlay

def render_stamp_card(visits: int) -> BytesIO:
    """Render the loyalty stamp card as a PNG and return an in-memory buffer."""
    visits = max(0, min(10, int(visits)))

    # Canvas + palette
    W, H = 1080, 1080
    bg, fg, red = (0, 0, 0), (255, 255, 255), (220, 53, 69)

    im = Image.new("RGB", (W, H), bg)
    d  = ImageDraw.Draw(im)

    # Fonts
    title_f = _font(120, bold=True)
    sub_f   = _font(50,  bold=True)
    foot_f  = _font(40,  bold=True)
    logo_f  = _font(40,  bold=True)

    # --- header: COFFEE SHOP ---
    title_box = d.textbbox((0, 0), "COFFEE SHOP", font=title_f)
    title_w   = title_box[2] - title_box[0]
    d.text(((W - title_w)//2, 56), "COFFEE SHOP", font=title_f, fill=fg)

    # --- logo placeholder (concentric rings, centered LOGO text) ---
    logo_center = (W // 2, 330)
    for r in (100, 80):
        d.ellipse([logo_center[0]-r, logo_center[1]-r,
                   logo_center[0]+r, logo_center[1]+r],
                  outline=fg, width=6)
    logo_text = "LOGO"
    lbox = d.textbbox((0, 0), logo_text, font=logo_f)
    lw, lh = lbox[2] - lbox[0], lbox[3] - lbox[1]
    d.text((logo_center[0] - lw//2, logo_center[1] - lh//2),
           logo_text, font=logo_f, fill=fg)

    # --- thank-you line ---
    sub_text = "THANK YOU FOR VISITING TODAY!"
    sbox = d.textbbox((0, 0), sub_text, font=sub_f)
    sw = sbox[2] - sbox[0]
    d.text(((W - sw)//2, 568), sub_text, font=sub_f, fill=fg)

    # --- stamp grid (lowered to avoid overlap) ---
    CIRCLE_R, ROW_GAP, COL_GAP, GRID_TOP = 72, 180, 180, 720
    left_x = (W - 4 * COL_GAP) // 2

    def circle_bbox(x, y):
        return [x - CIRCLE_R, y - CIRCLE_R, x + CIRCLE_R, y + CIRCLE_R]

    # Solid-red stamp with white coffee overlay
    def draw_stamp(x, y):
        # solid red disk + red outline
        d.ellipse(circle_bbox(x, y), fill=red, outline=red, width=6)

        # optional white coffee overlay derived from grayscale icon
        if _coffee_src is not None:
            icon_size = int(CIRCLE_R * 1.2)  # tweak 1.0–1.4 to taste
            icon_gray = _coffee_src.resize((icon_size, icon_size), Image.LANCZOS)
            white_rgba = Image.new("RGBA", icon_gray.size, (255, 255, 255, 255))
            # Use icon_gray as alpha to cut the white overlay
            white_icon = Image.new("RGBA", icon_gray.size, (0, 0, 0, 0))
            white_icon.paste(white_rgba, (0, 0), icon_gray)
            im.paste(white_icon, (x - icon_size//2, y - icon_size//2), white_icon)

    def draw_empty(x, y):
        d.ellipse(circle_bbox(x, y), outline=fg, width=6)

    # paint the grid
    k = 0
    for row in range(2):
        y = GRID_TOP + row * ROW_GAP
        for col in range(5):
            x = left_x + col * COL_GAP
            (draw_stamp if k < visits else draw_empty)(x, y)
            k += 1

    # --- footer ---
    foot_text = "10 STAMPS = 1 FREE COFFEE"
    fbox = d.textbbox((0, 0), foot_text, font=foot_f)
    fw   = fbox[2] - fbox[0]
    d.text(((W - fw)//2, H - 74), foot_text, font=foot_f, fill=fg)

    # Return PNG buffer
    buf = BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    return buf
# ========================== END: IMAGE RENDERING BLOCK ==========================
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# WhatsApp helpers
# ------------------------------------------------------------------------------
def build_card_url(visits: int) -> str:
    """Public URL used in WhatsApp image messages."""
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

# ------------------------------------------------------------------------------
# Webhook
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

        # --- simple test image ---
        if text == "TEST":
            send_image(from_number, build_card_url(0))
            send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
            return "ok", 200

        # --- record a sale/visit ---
        if text == "SALE":
            row = (
                sb.table("customers")
                  .select("number_of_visits")
                  .eq("customer_id", from_number)
                  .maybe_single()
                  .execute()
                  .data
            )
            visits = (row["number_of_visits"] + 1) if row else 1

            sb.table("customers").upsert({
                "customer_id": from_number,
                "number_of_visits": visits,
                "last_visit_at": datetime.datetime.utcnow().isoformat()
            }).execute()

            visits = max(1, min(10, visits))
            send_image(from_number, build_card_url(visits))
            if visits >= 10:
                send_text(from_number, "🎉 Free coffee unlocked! Show this to the barista.")
            else:
                send_text(from_number, f"Thanks for your visit! You now have {visits} stamp(s).")
            return "ok", 200

        # --- REPORT trigger for owner ---
        if text == "REPORT":
            # 1) Total customers (all rows)
            all_rows = sb.table("customers").select("customer_id").execute().data or []
            total_customers = len(all_rows)

            # 2) Active in last 7 days
            seven_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
            active_rows = (
                sb.table("customers")
                  .select("customer_id")
                  .gte("last_visit_at", seven_days_ago)
                  .execute()
                  .data or []
            )
            active_count = len(active_rows)

            # 3) Growth vs total (%)
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

    except Exception as exc:
        print("Webhook error:", exc)

    return "ok", 200

# ------------------------------------------------------------------------------
# =========================== BEGIN: IMAGE DELIVERY BLOCK ==========================
# ------------------------------------------------------------------------------
@app.route("/card/<int:visits>.png")
def card_png(visits: int):
    """Serve the rendered PNG over HTTP."""
    return send_file(render_stamp_card(visits), mimetype="image/png", max_age=300)

@app.route("/card")
def card_query():
    """Convenience endpoint: /card?n=5 -> redirects to /card/5.png"""
    try:
        n = int(request.args.get("n", 0))
    except ValueError:
        n = 0
    return redirect(url_for("card_png", visits=max(0, min(10, n))), code=302)
# ------------------------------------------------------------------------------
# ============================ END: IMAGE DELIVERY BLOCK ==========================
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
