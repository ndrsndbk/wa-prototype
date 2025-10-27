# clockin.py
import datetime

# How many minutes to treat repeated clock-ins as duplicates (no extra count)?
DEBOUNCE_MINUTES = 5


def handle_clockin(sb, from_number: str, send_text, display_name: str | None = None):
    """
    Record a staff clock-in.
    Table: public.staff
      - staff_id (text, PK)        -> WhatsApp number (message.from)
      - name (text)                -> WhatsApp profile name (contacts[0].profile.name)
      - last_checkin_at (timestamptz)
      - checkin_count (int)

    Args:
        sb: Supabase client
        from_number: WhatsApp number string (e.g., "2783...")
        send_text: callback(to, body) for WhatsApp
        display_name: Optional WhatsApp profile display name
    """
    # 1) Fetch existing row (if any)
    try:
        resp = (
            sb.table("staff")
            .select("staff_id, name, last_checkin_at, checkin_count")
            .eq("staff_id", from_number)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
    except Exception as e:
        print("staff select error:", e)
        rows = []

    now_utc = datetime.datetime.utcnow().replace(microsecond=0)
    now_iso = now_utc.isoformat() + "Z"

    existing = rows[0] if rows else None
    current_name = (display_name or "").strip() or (existing.get("name") if existing else None)

    # 2) Debounce repeated clock-ins within DEBOUNCE_MINUTES
    new_count = 1
    recently_clocked = False
    if existing:
        prev_count = int(existing.get("checkin_count", 0) or 0)
        new_count = prev_count + 1

        last_ts_raw = existing.get("last_checkin_at")
        if last_ts_raw:
            try:
                # Accept both "YYYY-MM-DDTHH:MM:SSZ" and full ISO strings
                last_dt = (
                    datetime.datetime.fromisoformat(last_ts_raw.replace("Z", "+00:00"))
                    if isinstance(last_ts_raw, str)
                    else last_ts_raw
                )
                delta = now_utc - last_dt.replace(tzinfo=None)
                if delta.total_seconds() < DEBOUNCE_MINUTES * 60:
                    recently_clocked = True
                    new_count = prev_count  # do not increment
            except Exception as _:
                pass

    payload = {
        "staff_id": from_number,
        "name": current_name,
        "last_checkin_at": now_iso,
        "checkin_count": new_count,
    }

    # 3) Upsert
    try:
        sb.table("staff").upsert(payload).execute()
        name_for_msg = current_name or "team member"
        if recently_clocked:
            send_text(
                from_number,
                f"✅ Already clocked in recently, {name_for_msg}. Last updated at {now_iso}."
            )
        else:
            send_text(
                from_number,
                f"✅ Clocked in, {name_for_msg}! Total check-ins: {new_count}."
            )
    except Exception as e:
        print("staff upsert error:", e)
        send_text(from_number, "⚠️ Sorry, I couldn't record your clock-in. Please try again.")
