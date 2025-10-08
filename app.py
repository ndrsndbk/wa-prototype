"""
Flask app for WhatsApp Cloud API loyalty card prototype.
- Uses supabase-py (HTTP) instead of psycopg2 (TCP)
- Image fixes: concentric logo rings, centered "LOGO", dynamic layout
- Stamped circles: solid red + white coffee icon overlay
- REPORT trigger for weekly owner summary
- PROFILE flow: asks 3 questions and stores responses without WhatsApp Flows
"""
import os
import datetime
from io import BytesIO

from flask import Flask, request, send_file, redirect, url_for
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, Image
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

# Import QA helpers (3-question profile flow)
from qa_handler import start_profile_flow, handle_profile_answer

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
    """Render the loyalty stamp card as a PNG and return an in-memory buffer.
    This function is exception-safe: it always returns a valid PNG.
    """
    try:
        visits = max(0, min(10, int(visits)))

        # Canvas + palette
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

        # ---------- Title ----------
        title_text = "COFFEE SHOP"
        tbox = d.textbbox((0, 0), title_text, font=title_f)  # (l, t, r, b)
        t_w, t_h = tbox[2] - tbox[0], tbox[3] - tbox[1]
        y = 56
        d.text(((W - t_w)//2, y), title_text, font=title_f, fill=FG)
        y += t_h + GAP_AFTER_TITLE

        # ---------- Logo (concentric rings + centered "LOGO") ----------
        logo_outer_r, logo_inner_r = 100, 80
        logo_center = (W // 2, y + logo_outer_r)
        for r in (logo_outer_r, logo_inner_r):
            d.ellipse([logo_center[0]-r, logo_center[1]-r,
                       logo_center[0]+r, logo_center[1]+r],
                      outline=FG, width=6)
        logo_text = "LOGO"
        lbox = d.textbbox((0, 0), logo_text, font=logo_f)
        lw, lh = lbox[2] - lbox[0], lbox[3] - lbox[1]
        d.text((logo_center[0] - lw//2, logo_center[1] - lh//2),
               logo_text, font=logo_f, fill=FG)
        y = logo_center[1] + logo_outer_r + GAP_AFTER_LOGO

        # ---------- Thank-you line ----------
        thank_text = "THANK YOU FOR VISITING TODAY!"
        sbox = d.textbbox((0, 0), thank_text, font=sub_f)
        sw, sh = sbox[2] - sbox[0], sbox[3] - sbox[1]
        d.text(((W - sw)//2, y), thank_text, font=sub_f, fill=FG)
        y += sh + GAP_AFTER_THANK

        # ---------- Stamp grid (auto-placed under thank-you) ----------
        CIRCLE_R, ROW_GAP, COL_GAP = 72, 180, 180
        GRID_TOP = y
        left_x   = (W - 4 * COL_GAP) // 2

        def circle_bbox(cx, cy):
            return [cx - CIRCLE_R, cy - CIRCLE_R, cx + CIRCLE_R, cy + CIRCLE_R]

        def draw_empty(cx, cy):
            d.ellipse(circle_bbox(cx, cy), outline=FG, width=6)

        def draw_stamp(cx, cy):
            # Solid red fill via RGBA mini-layer
            stamp_size = CIRCLE_R * 2
            stamp = Image.new("RGBA", (stamp_size, stamp_size), (0, 0, 0, 0))
            sd = ImageDraw.Draw(stamp)
            sd.ellipse([0, 0, stamp_size-1, stamp_size-1], fill=(RED[0], RED[1], RED[2], 255))
            im.paste(stamp, (cx - CIRCLE_R, cy - CIRCLE_R), stamp)

            # Crisp red outline
            d.ellipse(circle_bbox(cx, cy), outline=RED, width=6)

            # Optional white coffee overlay from grayscale icon
            if _coffee_src is not None:
                icon_size = int(CIRCLE_R * 1.2)
                icon_gray = _coffee_src.resize((icon_size, icon_size), Image.LANCZOS)
                white_rgba = Image.new("RGBA", icon_gray.size, (255, 255, 255, 255))
                white_icon = Image.new("RGBA", icon_gray.size, (0, 0, 0, 0))
                white_icon.paste(white_rgba, (0, 0), icon_gray)  # icon as alpha
                im.paste(white_icon, (cx - icon_size//2, cy - icon_size//2), white_icon)

        k = 0
        for row in range(2):
            cy = GRID_TOP + row * ROW_GAP
            for col in range(5):
                cx = left_x + col * COL_GAP
                (draw_stamp if k < visits else draw_empty)(cx, cy)
                k += 1

        # ---------- Footer ----------
        foot_text = "10 STAMPS = 1 FREE COFFEE"
        fbox = d.textbbox((0, 0), foot_text, font=foot_f)
        fw = fbox[2] - fbox[0]
        d.text(((W - fw)//2, H - FOOTER_GAP_BOTTOM), foot_text, font=foot_f, fill=FG)

        # Return PNG buffer
        buf = BytesIO()
        im.save(buf, format="PNG")
        buf.seek(0)
        return buf

    except Exception as e:
        # Emergency fallback image so we never return None
        from PIL import Image as PILImage, ImageDraw as PILDraw
        fallback = PILImage.new("RGB", (1080, 1080), (30, 30, 30))
        dd = PILDraw.Draw(fallback)
        try:
            err_f = _font(48, bold=True)
        except Exception:
            err_f = None
        dd.text((40, 40), "Card render error", fill=(255, 120, 120), font=err_f)
        dd.text((40, 110), str(e)[:600], fill=(220, 220, 220))
        out = BytesIO()
        fallback.save(out, format="PNG")
        out.seek(0)
        return out
# ========================== END: IMAGE RENDERING BLOCK ==========================
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# WhatsApp helpers
# ------------------------------------------------------------------------------
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

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
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

        token = text.strip().upper()

        # PROFILE flow
        if token == "PROFILE":
            start_profile_flow(sb, from_number, send_text)
            return "ok", 200

        if token == "TEST":
            send_image(from_number, build_card_url(0))
            send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
            return "ok", 200

        if token == "SALE":
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

        if token == "REPORT":
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

        # Treat as profile answer if flow is active
        handled = handle_profile_answer(sb, from_number, text, send_text)
        if handled:
            return "ok", 200

    except Exception as exc:
        print("Webhook error:", exc)

    return "ok", 200

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

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
