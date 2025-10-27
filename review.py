# review.py
import datetime

PROMPT_TEMPLATE_WITH_NAME = (
    "Thanks for checking in today {name}! "
    "How was your experience today? If there is anything we can do to improve, "
    "please let us know by responding"
)
PROMPT_TEMPLATE_NO_NAME = (
    "Thanks for checking in today! "
    "How was your experience today? If there is anything we can do to improve, "
    "please let us know by responding"
)

GOOGLE_TEMPLATE_WITH_NAME = (
    "Thanks for making your 3rd visit today {name}! 🌟\n"
    "We value your feedback and would appreciate it if you gave us a review on Google:"
)
GOOGLE_TEMPLATE_NO_NAME = (
    "Thanks for making your 3rd visit today! 🌟\n"
    "We value your feedback and would appreciate it if you gave us a review on Google:"
)

GOOGLE_REVIEW_URL = "https://search.google.com/local/writereview?placeid=ChIJj61dQgK6j4AR4GeTYWZsKWw"


def _safe_now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _fetch_name_if_missing(sb, from_number: str):
    """
    Try to discover a display name if not provided:
      1) members.name  (gym/customer check-ins)
      2) staff.name    (team clock-ins)
    Returns None if not found.
    """
    # Try members
    try:
        r = (
            sb.table("members")
            .select("name")
            .eq("member_id", from_number)
            .limit(1)
            .execute()
        )
        rows = getattr(r, "data", None) or []
        if rows and rows[0].get("name"):
            return rows[0]["name"]
    except Exception as e:
        print("review: members lookup error:", e)

    # Try staff
    try:
        r = (
            sb.table("staff")
            .select("name")
            .eq("staff_id", from_number)
            .limit(1)
            .execute()
        )
        rows = getattr(r, "data", None) or []
        if rows and rows[0].get("name"):
            return rows[0]["name"]
    except Exception as e:
        print("review: staff lookup error:", e)

    return None


def start_review_flow(sb, from_number: str, send_text, display_name: str | None = None):
    """
    Sends the review prompt and logs it in public.responses.

    Table: public.responses (recommended schema below)
      - response_id uuid default gen_random_uuid() primary key
      - customer_id text
      - name text
      - prompt text
      - created_at timestamptz
      - channel text
      - source_trigger text
      - status text
    """
    name = (display_name or "").strip() or _fetch_name_if_missing(sb, from_number)

    if name:
        prompt = PROMPT_TEMPLATE_WITH_NAME.format(name=name)
    else:
        prompt = PROMPT_TEMPLATE_NO_NAME

    # 1) Send WhatsApp message
    send_text(from_number, prompt)

    # 2) Persist outbound prompt
    payload = {
        "customer_id": from_number,
        "name": name,
        "prompt": prompt,
        "created_at": _safe_now_iso(),
        "channel": "whatsapp",
        "source_trigger": "REVIEW",
        "status": "pending",
    }
    try:
        sb.table("responses").insert(payload).execute()
    except Exception as e:
        print("review: responses insert error (REVIEW):", e)


def send_google_review_link(sb, from_number: str, send_text, display_name: str | None = None):
    """
    Sends a Google review request and logs it in public.responses.

    Message format (with emojis and one blank line before the link):
      Thanks for making your 3rd visit today {name}! 🌟
      We value your feedback and would appreciate it if you gave us a review on Google:

      https://search.google.com/local/writereview?placeid=...

    Stored with source_trigger='GOOGLE' and status='sent'.
    """
    name = (display_name or "").strip() or _fetch_name_if_missing(sb, from_number)

    if name:
        header = GOOGLE_TEMPLATE_WITH_NAME.format(name=name)
    else:
        header = GOOGLE_TEMPLATE_NO_NAME

    full_message = f"{header}\n\n{GOOGLE_REVIEW_URL} 📍"

    # 1) Send WhatsApp message
    send_text(from_number, full_message)

    # 2) Persist outbound prompt
    payload = {
        "customer_id": from_number,
        "name": name,
        "prompt": full_message,
        "created_at": _safe_now_iso(),
        "channel": "whatsapp",
        "source_trigger": "GOOGLE",
        "status": "sent",
    }
    try:
        sb.table("responses").insert(payload).execute()
    except Exception as e:
        print("review: responses insert error (GOOGLE):", e)
        
def handle_review_reply(sb, from_number: str, text: str, send_text):
    """
    Capture any text reply after a review prompt and store it under the responses table.
    """
    if not text:
        return False

    # Save the reply
    payload = {
        "customer_id": from_number,
        "prompt": text,
        "created_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "channel": "whatsapp",
        "source_trigger": "REVIEW_REPLY",
        "status": "received",
    }
    try:
        sb.table("responses").insert(payload).execute()
        send_text(from_number, "🙏 Thank you for your feedback — it’s been noted and shared with our team.")
        return True
    except Exception as e:
        print("review: insert error (reply):", e)
        return False

def handle_review_reply(sb, from_number: str, text: str, send_text):
    if not text:
        return False

    payload = {
        "customer_id": from_number,
        "prompt": text,
        "created_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "channel": "whatsapp",
        "source_trigger": "REVIEW_REPLY",
        "status": "received",
    }
    try:
        sb.table("responses").insert(payload).execute()
    except Exception as e:
        print("review: insert error (reply):", e)
        # We STILL consume the message to avoid falling through to the survey.
        # You already thanked the user below.

    # Always acknowledge and consume
    send_text(from_number, "🙏 Thank you for your feedback — it’s been noted and shared with our team.")
    return True

