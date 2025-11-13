# app.py
# -------------------------------------------------------------------
# WhatsApp Cloud API loyalty prototype (merged):
# - Preserves original base commands (TEST, STAMP/SALE, SURVEY, REVIEW, GOOGLE,
#   CLOCKIN, CHECKIN, REPORT) and Supabase-CDN stamp cards.
# - Adds SIGNUP flow (birthday -> drink buttons -> 0-stamp card).
# - Adds a light state machine and idempotency to prevent cross-flow triggers.
#
# DB schema used:
#   public.customers(customer_id text, number_of_visits bigint, last_visit_at timestamptz, ...)
# Also uses:
#   public.conversation_state(customer_id text pk, active_flow text, step int, updated_at timestamptz)
#   public.processed_events(message_id text pk, processed_at timestamptz)
# -------------------------------------------------------------------

import os
import json
import time
import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify, send_file, redirect, url_for, make_response
import requests
from supabase import create_client, Client

# ---- Optional feature modules (kept from your base) ---------------------------
try:
    # Existing survey/profile flow handlers
    from qa_handler import start_profile_flow, handle_profile_answer
except Exception:
    def start_profile_flow(*args, **kwargs):
        pass
    def handle_profile_answer(*args, **kwargs):
        return False

try:
    from clockin import handle_clockin
except Exception:
    handle_clockin = None

try:
    from checkin import handle_checkin
except Exception:
    handle_checkin = None

try:
    # Review flows (including reply handler)
    from review import start_review_flow, send_google_review_link, handle_review_reply
except Exception:
    start_review_flow = None
    send_google_review_link = None
    handle_review_reply = None

try:
    # Legacy dynamic renderer (safe if present)
    from card_svg import render_card_png
except Exception:
    render_card_png = None

# --------------------------- ENV / CONFIG -------------------------------------

VERIFY_TOKEN     = os.getenv("VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN   = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID  = os.getenv("PHONE_NUMBER_ID", "")

# Static card hosting (defaults based on your Supabase Storage layout)
CARDS_BASE_URL   = os.getenv("CARDS_BASE_URL", "https://lhbtgjvejsnsrlstwlwl.supabase.co/storage/v1/object/public/cards")
CARDS_VERSION    = os.getenv("CARDS_VERSION", "v1")
CARD_PREFIX      = os.getenv("CARD_PREFIX", "Demo_Shop_")  # e.g., Demo_Shop_0.png

STAMP_CARD_ZERO_URL = os.getenv(
    "STAMP_CARD_ZERO_URL",
    f"{CARDS_BASE_URL}/{CARDS_VERSION}/{CARD_PREFIX}0.png"
)

SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY     = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/KEY")

# WhatsApp Graph (use v23)
WA_URL           = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"

# ----------------------------- CLIENTS ----------------------------------------

app: Flask = Flask(__name__)
sb: Client  = create_client(SUPABASE_URL, SUPABASE_KEY)

WA_SESSION = requests.Session()
WA_SESSION.headers.update({
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
})

# --------------------------- CARD URL HELPER ----------------------------------

def build_card_url(visits: int) -> str:
    """
    Return the CDN URL to a pre-rendered, immutable PNG for the given visit count.
    Files must exist at: {CARDS_BASE_URL}/{CARDS_VERSION}/{CARD_PREFIX}{n}.png
    """
    v = max(0, min(10, int(visits)))
    return f"{CARDS_BASE_URL}/{CARDS_VERSION}/{CARD_PREFIX}{v}.png"

# -------------------------- IDEMPOTENCY GUARD ---------------------------------

def already_processed(message_id: Optional[str]) -> bool:
    if not message_id:
        return False
    try:
        r = sb.table("processed_events").select("message_id").eq("message_id", message_id).limit(1).execute()
        rows = getattr(r, "data", None) or []
        return len(rows) > 0
    except Exception as e:
        print("processed check error:", e)
        return False

def mark_processed(message_id: Optional[str]) -> None:
    if not message_id:
        return
    try:
        sb.table("processed_events").insert({"message_id": message_id}).execute()
    except Exception:
        pass

# ------------------------------ STATE CACHE -----------------------------------

_state_cache: Dict[str, Dict[str, Any]] = {}
STATE_TTL_SECS = 120

def _cache_get(customer_id: str) -> Optional[Dict[str, Any]]:
    rec = _state_cache.get(customer_id)
    if not rec:
        return None
    if (time.time() - rec.get("ts", 0)) > STATE_TTL_SECS:
        _state_cache.pop(customer_id, None)
        return None
    return rec

def _cache_put(customer_id: str, state: Dict[str, Any]) -> None:
    s = dict(state or {})
    s["ts"] = time.time()
    _state_cache[customer_id] = s

def get_state(customer_id: str) -> Dict[str, Any]:
    c = _cache_get(customer_id)
    if c:
        return {"active_flow": c.get("active_flow"), "step": c.get("step", 0)}
    try:
        r = sb.table("conversation_state").select("active_flow, step").eq("customer_id", customer_id).limit(1).execute()
        rows = getattr(r, "data", None) or []
        state = rows[0] if rows else {"active_flow": None, "step": 0}
        _cache_put(customer_id, state)
        return state
    except Exception as e:
        print("get_state error:", e)
        return {"active_flow": None, "step": 0}

def set_state(customer_id: str, flow: Optional[str], step: int = 0) -> None:
    state = {
        "customer_id": customer_id,
        "active_flow": flow,
        "step": step,
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z"
    }
    try:
        sb.table("conversation_state").upsert(state).execute()
    except Exception as e:
        print("set_state error:", e)
    _cache_put(customer_id, state)

def clear_state(customer_id: str) -> None:
    set_state(customer_id, None, 0)

# ---------------------------- SEND HELPERS ------------------------------------

def send_text(to: str, body: str) -> None:
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=15)
        print("[WA SEND text]", r.status_code, r.text)
    except Exception as e:
        print("send_text exception:", e)

def send_image(to: str, link: str, caption: Optional[str] = None) -> None:
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": link}}
    if caption:
        payload["image"]["caption"] = caption
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=20)
        print("[WA SEND image]", r.status_code, r.text)
    except Exception as e:
        print("send_image exception:", e)

def send_interactive_buttons(to: str, body_text: str, buttons: List[Dict[str, str]]) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                for b in buttons
            ]}
        }
    }
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=15)
        print("[WA SEND interactive]", r.status_code, r.text)
    except Exception as e:
        print("send_interactive exception:", e)

# ---------------------------- CUSTOMER HELPERS --------------------------------

def fetch_single_customer(customer_id: str) -> Optional[Dict[str, Any]]:
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

def upsert_customer(customer_id: str, profile_name: Optional[str], opt_in_source: Optional[str] = None, locale: Optional[str] = None) -> None:
    now = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        existing = sb.table("customers").select("customer_id").eq("customer_id", customer_id).limit(1).execute()
        if (getattr(existing, "data", None) or []):
            sb.table("customers").update({"last_seen_at": now, "profile_name": profile_name}).eq("customer_id", customer_id).execute()
        else:
            sb.table("customers").insert({
                "customer_id": customer_id,
                "profile_name": profile_name,
                "opt_in_source": opt_in_source,
                "locale": locale,
                "created_at": now,
                "last_seen_at": now
            }).execute()
    except Exception as e:
        print("upsert_customer error:", e)

def set_customer_birthday(customer_id: str, birthday_date: Optional[datetime.date]) -> None:
    try:
        sb.table("customers").update({"birthday": birthday_date.isoformat() if birthday_date else None}).eq("customer_id", customer_id).execute()
    except Exception as e:
        print("set_customer_birthday error:", e)

def set_customer_preferred_drink(customer_id: str, drink: str) -> None:
    try:
        sb.table("customers").update({"preferred_drink": drink}).eq("customer_id", customer_id).execute()
    except Exception as e:
        print("set_customer_preferred_drink error:", e)

# ------------------------------ SIGNUP FLOW -----------------------------------

def _parse_birthday(raw: str) -> Optional[datetime.date]:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except Exception:
            continue
    return None

def start_signup_flow(customer_id: str, wa_name: Optional[str]) -> None:
    wave = "👋"
    welcome = (
        f"Welcome{',' if wa_name else ''}{(' ' + wa_name) if wa_name else ''} {wave} "
        "Answer 2 quick questions to signup for the stamp card:\n\n"
        "First, when is your birthday?\n_You get a free drink on your birthday_"
    )
    send_text(customer_id, welcome)
    set_state(customer_id, "signup", 1)

def handle_signup_text_step1(customer_id: str, user_text: str) -> bool:
    st = get_state(customer_id)
    if st.get("active_flow") != "signup" or st.get("step", 0) != 1:
        return False
    bday = _parse_birthday(user_text or "")
    set_customer_birthday(customer_id, bday)
    send_interactive_buttons(
        customer_id,
        "Last question: What's your preferred drink?",
        [
            {"id": "drink_matcha",     "title": "matcha"},
            {"id": "drink_americano",  "title": "americano"},
            {"id": "drink_cappuccino", "title": "cappuccino"},
        ]
    )
    set_state(customer_id, "signup", 2)
    return True

def handle_signup_interactive_step2(customer_id: str, reply_id: str) -> bool:
    st = get_state(customer_id)
    if st.get("active_flow") != "signup" or st.get("step", 0) != 2:
        return False
    mapping = {
        "drink_matcha": "matcha",
        "drink_americano": "americano",
        "drink_cappuccino": "cappuccino",
    }
    choice = mapping.get(reply_id)
    if not choice:
        return False
    set_customer_preferred_drink(customer_id, choice)
    send_text(customer_id, "Thanks! Here's your stamp card 🎉")
    send_image(customer_id, STAMP_CARD_ZERO_URL)
    clear_state(customer_id)
    return True

# ------------------------------- ROUTES ---------------------------------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": time.time()}), 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "forbidden", 403

@app.route("/webhook", methods=["POST"])
def inbound():
    data = request.get_json(silent=True) or {}
    try:
        entry   = (data.get("entry") or [None])[0] or {}
        changes = (entry.get("changes") or [None])[0] or {}
        value   = changes.get("value") or {}
        message = (value.get("messages") or [None])[0]

        if not message:
            return "ignored", 200

        msg_id       = message.get("id")
        from_number  = message.get("from")                  # E.164 (no '+')
        contacts     = value.get("contacts") or []
        wa_name      = (contacts[0].get("profile") or {}).get("name") if contacts else None

        # Idempotency
        if already_processed(msg_id):
            return "ok", 200
        mark_processed(msg_id)

        # Ensure customer exists/updated
        upsert_customer(from_number, wa_name)

        # Determine message type
        msg_type   = message.get("type")  # 'text', 'interactive', ...
        text_raw   = ((message.get("text") or {}).get("body") or "") if msg_type == "text" else ""
        token      = text_raw.strip().upper()

        # Interactive reply id (for button/list)
        reply_id = None
        if msg_type == "interactive":
            interactive = message.get("interactive") or {}
            if interactive.get("type") == "button_reply":
                reply_id = (interactive.get("button_reply") or {}).get("id")
            elif interactive.get("type") == "list_reply":
                reply_id = (interactive.get("list_reply") or {}).get("id")

        # ---------------- Commands (preserved + extended) -------------------
        if token in ("TEST", "STAMP", "SALE", "SURVEY", "REVIEW", "GOOGLE", "CLOCKIN", "CHECKIN", "REPORT", "SIGNUP", "STOP"):
            if token == "TEST":
                send_image(from_number, build_card_url(0))
                send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
                return "ok", 200

            if token in ("STAMP", "SALE"):
                row = fetch_single_customer(from_number)
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

            if token == "REVIEW":
                if start_review_flow:
                    start_review_flow(sb, from_number, send_text, wa_name)
                else:
                    start_profile_flow(sb, from_number, send_text)
                    send_text(from_number, "ℹ️ REVIEW flow is in preview — using the standard survey for now.")
                return "ok", 200

            if token == "GOOGLE":
                if send_google_review_link:
                    send_google_review_link(sb, from_number, send_text, wa_name)
                else:
                    send_text(from_number, "🌟 GOOGLE review module coming soon.")
                return "ok", 200

            if token == "CLOCKIN":
                if handle_clockin:
                    handle_clockin(sb, from_number, send_text, wa_name)
                else:
                    send_text(from_number, "🕒 CLOCKIN coming soon — module not deployed yet.")
                return "ok", 200

            if token == "CHECKIN":
                if handle_checkin:
                    handle_checkin(sb, from_number, send_text, wa_name)
                else:
                    send_text(from_number, "✅ CHECKIN coming soon — module not deployed yet.")
                return "ok", 200

            if token == "REPORT":
                all_rows = sb.table("customers").select("customer_id").execute().data or []
                total_customers = len(all_rows)
                seven_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
                active_rows = (
                    sb.table("customers").select("customer_id").gte("last_visit_at", seven_days_ago).execute().data or []
                )
                active_count = len(active_rows)
                growth_pct = (active_count / total_customers * 100) if total_customers > 0 else 0.0
                report_text = (
                    "📊 *Here's your dashboard*\n\n"
                    f"Active customers (last 7 days): {active_count}\n"
                    f"Growth vs total: {growth_pct:.1f}%\n\n"
                    Dashboard: "https://ndrsndbk.github.io/stamp-card-dashboard/"
                )
                send_text(from_number, report_text)
                return "ok", 200

            if token == "SIGNUP":
                start_signup_flow(from_number, wa_name)
                return "ok", 200

            if token == "STOP":
                clear_state(from_number)
                send_text(from_number, "You’ve been unsubscribed from the current flow. 👋")
                return "ok", 200

        # ---------------- Interactive first (flows) -------------------------
        if reply_id:
            # SIGNUP step 2: drink selection
            if handle_signup_interactive_step2(from_number, reply_id):
                return "ok", 200

            # REVIEW reply via buttons (if implemented)
            if reply_id in ("review_yes", "review_no", "yes_deal", "no_thanks") and handle_review_reply:
                if handle_review_reply(sb, from_number, "yes" if reply_id in ("review_yes","yes_deal") else "no", send_text):
                    return "ok", 200

            return "ok", 200

        # ---------------- Free-text routed by active flow -------------------
        st = get_state(from_number)
        if st.get("active_flow") == "signup" and st.get("step", 0) == 1 and msg_type == "text":
            if handle_signup_text_step1(from_number, text_raw):
                return "ok", 200

        # REVIEW text reply (if module present)
        if handle_review_reply and msg_type == "text":
            if handle_review_reply(sb, from_number, text_raw, send_text):
                return "ok", 200

        # SURVEY/Profile flow text handler (existing)
        if handle_profile_answer(sb, from_number, text_raw, send_text):
            return "ok", 200

    except Exception as exc:
        print("Webhook error:", exc, "| payload:", json.dumps(data)[:800])

    return "ok", 200

# ----------------------- Legacy dynamic card endpoints -------------------------

@app.route("/card/<int:visits>.png")
def card_png(visits: int):
    if not render_card_png:
        return "Renderer not deployed", 404
    buf = render_card_png(visits)
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

# -------------------------------- MAIN ----------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
