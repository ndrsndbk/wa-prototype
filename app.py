import os
import hmac
import hashlib
import json
import datetime
from typing import Optional, Dict, Any

import requests
from flask import Flask, request, jsonify

# ----------------------------- ENV VARS ---------------------------------------
# WhatsApp (Cloud API)
WABA_API_VERSION = os.getenv("WABA_API_VERSION", "v23.0")
WABA_PHONE_NUMBER_ID = os.getenv("WABA_PHONE_NUMBER_ID", "")  # e.g. 858272234034248
WABA_TOKEN = os.getenv("WABA_TOKEN", "")  # permanent system user token

# Optional webhook verify token (Meta webhook verification handshake)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_me")

# Optional HMAC secret from Meta (x-hub-signature-256). If unset, signature check is skipped.
WEBHOOK_APP_SECRET = os.getenv("WEBHOOK_APP_SECRET", "")

# Dashboard URL (for /REPORT)
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL",
    "https://ndrsndbk.github.io/stamp-card-dashboard/"
)

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://lhbtgjvejsnsrlstwlwl.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxoYnRnanZlanNuc3Jsc3R3bHdsIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NjE4NjMyNCwiZXhwIjoyMDcxNzYyMzI0fQ.6Fc20YQezPUX0LqfybirrHzj9eynstHijTx2gDxKr7M")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("⚠️ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in env!")

# ----------------------------- SUPABASE CLIENT --------------------------------
from supabase import create_client, Client
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ----------------------------- FLASK APP --------------------------------------
app = Flask(__name__)

# ----------------------------- HELPERS ----------------------------------------
def send_whatsapp_message(payload: Dict[str, Any]) -> None:
    """
    Low-level WhatsApp Cloud API sender.
    """
    if not WABA_PHONE_NUMBER_ID or not WABA_TOKEN:
        print("⚠️ Missing WABA_PHONE_NUMBER_ID or WABA_TOKEN.")
        return
    url = f"https://graph.facebook.com/{WABA_API_VERSION}/{WABA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WABA_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            print("❌ WhatsApp send error:", r.status_code, r.text)
        else:
            print("✅ WhatsApp message sent:", r.json())
    except Exception as e:
        print("❌ WhatsApp send exception:", e)

def send_text(to_number: str, text: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text}
    }
    send_whatsapp_message(payload)

def send_image(to_number: str, image_url: str, caption: Optional[str] = None) -> None:
    msg = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "image",
        "image": {"link": image_url}
    }
    if caption:
        msg["image"]["caption"] = caption
    send_whatsapp_message(msg)

def build_card_url(visits: int) -> str:
    """
    Map 0..10 visits to a pre-rendered card asset URL.
    You already host these in Supabase Storage (example shown).
    """
    visits = max(0, min(10, int(visits)))
    # Change this base to your bucket path:
    base = "https://lhbtgjvejsnsrlstwlwl.supabase.co/storage/v1/object/public/cards/v1/Demo_Shop_"
    return f"{base}{visits}.png"

def fetch_single_customer(customer_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = sb.table("customers").select("*").eq("customer_id", customer_id).limit(1).execute()
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print("fetch_single_customer error:", e)
        return None

def verify_meta_signature(raw_body: bytes, signature_256: str) -> bool:
    """
    Validate x-hub-signature-256 from Meta if WEBHOOK_APP_SECRET is set.
    Signature format: 'sha256=...'
    """
    if not WEBHOOK_APP_SECRET:
        return True  # skip if not configured

    try:
        if not signature_256 or not signature_256.startswith("sha256="):
            return False

        sig_hex = signature_256.split("=", 1)[1].strip()
        mac = hmac.new(WEBHOOK_APP_SECRET.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256)
        expected = mac.hexdigest()
        return hmac.compare_digest(sig_hex, expected)
    except Exception as e:
        print("verify_meta_signature error:", e)
        return False

# ---------------------------- STREAK HELPERS ----------------------------------
def _utc_today() -> datetime.date:
    # Use UTC consistently since you store last_visit_at as UTC ISO
    return datetime.datetime.utcnow().date()

def _date_from_ts(ts: Optional[str]) -> Optional[datetime.date]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except Exception:
        return None

def get_and_update_streak(customer_id: str, last_visit_at: Optional[str]) -> int:
    """
    Returns new streak_days after applying today's visit.

    Logic:
      - If last_day == today       -> streak unchanged (no extra day)
      - If last_day == today - 1   -> streak_days += 1
      - Else                       -> streak_days = 1
    Also upserts the row in customer_streaks with last_day=today.
    """
    today = _utc_today()
    yesterday = today - datetime.timedelta(days=1)
    last_day_from_customer = _date_from_ts(last_visit_at)

    # Read current row
    try:
        resp = sb.table("customer_streaks").select("streak_days,last_day").eq("customer_id", customer_id).limit(1).execute()
        rows = getattr(resp, "data", None) or []
    except Exception as e:
        print("streak select error:", e)
        rows = []

    if rows:
        cur = rows[0]
        streak_days = int(cur.get("streak_days", 1))
        last_day = datetime.date.fromisoformat(cur["last_day"]) if cur.get("last_day") else last_day_from_customer
    else:
        streak_days = 1
        last_day = last_day_from_customer

    if last_day == today:
        new_streak = streak_days
    elif last_day == yesterday:
        new_streak = streak_days + 1
    else:
        new_streak = 1

    # Upsert with today's date
    try:
        sb.table("customer_streaks").upsert({
            "customer_id": customer_id,
            "streak_days": new_streak,
            "last_day": str(today),
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        }).execute()
    except Exception as e:
        print("streak upsert error:", e)

    return new_streak

# ----------------------------- ROUTES -----------------------------------------
@app.route("/", methods=["GET"])
def index():
    return "OK", 200

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": datetime.datetime.utcnow().isoformat() + "Z"}), 200

# Webhook verification (Meta)
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403

# Webhook receiver (Meta -> your app)
@app.route("/webhook", methods=["POST"])
def webhook():
    # Optional signature check
    if WEBHOOK_APP_SECRET:
        sig256 = request.headers.get("x-hub-signature-256", "")
        if not verify_meta_signature(request.data, sig256):
            return "invalid signature", 403

    data = request.get_json(silent=True) or {}
    print("Incoming:", json.dumps(data)[:1200], " ...")  # truncated

    try:
        entry = (data.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value", {})
        messages = value.get("messages") or []
    except Exception:
        messages = []

    if not messages:
        return "ok", 200

    for msg in messages:
        # WhatsApp structure
        from_number = msg.get("from")  # E.164 string without '+'
        type_ = msg.get("type")
        text_body = ""
        if type_ == "text":
            text_body = (msg.get("text") or {}).get("body") or ""
        elif type_ == "button":
            text_body = (msg.get("button") or {}).get("text") or ""
        elif type_ == "interactive":
            # handle interactive reply (list, button) if used
            inter = msg.get("interactive") or {}
            # 'button_reply' or 'list_reply'
            button = inter.get("button_reply") or {}
            list_reply = inter.get("list_reply") or {}
            text_body = button.get("title") or list_reply.get("title") or ""

        token = (text_body or "").strip().upper()

        # ---------------- COMMANDS ----------------
        if token in ("HI", "HELLO", "HELP", "START"):
            send_text(
                from_number,
                "👋 *Welcome to The Potential Company Stamp Card!*\n\n"
                "Send *STAMP* each visit to collect your stamps.\n"
                "Send *CARD* to see your current card.\n"
                "Send *REPORT* for the live dashboard link."
            )
            return "ok", 200

        if token in ("CARD", "STATUS"):
            row = fetch_single_customer(from_number)
            visits = int((row or {}).get("number_of_visits", 0))
            visits = max(0, min(10, visits))
            send_image(from_number, build_card_url(visits))
            send_text(from_number, f"Current stamps: *{visits}*")
            return "ok", 200

        if token == "REPORT":
            # Simple example analytics
            try:
                all_rows = sb.table("customers").select("customer_id").execute().data or []
            except Exception as e:
                print("report select total error:", e)
                all_rows = []
            total_customers = len(all_rows)

            seven_days_ago_iso = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat() + "Z"
            try:
                act_rows = (
                    sb.table("customers")
                    .select("customer_id")
                    .gte("last_visit_at", seven_days_ago_iso)
                    .execute()
                    .data
                    or []
                )
            except Exception as e:
                print("report select active error:", e)
                act_rows = []
            active_count = len(act_rows)
            growth_pct = (active_count / total_customers * 100) if total_customers > 0 else 0.0

            report_text = (
                "📊 *Here's your dashboard*\n\n"
                f"Active customers (last 7 days): {active_count}\n"
                f"Growth vs total: {growth_pct:.1f}%\n\n"
                f"Dashboard: {DASHBOARD_URL}"
            )
            send_text(from_number, report_text)
            return "ok", 200

        # ---------------- CORE VISIT LOGIC (STAMP/SALE) ----------------
        if token in ("STAMP", "SALE"):
            # Fetch current row
            row = fetch_single_customer(from_number)
            current_visits = int((row or {}).get("number_of_visits", 0))
            last_visit_at = (row or {}).get("last_visit_at")

            # Compute streak & update streak table to today
            streak_days = get_and_update_streak(from_number, last_visit_at)

            # WhatsApp nudges + possible double-stamp today
            add_stamps = 1
            if streak_days == 2:
                send_text(
                    from_number,
                    "🔥 *You’re on a 2-day streak!* 🔥\n\n"
                    "Keep it going — reach *5 days* and earn an *extra stamp* 🏆"
                )
            elif streak_days == 5:
                add_stamps = 2  # double stamp for the 5th consecutive day
                send_text(
                    from_number,
                    "🏆 *Day 5 Streak!* 🏆\n\n"
                    "You’ve unlocked *double stamps today* — this visit counts as *+2*. Keep the momentum going!\n"
                    "_(Double applies to today’s visit only.)_"
                )

            # Upsert visit tally + timestamp (UTC)
            new_visits = current_visits + add_stamps
            now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            try:
                sb.table("customers").upsert({
                    "customer_id": from_number,
                    "number_of_visits": new_visits,
                    "last_visit_at": now_iso
                }).execute()
            except Exception as e:
                print("customers upsert error:", e)
                send_text(from_number, "⚠️ Sorry, I couldn't record your visit. Please try again.")
                return "ok", 200

            # Send correct card (0..10)
            card_visits = max(0, min(10, new_visits))
            send_image(from_number, build_card_url(card_visits))

            # Reward / acknowledgement
            if new_visits >= 10:
                send_text(from_number, "🎉 *Free coffee unlocked!* Show this to the barista.")
            else:
                delta_txt = "+2 (double!)" if add_stamps == 2 else "+1"
                send_text(
                    from_number,
                    f"Thanks for your visit — {delta_txt}\n"
                    f"Current total: *{new_visits}* stamp(s)."
                )
            return "ok", 200

        # Unknown command -> brief help
        send_text(
            from_number,
            "🤖 I didn’t recognise that.\n\n"
            "• Send *STAMP* to collect a stamp\n"
            "• Send *CARD* to see your card\n"
            "• Send *REPORT* for the dashboard"
        )
        return "ok", 200

    return "ok", 200


# ----------------------------- WSGI ENTRYPOINT -------------------------------
# For gunicorn: `gunicorn --bind 0.0.0.0:$PORT app:app`
if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
