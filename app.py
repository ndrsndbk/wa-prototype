# app.py
# -------------------------------------------------------------------
# WhatsApp webhook + simple flow engine (SIGNUP / SURVEY / REVIEW)
# Schema is aligned to your existing Supabase table where:
#   public.customers.customer_id (text) = the user's WhatsApp number (E.164)
#
# Flows:
#   SIGNUP: step1 ask birthday -> step2 drink buttons -> send 0-stamp card
#   SURVEY / REVIEW: minimal stubs showing state routing (extend as needed)
#
# Key patterns to keep things robust:
#   - Idempotency: don't reprocess the same WhatsApp message ID
#   - State machine: only the active flow handles a message
#   - Interactive first: route button replies before free text
#   - DB reads/writes are minimal (and cached briefly)
# -------------------------------------------------------------------

import os
import json
import time
import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
import requests
from supabase import create_client, Client  # pip install supabase

# --------------------------- ENV / CONFIG ---------------------------

WHATSAPP_TOKEN  = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN    = os.environ.get("VERIFY_TOKEN", "my-verify-token")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Public image link for a 0-stamp card (fallback provided)
STAMP_CARD_ZERO_URL = os.environ.get(
    "STAMP_CARD_ZERO_URL",
    "https://lhbtgjvejsnsrlstwlwl.supabase.co/storage/v1/object/public/cards/v1/Demo_Shop_0.png"
)

# WhatsApp Graph endpoint for sending messages
WA_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"

# ----------------------------- CLIENTS -----------------------------

app = Flask(__name__)

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

WA_SESSION = requests.Session()
WA_SESSION.headers.update({
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
})

# -------------------------- LIGHTWEIGHT CACHE ----------------------
# Cache conversation_state for a short TTL to reduce DB reads.

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

# -------------------------- IDEMPOTENCY GUARD ----------------------
# processed_events(message_id PK) prevents double-handling the same WA message.

def already_processed(message_id: str) -> bool:
    try:
        r = sb.table("processed_events").select("message_id").eq("message_id", message_id).limit(1).execute()
        rows = getattr(r, "data", None) or []
        return len(rows) > 0
    except Exception as e:
        print("processed check error:", e)
        return False

def mark_processed(message_id: str) -> None:
    try:
        sb.table("processed_events").insert({"message_id": message_id}).execute()
    except Exception:
        # Duplicate primary key raises error the second time; ignore.
        pass

# ---------------------------- STATE HELPERS ------------------------

def get_state(customer_id: str) -> Dict[str, Any]:
    """Return {'active_flow': str|None, 'step': int}."""
    cached = _cache_get(customer_id)
    if cached:
        return {"active_flow": cached.get("active_flow"), "step": cached.get("step", 0)}

    try:
        r = sb.table("conversation_state").select("active_flow, step") \
             .eq("customer_id", customer_id).limit(1).execute()
        rows = getattr(r, "data", None) or []
        state = rows[0] if rows else {"active_flow": None, "step": 0}
        _cache_put(customer_id, state)
        return state
    except Exception as e:
        print("get_state error:", e)
        return {"active_flow": None, "step": 0}

def set_state(customer_id: str, flow: Optional[str], step: int = 0) -> None:
    """Upsert the user's active flow + step."""
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

# ---------------------------- SENDER HELPERS -----------------------

def send_text(to: str, text: str) -> None:
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=15)
        if r.status_code >= 300:
            print("send_text fail:", r.status_code, r.text)
    except Exception as e:
        print("send_text exception:", e)

def send_image(to: str, image_link: str, caption: Optional[str] = None) -> None:
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": image_link}}
    if caption:
        payload["image"]["caption"] = caption
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=20)
        if r.status_code >= 300:
            print("send_image fail:", r.status_code, r.text)
    except Exception as e:
        print("send_image exception:", e)

def send_interactive_buttons(to: str, body_text: str, buttons: List[Dict[str, str]]) -> None:
    """
    buttons: [{"id":"drink_matcha","title":"matcha"}, ...]
    """
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
        if r.status_code >= 300:
            print("send_interactive fail:", r.status_code, r.text)
    except Exception as e:
        print("send_interactive exception:", e)

# --------------------------- CUSTOMER HELPERS ----------------------

def upsert_customer(customer_id: str, profile_name: Optional[str],
                    opt_in_source: Optional[str] = None, locale: Optional[str] = None) -> None:
    """
    Ensure a row exists in public.customers for this WhatsApp number (customer_id).
    Updates last_seen_at and profile_name on each contact.
    """
    now = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        existing = sb.table("customers").select("customer_id").eq("customer_id", customer_id).limit(1).execute()
        if (getattr(existing, "data", None) or []):
            sb.table("customers").update({
                "last_seen_at": now,
                "profile_name": profile_name
            }).eq("customer_id", customer_id).execute()
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
        sb.table("customers").update({
            "birthday": birthday_date.isoformat() if birthday_date else None
        }).eq("customer_id", customer_id).execute()
    except Exception as e:
        print("set_customer_birthday error:", e)

def set_customer_preferred_drink(customer_id: str, drink: str) -> None:
    try:
        sb.table("customers").update({"preferred_drink": drink}).eq("customer_id", customer_id).execute()
    except Exception as e:
        print("set_customer_preferred_drink error:", e)

# ------------------------------ FLOWS ------------------------------
# SURVEY / REVIEW are minimal examples so you can see the pattern.

def start_survey_flow(customer_id: str) -> None:
    send_text(customer_id, "📝 Survey started. How was your visit today? (1–5)")
    set_state(customer_id, "survey", 1)

def handle_survey_reply(customer_id: str, content: str) -> bool:
    st = get_state(customer_id)
    if st.get("active_flow") != "survey":
        return False
    if st.get("step", 0) == 1:
        send_text(customer_id, "Thanks! Any comments to add?")
        set_state(customer_id, "survey", 2)
        return True
    elif st.get("step", 0) == 2:
        send_text(customer_id, "Appreciate the feedback! ✅")
        clear_state(customer_id)
        return True
    return False

def start_review_flow(customer_id: str, wa_name: Optional[str]) -> None:
    send_text(customer_id, f"⭐ Thanks{(' ' + wa_name) if wa_name else ''}! Would you recommend us? (Yes/No)")
    set_state(customer_id, "review", 1)

def handle_review_reply(customer_id: str, content: str) -> bool:
    st = get_state(customer_id)
    if st.get("active_flow") != "review":
        return False
    if st.get("step", 0) == 1:
        txt = (content or "").strip().lower()
        if txt in ("yes", "y", "review_yes"):
            send_text(customer_id, "Amazing! Here’s the Google review link: https://g.page/r/your-link")
        else:
            send_text(customer_id, "No worries—thanks for your time! 🙏")
        clear_state(customer_id)
        return True
    return False

# --------------------------- SIGNUP FLOW ---------------------------
# Step 1 (text): ask birthday (free form; we try a few date formats)
# Step 2 (interactive): buttons for preferred drink
# Then: thank-you + send 0-stamp card image

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

    # Parse + store birthday (optional; we proceed even if parsing fails)
    bday = _parse_birthday(user_text or "")
    set_customer_birthday(customer_id, bday)

    # Ask for preferred drink with interactive buttons
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

# ------------------------------ ROUTES -----------------------------

@app.route("/webhook", methods=["GET"])
def verify():
    """
    Meta webhook verification.
    - Set your VERIFY_TOKEN in the WA App settings and env here.
    """
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403

@app.route("/webhook", methods=["POST"])
def inbound():
    """
    Main webhook handler:
    - Idempotency via processed_events
    - Command router (SIGNUP / SURVEY / REVIEW / etc.)
    - Interactive replies are handled before free text
    - Active flow is the only one allowed to process the message
    """
    data = request.get_json(silent=True, force=True) or {}
    try:
        entry = (data.get("entry") or [])[0]
        change = (entry.get("changes") or [])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []
        contacts = value.get("contacts") or []
        if not messages:
            return "ok", 200

        msg = messages[0]
        msg_id = msg.get("id")

        # Idempotency: bail if processed
        if msg_id and already_processed(msg_id):
            return "ok", 200
        if msg_id:
            mark_processed(msg_id)

        customer_id = msg.get("from")  # WhatsApp sender (E.164, no +)
        wa_name = None
        if contacts:
            wa_name = ((contacts[0].get("profile") or {}).get("name"))

        # Ensure we have a row for this person
        upsert_customer(customer_id, wa_name)

        # Extract message kind
        msg_type = msg.get("type")  # 'text' or 'interactive'
        text_body = ((msg.get("text") or {}).get("body") or "") if msg_type == "text" else ""
        token = text_body.strip().upper()

        reply_id = None
        if msg_type == "interactive":
            interactive = msg.get("interactive") or {}
            if interactive.get("type") == "button_reply":
                reply_id = (interactive.get("button_reply") or {}).get("id")
            elif interactive.get("type") == "list_reply":
                reply_id = (interactive.get("list_reply") or {}).get("id")

        # -------------------- Command routing (priority) --------------------
        if token in ("SIGNUP", "SURVEY", "REVIEW", "GOOGLE", "CLOCKIN", "CHECKIN", "STOP"):
            if token == "SIGNUP":
                start_signup_flow(customer_id, wa_name)
                return "ok", 200
            if token == "SURVEY":
                start_survey_flow(customer_id)
                return "ok", 200
            if token == "REVIEW":
                start_review_flow(customer_id, wa_name)
                return "ok", 200
            if token == "GOOGLE":
                clear_state(customer_id)
                send_text(customer_id, "🌟 Leave a review: https://g.page/r/your-link")
                return "ok", 200
            if token == "CLOCKIN":
                set_state(customer_id, "clockin", 1)
                send_text(customer_id, "🕒 Clock-in recorded.")
                clear_state(customer_id)
                return "ok", 200
            if token == "CHECKIN":
                set_state(customer_id, "checkin", 1)
                send_text(customer_id, "✅ Check-in successful.")
                clear_state(customer_id)
                return "ok", 200
            if token == "STOP":
                clear_state(customer_id)
                send_text(customer_id, "You’ve been unsubscribed from the current flow. 👋")
                return "ok", 200

        # -------------------- Interactive replies first --------------------
        if reply_id:
            # SIGNUP step 2: drink selection
            if handle_signup_interactive_step2(customer_id, reply_id):
                return "ok", 200

            # REVIEW could also use buttons (example mapping)
            if reply_id in ("review_yes", "review_no", "yes_deal", "no_thanks"):
                if handle_review_reply(customer_id, "yes" if reply_id in ("review_yes", "yes_deal") else "no"):
                    return "ok", 200

            # Unknown interactive reply -> ignore gracefully
            return "ok", 200

        # -------------------- Free-text routed by active flow --------------
        st = get_state(customer_id)
        active = st.get("active_flow")
        step = st.get("step", 0)

        # SIGNUP step 1: birthday
        if active == "signup" and step == 1 and msg_type == "text":
            if handle_signup_text_step1(customer_id, text_body):
                return "ok", 200

        if active == "survey":
            if handle_survey_reply(customer_id, text_body):
                return "ok", 200

        if active == "review":
            if handle_review_reply(customer_id, text_body):
                return "ok", 200

        # Nothing to do; keep quiet (or provide a gentle hint if desired)
        # send_text(customer_id, "Reply SIGNUP, SURVEY or REVIEW to continue.")
        return "ok", 200

    except Exception as e:
        # Never throw to Meta; just log and ack.
        print("webhook error:", e, "payload:", json.dumps(data)[:800])
        return "ok", 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "ts": time.time()}), 200

# ------------------------------ MAIN --------------------------------

if __name__ == "__main__":
    # IMPORTANT:
    # You already ran the SQL to create tables/columns.
    # If you ever want to auto-bootstrap tables from here, add an RPC or direct SQL client.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
import os, json, time, datetime
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify, abort
import requests

# ======== ENV ========
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my-verify-token")  # for webhook setup

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")  # service role for upsert

# ======== EXTERNAL CLIENTS ========
from supabase import create_client, Client  # pip install supabase
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Reuse TCP connections to Meta (faster than new requests each time)
WA_SESSION = requests.Session()
WA_SESSION.headers.update({
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
})
WA_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"

# ======== APP ========
app = Flask(__name__)

# ------- minimal in-memory cache for state (optional micro-optimization) -------
_state_cache: Dict[str, Dict[str, Any]] = {}  # {customer_id: {"active_flow":..., "step":..., "ts":...}}
STATE_TTL = 120  # seconds

def _cache_get(customer_id: str) -> Optional[Dict[str, Any]]:
    rec = _state_cache.get(customer_id)
    if not rec:
        return None
    if (time.time() - rec.get("ts", 0)) > STATE_TTL:
        _state_cache.pop(customer_id, None)
        return None
    return rec

def _cache_put(customer_id: str, state: Dict[str, Any]) -> None:
    state = dict(state or {})
    state["ts"] = time.time()
    _state_cache[customer_id] = state

# ======== STATE PERSISTENCE ========
def ensure_tables():
    # idempotent: create if not exists (run once on boot)
    sb.rpc("sql", {
        "query": """
        create table if not exists conversation_state(
          customer_id text primary key,
          active_flow text,
          step int default 0,
          updated_at timestamptz default now()
        );
        create table if not exists processed_events(
          message_id text primary key,
          processed_at timestamptz default now()
        );
        """
    }).execute()

def get_state(customer_id: str) -> Dict[str, Any]:
    # try cache
    c = _cache_get(customer_id)
    if c: return {"active_flow": c.get("active_flow"), "step": c.get("step")}
    try:
        r = sb.table("conversation_state").select("active_flow, step").eq("customer_id", customer_id).limit(1).execute()
        rows = getattr(r, "data", None) or []
        state = rows[0] if rows else {"active_flow": None, "step": 0}
        _cache_put(customer_id, state)
        return state
    except Exception as e:
        print("get_state error:", e)
        return {"active_flow": None, "step": 0}

def set_state(customer_id: str, flow: Optional[str], step: int = 0):
    state = {"customer_id": customer_id, "active_flow": flow, "step": step,
             "updated_at": datetime.datetime.utcnow().isoformat() + "Z"}
    try:
        sb.table("conversation_state").upsert(state).execute()
    except Exception as e:
        print("set_state error:", e)
    _cache_put(customer_id, state)

def clear_state(customer_id: str):
    set_state(customer_id, None, 0)

# ======== IDEMPOTENCY ========
def already_processed(message_id: str) -> bool:
    try:
        r = sb.table("processed_events").select("message_id").eq("message_id", message_id).limit(1).execute()
        rows = getattr(r, "data", None) or []
        return len(rows) > 0
    except Exception as e:
        print("processed check error:", e)
        return False

def mark_processed(message_id: str):
    try:
        sb.table("processed_events").insert({"message_id": message_id}).execute()
    except Exception as e:
        # ignore duplicate key error races
        print("mark processed error:", e)

# ======== SENDERS ========
def send_text(to: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=12)
        if r.status_code >= 300:
            print("send_text fail:", r.status_code, r.text)
    except Exception as e:
        print("send_text exception:", e)

def send_interactive_buttons(to: str, body_text: str, buttons: list):
    """
    buttons: list of dicts: [{"id":"yes_deal","title":"Yes"},{"id":"no_thanks","title":"No"}]
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons]
            }
        }
    }
    try:
        r = WA_SESSION.post(WA_URL, json=payload, timeout=12)
        if r.status_code >= 300:
            print("send_interactive fail:", r.status_code, r.text)
    except Exception as e:
        print("send_interactive exception:", e)

# ======== FLOW STARTERS / HANDLERS (stub the internals you already have) ========
def start_survey_flow(customer_id: str):
    send_text(customer_id, "📝 Survey started. How was your visit today? (1–5)")
    set_state(customer_id, "survey", 1)

def start_review_flow(customer_id: str, wa_name: Optional[str]):
    send_text(customer_id, f"⭐ Thanks{(' ' + wa_name) if wa_name else ''}! Would you recommend us? (Yes/No)")
    set_state(customer_id, "review", 1)

def handle_survey_reply(customer_id: str, content: str) -> bool:
    """Return True if consumed."""
    st = get_state(customer_id)
    if st.get("active_flow") != "survey":
        return False
    step = st.get("step", 0)

    if step == 1:
        # rating
        send_text(customer_id, "Thanks! Any comments to add?")
        set_state(customer_id, "survey", 2)
        return True
    elif step == 2:
        send_text(customer_id, "Appreciate the feedback! ✅")
        clear_state(customer_id)
        return True
    return False

def handle_review_reply(customer_id: str, content: str) -> bool:
    st = get_state(customer_id)
    if st.get("active_flow") != "review":
        return False
    step = st.get("step", 0)

    if step == 1:
        if str(content).strip().lower() in ("yes", "y", "review_yes"):
            send_text(customer_id, "Amazing! Here’s the Google review link: https://g.page/r/your-link")
        else:
            send_text(customer_id, "No worries—thanks for your time! 🙏")
        clear_state(customer_id)
        return True
    return False

# ======== ROUTER ========
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403

@app.route("/webhook", methods=["POST"])
def inbound():
    data = request.get_json(silent=True, force=True) or {}
    try:
        entry = (data.get("entry") or [])[0]
        change = (entry.get("changes") or [])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []
        contacts = value.get("contacts") or []
        if not messages:
            return "ok", 200

        msg = messages[0]
        msg_id = msg.get("id")
        if msg_id and already_processed(msg_id):
            return "ok", 200
        if msg_id:
            mark_processed(msg_id)

        from_number = msg.get("from")  # E.164
        wa_name = None
        if contacts:
            wa_name = ((contacts[0].get("profile") or {}).get("name"))

        msg_type = msg.get("type")
        text_body = ((msg.get("text") or {}).get("body") or "") if msg_type == "text" else ""
        token = text_body.strip().upper()

        # Interactive replies (button or list)
        reply_id = None
        interactive = msg.get("interactive") if msg_type == "interactive" else None
        if interactive:
            if interactive.get("type") == "button_reply":
                reply_id = (interactive.get("button_reply") or {}).get("id")
            elif interactive.get("type") == "list_reply":
                reply_id = (interactive.get("list_reply") or {}).get("id")

        # === Commands take priority ===
        if token in ("SURVEY", "REVIEW", "GOOGLE", "CLOCKIN", "CHECKIN", "STOP"):
            if token == "SURVEY":
                start_survey_flow(from_number)
                return "ok", 200
            if token == "REVIEW":
                start_review_flow(from_number, wa_name)
                return "ok", 200
            if token == "GOOGLE":
                clear_state(from_number)
                send_text(from_number, "🌟 Leave a review: https://g.page/r/your-link")
                return "ok", 200
            if token == "CLOCKIN":
                set_state(from_number, "clockin", 1)
                send_text(from_number, "🕒 Clock-in recorded.")
                clear_state(from_number)
                return "ok", 200
            if token == "CHECKIN":
                set_state(from_number, "checkin", 1)
                send_text(from_number, "✅ Check-in successful.")
                clear_state(from_number)
                return "ok", 200
            if token == "STOP":
                clear_state(from_number)
                send_text(from_number, "You’ve been unsubscribed from the current flow. 👋")
                return "ok", 200

        # === Route interactive reply by active flow & reply_id ===
        if reply_id:
            # Map friendly IDs to text if needed
            if reply_id in ("review_yes", "yes_deal"):
                if handle_review_reply(from_number, "yes"):
                    return "ok", 200
            elif reply_id in ("review_no", "no_thanks"):
                if handle_review_reply(from_number, "no"):
                    return "ok", 200

            # Survey list/button choices could be handled here
            if handle_survey_reply(from_number, reply_id):
                return "ok", 200

            return "ok", 200  # don't leak to other handlers

        # === Plain text goes ONLY to current active flow ===
        state = get_state(from_number)
        active = state.get("active_flow")

        if active == "review":
            if handle_review_reply(from_number, text_body):
                return "ok", 200

        if active == "survey":
            if handle_survey_reply(from_number, text_body):
                return "ok", 200

        # No active flow – ignore or send a gentle helper
        # (Comment out to be fully silent)
        # send_text(from_number, "Reply SURVEY or REVIEW to begin.")
        return "ok", 200

    except Exception as e:
        print("webhook error:", e, "payload:", json.dumps(data)[:800])
        return "ok", 200

# Healthcheck
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "ts": time.time()}), 200


if __name__ == "__main__":
    ensure_tables()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
