import os
import hmac
import hashlib
import json
import datetime
from typing import Optional, Dict, Any, Tuple

import requests
from flask import Flask, request, jsonify

# ----------------------------- ENV VARS ---------------------------------------
# Centralised configuration for environment variables.
# This works with BOTH your older naming (WABA_*, SUPABASE_SERVICE_KEY)
# and the Render dashboard keys you showed in the screenshot:
#   PHONE_NUMBER_ID, WHATSAPP_TOKEN, WHATSAPP_VERIFY_TOKEN,
#   SUPABASE_SERVICE_ROLE_KEY, etc.

# ---------------- WhatsApp (Cloud API) ----------------
# Version of the Graph API to call.
WABA_API_VERSION = os.getenv("WABA_API_VERSION", "v23.0")

# Phone number ID used in the WhatsApp Cloud API URL.
# Priority: WABA_PHONE_NUMBER_ID (new) → PHONE_NUMBER_ID (Render screenshot).
WABA_PHONE_NUMBER_ID = os.getenv("WABA_PHONE_NUMBER_ID") or os.getenv("PHONE_NUMBER_ID", "")

# Permanent system-user access token.
# Priority: WABA_TOKEN (new) → WHATSAPP_TOKEN (Render screenshot).
WABA_TOKEN = os.getenv("WABA_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")

# Webhook verification token used during the Meta webhook "GET" handshake.
# Priority: VERIFY_TOKEN (new) → WHATSAPP_VERIFY_TOKEN (Render screenshot).
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN", "myverifytoken")

# Optional HMAC secret from Meta (x-hub-signature-256). If unset, signature check is skipped.
WEBHOOK_APP_SECRET = os.getenv("WEBHOOK_APP_SECRET", "")

# ---------------- Dashboard URL (for REPORT shortcut) ----------------
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL",
    "https://ndrsndbk.github.io/stamp-card-dashboard/"
)

# ---------------- Supabase ----------------
# Supabase project URL and service key.
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://lhbtgjvejsnsrlstwlwl.supabase.co")

# Priority: SUPABASE_SERVICE_KEY (old naming) → SUPABASE_SERVICE_ROLE_KEY (Render screenshot).
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("⚠️ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY / SUPABASE_SERVICE_ROLE_KEY in env!")


def env_diagnostics() -> None:
    """
    Print a one-shot summary of which critical env vars are loaded.
    Values are not printed (only booleans) so secrets never leak.
    """
    print("\n🔍 ENV DIAGNOSTICS (on boot)")
    print("-------------------------------------------")
    print(f"WABA_PHONE_NUMBER_ID loaded:  {bool(WABA_PHONE_NUMBER_ID)} (or PHONE_NUMBER_ID)")
    print(f"WABA_TOKEN loaded:            {bool(WABA_TOKEN)} (or WHATSAPP_TOKEN)")
    print(f"VERIFY_TOKEN loaded:          {bool(VERIFY_TOKEN)} (or WHATSAPP_VERIFY_TOKEN)")
    print(f"SUPABASE_URL loaded:          {bool(SUPABASE_URL)}")
    print(f"SUPABASE_SERVICE_KEY loaded:  {bool(SUPABASE_SERVICE_KEY)} (or SUPABASE_SERVICE_ROLE_KEY)")
    print(f"WEBHOOK_APP_SECRET set:       {bool(WEBHOOK_APP_SECRET)}")
    print(f"DASHBOARD_URL:                {DASHBOARD_URL}")
    print("-------------------------------------------\n")


env_diagnostics()

# ----------------------------- SUPABASE CLIENT --------------------------------
from supabase import create_client, Client

# Service-role client (server-side only; never expose this key in frontend)
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ----------------------------- FLASK APP --------------------------------------
app = Flask(__name__)

# ----------------------------- HELPERS ----------------------------------------
def send_whatsapp_message(payload: Dict[str, Any]) -> None:
    """
    Low-level WhatsApp Cloud API sender.
    Expects a fully formed payload (text, media, template, etc.).
    """
    if not WABA_PHONE_NUMBER_ID or not WABA_TOKEN:
        print("⚠️ Missing WABA_PHONE_NUMBER_ID or WABA_TOKEN.")
        return

    url = f"https://graph.facebook.com/{WABA_API_VERSION}/{WABA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WABA_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            print("❌ WhatsApp send error:", r.status_code, r.text[:500])
        else:
            print("✅ WhatsApp send ok:", r.json())
    except Exception as e:
        print("send_whatsapp_message exception:", e)


def send_text(to_number: str, body: str) -> None:
    """
    Convenience wrapper to send a plain text WhatsApp message.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body},
    }
    send_whatsapp_message(payload)


def build_stamp_card_url(visits: int) -> str:
    """
    Map a customer's visit count → correct static PNG URL.
    The PNGs are pre-rendered and stored in Supabase storage:
      Demo_Shop_0.png ... Demo_Shop_10.png
    """
    if visits < 0:
        visits = 0
    if visits > 10:
        visits = 10
    base = "https://lhbtgjvejsnsrlstwlwl.supabase.co/storage/v1/object/public/cards/v1/Demo_Shop_"
    return f"{base}{visits}.png"


def fetch_single_customer(customer_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch one row from the `customers` table by `customer_id`.
    Returns None if the customer does not exist.
    """
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


# ----------------------------- STREAK LOGIC -----------------------------------
def get_and_update_streak(customer_id: str) -> Tuple[int, bool, bool]:
    """
    Streak helper backed by the `customer_streaks` table.

    Table schema used:
      - customer_id (text, PK-ish)
      - streak_days (int)
      - last_day (date)
      - updated_at (timestamptz)  <-- maintained for debugging / audit

    Behaviour:
      - If last_day == today               → streak stays the same
        (multiple stamps in the same day do NOT increase the streak).
      - If last_day == yesterday           → streak_days += 1
      - Otherwise (gap / no row)          → streak_days = 1

    Returns:
      (new_streak_days, hit_2_today, hit_5_today)
      where `hit_2_today` is True only when the streak *first* reaches 2,
      and `hit_5_today` is True only when the streak *first* reaches 5.
    """

    today = datetime.date.today()

    # --- Load existing row (if any) ---
    try:
        resp = (
            sb.table("customer_streaks")
            .select("*")
            .eq("customer_id", customer_id)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        row = rows[0] if rows else None
    except Exception as e:
        print("get_and_update_streak: select error:", e)
        row = None

    prev_streak = row.get("streak_days", 0) if row else 0

    # Parse last_day to date
    last_day = None
    if row and row.get("last_day"):
        try:
            last_day = datetime.date.fromisoformat(str(row["last_day"]))
        except Exception:
            last_day = None

    # --- Compute new streak based on last_day ---
    if last_day == today:
        # Already visited today → streak unchanged
        new_streak = prev_streak or 1
    elif last_day == today - datetime.timedelta(days=1):
        # Consecutive day → increment streak
        new_streak = (prev_streak or 1) + 1
    else:
        # Break in streak or first visit
        new_streak = 1

    # Flags for messages – only when we *first* reach 2 or 5
    hit_2_today = (new_streak == 2 and prev_streak < 2)
    hit_5_today = (new_streak == 5 and prev_streak < 5)

    # --- Upsert new streak state ---
    try:
        sb.table("customer_streaks").upsert(
            {
                "customer_id": customer_id,
                "last_day": today.isoformat(),
                "streak_days": new_streak,
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            }
        ).execute()
    except Exception as e:
        print("get_and_update_streak: upsert error:", e)

    return new_streak, hit_2_today, hit_5_today


# ----------------------------- ROUTES -----------------------------------------
@app.route("/", methods=["GET"])
def health():
    """
    Simple healthcheck endpoint so that Render / uptime monitors can see the app is alive.
    """
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Meta / WhatsApp Webhook verification (GET).
    This is called once when you configure the webhook URL in WhatsApp Manager.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified.")
        return challenge, 200
    else:
        print("❌ Webhook verification failed.")
        return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Main webhook handler for incoming WhatsApp messages.
    Handles:
      - STAMP
      - CARD
      - REPORT
      - any other text → short help menu
    Also contains the 2-day / 5-day streak logic.
    """

    # Optional signature check
    if WEBHOOK_APP_SECRET:
        sig256 = request.headers.get("x-hub-signature-256", "")
        if not verify_meta_signature(request.data, sig256):
            return "invalid signature", 403

    data = request.get_json(silent=True) or {}
    print("Incoming:", json.dumps(data)[:1200], " ...")  # truncated for logs

    # Defensive extraction of messages
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
        from_number = msg.get("from")  # sender's WA ID (phone, no '+')
        type_ = msg.get("type")

        # Normalise the text body across text / button / interactive
        text_body = ""
        if type_ == "text":
            text_body = (msg.get("text") or {}).get("body") or ""
        elif type_ == "button":
            text_body = (msg.get("button") or {}).get("text") or ""
        elif type_ == "interactive":
            interactive = msg.get("interactive") or {}
            text_body = (
                (interactive.get("button_reply") or {}).get("title")
                or (interactive.get("list_reply") or {}).get("title")
                or ""
            )

        text_lower = text_body.strip().upper()

        # -------------------- STAMP FLOW --------------------------------------
        if text_lower == "STAMP":
            # Fetch existing customer row, if any
            customer = fetch_single_customer(from_number)
            current_visits = customer.get("number_of_visits", 0) if customer else 0

            # Compute streak & update `customer_streaks` to today
            streak_days, hit_2_today, hit_5_today = get_and_update_streak(from_number)

            # Decide how many stamps to add today
            add_stamps = 1

            # Send streak encouragement messages only when the streak *hits*
            # that value, not on every subsequent STAMP.
            if hit_2_today:
                send_text(
                    from_number,
                    "🔥 *You’re on a 2-day streak!* 🔥\n\n"
                    "Keep it going — reach *5 days* and earn an *extra stamp* 🏆"
                )

            if hit_5_today:
                add_stamps = 2  # double stamp for the 5th consecutive day
                send_text(
                    from_number,
                    "🏆 *Day 5 Streak!* 🏆\n\n"
                    "You’ve unlocked *double stamps today* — this visit counts as *+2*. "
                    "Keep the momentum going!\n"
                    "_(Double applies to today’s visit only.)_"
                )

            # Upsert visit tally + timestamp (UTC)
            new_visits = current_visits + add_stamps
            now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            try:
                sb.table("customers").upsert(
                    {
                        "customer_id": from_number,
                        "number_of_visits": new_visits,
                        "last_visit_at": now_iso,
                    }
                ).execute()
            except Exception as e:
                print("STAMP upsert error:", e)

            # Send updated card image
            media_url = build_stamp_card_url(new_visits)
            payload = {
                "messaging_product": "whatsapp",
                "to": from_number,
                "type": "image",
                "image": {
                    "link": media_url,
                    "caption": (
                        f"You now have *{new_visits}* stamp(s). "
                        "10 stamps = 1 free coffee ☕"
                    ),
                },
            }
            send_whatsapp_message(payload)
            continue

        # -------------------- CARD FLOW ---------------------------------------
        if text_lower == "CARD":
            customer = fetch_single_customer(from_number)
            visits = customer.get("number_of_visits", 0) if customer else 0
            media_url = build_stamp_card_url(visits)

            payload = {
                "messaging_product": "whatsapp",
                "to": from_number,
                "type": "image",
                "image": {
                    "link": media_url,
                    "caption": (
                        f"You currently have *{visits}* stamp(s). "
                        "10 stamps = 1 free coffee ☕"
                    ),
                },
            }
            send_whatsapp_message(payload)
            continue

        # -------------------- REPORT FLOW -------------------------------------
        if text_lower == "REPORT":
            # Simple text with a link to your static dashboard
            send_text(
                from_number,
                "📊 *Here’s your dashboard*\n\n"
                f"{DASHBOARD_URL}\n\n"
                "You can see:\n"
                "• Total cards\n"
                "• Stamps issued & redeemed\n"
                "• Redemption rate & ROI\n"
            )
            continue

        # -------------------- HELP / DEFAULT ----------------------------------
        send_text(
            from_number,
            "👋 *Welcome to the Demo Coffee Shop stamp card!*\n\n"
            "You can send:\n"
            "• *STAMP* – log a visit and collect a stamp\n"
            "• *CARD* – see your current stamp card\n"
            "• *REPORT* – open the live dashboard\n"
        )

    return "ok", 200


# ----------------------------- WSGI ENTRYPOINT -------------------------------
# For gunicorn on Render: `gunicorn --bind 0.0.0.0:$PORT app:app`
if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
