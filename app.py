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
