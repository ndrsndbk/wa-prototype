"""
Flask app for WhatsApp Cloud API loyalty card prototype.
- Uses supabase-py (HTTP) instead of psycopg2 (TCP)
"""
import os
from io import BytesIO
from flask import Flask, request, send_file, redirect, url_for
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from supabase import create_client
import datetime

app = Flask(__name__)

VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
HOST_URL        = os.getenv("HOST_URL")

SUPABASE_URL    = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY    = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
try:
    sb.postgrest.schema = "public"
except Exception:
    pass

def _font(size: int, bold: bool = False):
    try:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

COFFEE_ICON_PATH = os.getenv("COFFEE_ICON_PATH", "coffee.png")
try:
    _coffee_src = Image.open(COFFEE_ICON_PATH).convert("L")
except Exception:
    _coffee_src = None

def render_stamp_card(visits: int) -> BytesIO:
    visits = max(0, min(10, int(visits)))
    W, H = 1080, 1080
    bg, fg, red = (0, 0, 0), (255, 255, 255), (220, 53, 69)

    im = Image.new("RGB", (W, H), bg)
    d  = ImageDraw.Draw(im)

    title_f = _font(120, bold=True)
    sub_f   = _font(50, bold=True)
    foot_f  = _font(40, bold=True)
    logo_f  = _font(40, bold=True)

    w_title, _ = d.textbbox((0,0), "COFFEE SHOP", font=title_f)[2:]
    d.text(((W - w_title)//2, 56), "COFFEE SHOP", font=title_f, fill=fg)
    d.text((W//2 - 40, 330), "LOGO", font=logo_f, fill=fg)
    d.text(((W - d.textbbox((0,0),'THANK YOU FOR VISITING TODAY!', font=sub_f)[2])//2, 568), 
           "THANK YOU FOR VISITING TODAY!", font=sub_f, fill=fg)

    CIRCLE_R, ROW_GAP, COL_GAP, GRID_TOP = 72, 180, 180, 600
    left_x = (W - 4 * COL_GAP) // 2
    def circle_bbox(x, y): return [x - CIRCLE_R, y - CIRCLE_R, x + CIRCLE_R, y + CIRCLE_R]

    icon_img = None
    if _coffee_src is not None:
        target = int(CIRCLE_R * 1.15)
        icon_gray = _coffee_src.resize((target, target), Image.LANCZOS)
        red_rgba = Image.new("RGBA", icon_gray.size, red + (255,))
        alpha = ImageOps.invert(icon_gray)
        stamped = Image.new("RGBA", icon_gray.size, (0,0,0,0))
        stamped.paste(red_rgba, (0,0), alpha)
        icon_img = stamped

    def draw_empty(x,y): d.ellipse(circle_bbox(x,y), outline=fg, width=6)
    def draw_stamp(x,y):
        d.ellipse(circle_bbox(x,y), outline=red, width=10)
        d.ellipse(circle_bbox(x,y), outline=red, width=3)
        if icon_img:
            ix, iy = icon_img.size
            im.paste(icon_img, (x-ix//2,y-iy//2), icon_img)

    k=0
    for row in range(2):
        y = GRID_TOP + row*ROW_GAP
        for col in range(5):
            x = left_x + col*COL_GAP
            (draw_stamp if k < visits else draw_empty)(x,y)
            k+=1

    w_foot, _ = d.textbbox((0,0), "10 STAMPS = 1 FREE COFFEE", font=foot_f)[2:]
    d.text(((W - w_foot)//2, H - 74), "10 STAMPS = 1 FREE COFFEE", font=foot_f, fill=fg)

    buf = BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    return buf

def build_card_url(visits: int) -> str:
    base = (HOST_URL or request.url_root).rstrip("/")
    return f"{base}/card/{int(visits)}.png"

def send_text(to: str, body: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID): return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=12)
    except Exception as e:
        print("send_text error:", e)

def send_image(to: str, link: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID): return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": link}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=12)
    except Exception as e:
        print("send_image error:", e)

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
            row = sb.table("customers").select("number_of_visits")                   .eq("customer_id", from_number).maybe_single().execute().data
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
