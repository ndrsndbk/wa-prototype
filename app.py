"""Flask application for WhatsApp Cloud API stamp‑card prototype.

This app combines a WhatsApp webhook handler and a simple dynamic
image generator.  Incoming messages to the WhatsApp test number are
parsed and used to update a customer record in a PostgreSQL database.
The visit count determines which stamp to show on a loyalty card.

Routes:

  * /webhook (GET) – verification endpoint for Meta's webhook setup.
  * /webhook (POST) – handle incoming WhatsApp messages.
  * /card – generate a stamp card on the fly given a visit count.

Environment variables expected:

  WHATSAPP_VERIFY_TOKEN – secret token used for webhook verification
  WHATSAPP_TOKEN        – permanent access token for the WhatsApp API
  PHONE_NUMBER_ID       – phone number ID provided by Meta
  DATABASE_URL          – PostgreSQL connection string (e.g. Supabase)
  HOST_URL              – public base URL of the deployed service

To run locally for testing, set these variables in your shell or a
`.env` file and install dependencies (Flask, Pillow, psycopg2, requests).
"""

import os
import requests
import psycopg2
from flask import Flask, request, send_file
from io import BytesIO
from PIL import Image, ImageDraw

# Create Flask application
app = Flask(__name__)

# Read environment variables
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HOST_URL = os.getenv("HOST_URL")  # e.g. https://your-app.onrender.com

# Establish a persistent DB connection
if DATABASE_URL:
    conn = psycopg2.connect(DATABASE_URL)
else:
    conn = None


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Verification endpoint for Meta.  When the app is first configured,
    Meta sends a GET request with hub.mode=subscribe and hub.challenge.
    We respond with the hub.challenge if the verify token matches.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """
    Handle incoming WhatsApp messages.  Supports two commands:
      * "TEST" – responds with a demo card (0 visits) and a greeting.
      * "SALE" – upserts/increments the visit count for the sender,
                 responds with a dynamic loyalty card and optionally
                 congratulates the customer on the 10th visit.
    """
    data = request.get_json(silent=True) or {}
    try:
        # Navigate the webhook payload to extract the message
        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        message = (value.get("messages") or [None])[0]
        if not message:
            # Not a text message – ignore
            return "ignored", 200

        from_number = message.get("from")
        text = (message.get("text", {}) or {}).get("body", "")
        text = text.strip().upper()

        # When a customer sends "TEST", show them a demo card
        if text == "TEST":
            # Use the dynamic card route with n=0 visits
            card_url = build_card_url(visits=0)
            send_image(from_number, card_url)
            send_text(from_number, "👋 Thanks for testing! Here's your demo loyalty card.")
            return "ok", 200

        # When a customer sends "SALE", record a sale and show card
        if text == "SALE":
            # Ensure DB connection is available
            if conn is None:
                raise RuntimeError("Database connection is not configured")

            visits = 1
            # Upsert the customer and increment visits
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO customers (customer_id, number_of_visits, last_visit_at)
                        VALUES (%s, 1, NOW())
                        ON CONFLICT (customer_id)
                        DO UPDATE SET
                            number_of_visits = customers.number_of_visits + 1,
                            last_visit_at     = NOW()
                        RETURNING number_of_visits
                        """,
                        (from_number,),
                    )
                    row = cur.fetchone()
                    if row:
                        visits = row[0]

            # Clamp visits to max 10 for card display
            visits = max(1, min(10, visits))
            card_url = build_card_url(visits=visits)
            send_image(from_number, card_url)

            # Congratulate on the 10th visit
            if visits >= 10:
                send_text(from_number, "🎉 Free coffee unlocked! Show this to the barista.")
            else:
                send_text(from_number, f"Thanks for your visit! You now have {visits} stamp(s).")
            return "ok", 200

    except Exception as exc:
        # Log the exception for debugging; return 200 to acknowledge receipt
        print("Error handling webhook:", exc)

    return "ok", 200


def build_card_url(visits: int) -> str:
    """
    Construct the absolute URL to the dynamic card image.
    Uses the HOST_URL environment variable if set, otherwise
    derives from the current request context.
    """
    base = HOST_URL
    if not base:
        # Fallback: derive from current request (e.g. http://localhost:3000/)
        base = request.url_root.rstrip("/")
    return f"{base}/card?n={visits}"


def send_text(to: str, body: str) -> None:
    """
    Send a plain text WhatsApp message using the Cloud API.
    """
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        raise RuntimeError("WhatsApp token or phone number ID not configured")
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as exc:
        # Print exceptions for debugging
        print("Error sending text:", exc)


def send_image(to: str, link: str) -> None:
    """
    Send an image WhatsApp message by providing a link to the image.
    The link must be publicly accessible; the /card route in this
    application generates dynamic images on demand.
    """
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        raise RuntimeError("WhatsApp token or phone number ID not configured")
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": link},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as exc:
        print("Error sending image:", exc)


@app.route("/card")
def card():
    """
    Generate a simple loyalty stamp card PNG on the fly.
    Accepts a query parameter `n` (int) representing the number of visits.
    Visits are clamped between 0 and 10.  The first n circles are filled
    with a green color; others remain empty.
    """
    # Parse the visit count; default to 0 if missing or invalid
    try:
        visits = int(request.args.get("n", 0))
    except ValueError:
        visits = 0
    visits = max(0, min(10, visits))

    # Image dimensions (WxH) in pixels
    width, height = 600, 400
    # Background color (light grey)
    bg_color = (245, 245, 245)
    # Create an image with the background color
    im = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(im)

    # Draw the title at the top
    title = f"Visits: {visits}/10"
    title_x = 20
    title_y = 30
    draw.text((title_x, title_y), title, fill=(40, 40, 40))

    # Layout for 10 stamp circles (2 rows of 5)
    radius = 25
    gap = 30
    margin_x = 50
    margin_y = 120

    for i in range(10):
        row = i // 5
        col = i % 5
        cx = margin_x + col * (2 * radius + gap) + radius
        cy = margin_y + row * (2 * radius + gap) + radius
        bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
        outline_color = (40, 40, 40)
        fill_color = (0, 180, 90) if i < visits else None
        draw.ellipse(bbox, fill=fill_color, outline=outline_color, width=3)

    # Serialize image to a buffer and return as a response
    buf = BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


if __name__ == "__main__":
    # Bind to all interfaces; default port 3000 for compatibility
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)