# qa_handler.py
# Minimal 3-question profile flow without WhatsApp Flows.
# Tables used: response_states, responses, profiles (see schema.sql)

from typing import Optional
import datetime
import re
from supabase import Client

QUESTIONS = [
    {
        "id": "birthday",
        "prompt": "When's your birthday? (You'll get a free coffee on your birthday :) )",
    },
    {
        "id": "favorite_flavor",
        "prompt": "What's your favorite flavor?",
    },
    {
        "id": "promo_opt_in",
        "prompt": "Would you like to be notified when we run a promotion? (Yes/No)",
    },
]
FLOW_ID = "profile_v1"

def _parse_birthday(text: str) -> str:
    t = text.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", t)
    if m:
        d, mo, y = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", t)
    if m:
        d, mo = m.groups()
        return f"--{int(mo):02d}-{int(d):02d}"
    return t

def _parse_bool(text: str) -> Optional[bool]:
    t = text.strip().lower()
    if t in {"y", "yes", "yeah", "yep", "sure", "ok"}:
        return True
    if t in {"n", "no", "nope"}:
        return False
    return None

def _upsert_profile_from_answers(sb: Client, customer_id: str):
    res = (
        sb.table("responses")
          .select("question_id, answer_text, received_at")
          .eq("customer_id", customer_id)
          .order("received_at", desc=True)
          .execute()
          .data or []
    )
    latest = {}
    for row in res:
        qid = row["question_id"]
        if qid not in latest:
            latest[qid] = row["answer_text"]

    birthday_raw = latest.get("birthday")
    favorite     = latest.get("favorite_flavor")
    promo_raw    = latest.get("promo_opt_in")

    birthday_norm = _parse_birthday(birthday_raw) if birthday_raw else None
    promo_bool    = _parse_bool(promo_raw) if promo_raw else None

    sb.table("profiles").upsert({
        "customer_id": customer_id,
        "birthday": birthday_norm,
        "favorite_flavor": favorite,
        "promo_opt_in": promo_bool,
        "updated_at": datetime.datetime.utcnow().isoformat()
    }).execute()

def start_profile_flow(sb: Client, customer_id: str, send_text) -> None:
    sb.table("response_states").upsert({
        "customer_id": customer_id,
        "flow_id": FLOW_ID,
        "step_index": 0,
        "total_steps": len(QUESTIONS),
    }).execute()
    send_text(customer_id, QUESTIONS[0]["prompt"])

def handle_profile_answer(sb: Client, customer_id: str, text: str, send_text) -> bool:
    state = (
        sb.table("response_states")
          .select("flow_id, step_index, total_steps")
          .eq("customer_id", customer_id)
          .maybe_single()
          .execute()
          .data
    )
    if not state or state.get("flow_id") != FLOW_ID:
        return False

    step = int(state.get("step_index") or 0)
    total = int(state.get("total_steps") or len(QUESTIONS))
    if step < 0 or step >= total:
        sb.table("response_states").delete().eq("customer_id", customer_id).execute()
        return False

    q = QUESTIONS[step]
    sb.table("responses").insert({
        "customer_id": customer_id,
        "question_id": q["id"],
        "answer_text": text.strip(),
    }).execute()

    step += 1
    if step >= total:
        sb.table("response_states").delete().eq("customer_id", customer_id).execute()
        _upsert_profile_from_answers(sb, customer_id)
        send_text(customer_id, "✅ Thanks! Your profile has been updated.")
        return True
    else:
        sb.table("response_states").upsert({
            "customer_id": customer_id,
            "flow_id": FLOW_ID,
            "step_index": step,
            "total_steps": total,
        }).execute()
        send_text(customer_id, QUESTIONS[step]["prompt"])
        return True
