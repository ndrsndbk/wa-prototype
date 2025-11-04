# signup_flow.py
import datetime
from typing import Optional, Callable, Dict

# Types for the callbacks we expect app.py to pass in
SendTextFn = Callable[[str, str], None]
SendImageFn = Callable[[str, str, Optional[str]], None]
SendButtonsFn = Callable[[str, str, list], None]
SetStateFn = Callable[[str, Optional[str], int], None]
ClearStateFn = Callable[[str], None]
SetBirthdayFn = Callable[[str, Optional[datetime.date], str], None]
SetDrinkFn = Callable[[str, str], None]

def _parse_birthday(raw: str) -> Optional[datetime.date]:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except Exception:
            pass
    return None

def start_signup_flow(
    customer_id: str,
    wa_name: Optional[str],
    send_text: SendTextFn,
    set_state: SetStateFn
):
    wave = "👋"
    welcome = (
        f"Welcome{',' if wa_name else ''}{(' ' + wa_name) if wa_name else ''} {wave} "
        "Answer 2 quick questions to signup for the stamp card:\n\n"
        "First, when is your birthday?\n_You get a free drink on your birthday_"
    )
    send_text(customer_id, welcome)
    set_state(customer_id, "signup", 1)

def handle_signup_text_step1(
    customer_id: str,
    text: str,
    send_buttons: SendButtonsFn,
    set_birthday: SetBirthdayFn,
    set_state: SetStateFn
) -> bool:
    bday = _parse_birthday(text)
    set_birthday(customer_id, bday, text or "")
    # Ask for preferred drink via interactive buttons
    send_buttons(
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

def handle_signup_reply_step2(
    customer_id: str,
    reply_id: str,
    send_text: SendTextFn,
    send_image: SendImageFn,
    set_drink: SetDrinkFn,
    clear_state: ClearStateFn,
    stamp_card_zero_url: str
) -> bool:
    mapping: Dict[str, str] = {
        "drink_matcha": "matcha",
        "drink_americano": "americano",
        "drink_cappuccino": "cappuccino",
    }
    choice = mapping.get(reply_id)
    if not choice:
        return False
    set_drink(customer_id, choice)
    send_text(customer_id, "Thanks! Here's your stamp card 🎉")
    send_image(customer_id, stamp_card_zero_url)
    clear_state(customer_id)
    return True
