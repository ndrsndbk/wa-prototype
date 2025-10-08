"""
Flask app for WhatsApp Cloud API loyalty card prototype.
- SALE: record a visit + send updated card
- SURVEY: start 3-question onboarding flow (qa_handler.py)
- REPORT: owner summary
- Cache-busting for WhatsApp image scraper
"""

import os
import time
import datetime
from flask import Flask, request, send_file, redirect, url_for, make_response
import requests
from supabase import create_client

from card_renderer import render_stamp_card  # <— NEW: split renderer here
from qa_handler import start_profile_flow, handle_profile_answer

app = Flask(__name__)

# ---------------- env ----------------
VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
HOST_URL        = os.getenv("HOST_URL")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
try:
    sb.postgrest.schema = "public"
except Exception:
    pass

# ---------------- helpers ----------------
def build_card_url(visits: int) -> str:
    """Cache-busted URL so WA/FB fetch a fresh PNG."""
    base = (HOST_URL or request.url_root).rstrip("/")
    v = int(time.time() // 10)  # 10s bucket
    return f"{base}/card/{int(visits)}.png?v={v}"

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

# ---------------- routes ----------------
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

        if token == "TEST":
            send_image(from_number, build_card_url(0))
            send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
            return "ok", 200

        if token == "SALE":
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
            start_profile_flow(sb, from_number, send_text)
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

        # Not a command: treat as potential SURVEY answer
        handled = handle_profile_answer(sb, from_number, text, send_text)
        if handled:
            return "ok", 200

    except Exception as exc:
        print("Webhook error:", exc)

    return "ok", 200

@app.route("/card/<int:visits>.png")
def card_png(visits: int):
    buf = render_stamp_card(visits)
    resp = make_response(send_file(buf, mimetype="image/png"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

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
