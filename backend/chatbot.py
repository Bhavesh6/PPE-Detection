"""In-app help chatbot via Google Gemini's free tier.

Answers "how do I..." questions about SafetyFirst itself, scoped to what
the asking account can actually see and do — a guest gets pointed at the
visitor demo and sign-up, a named operator gets their own history and
badge/verdict explained, an admin gets the full console. The scoping is a
system prompt per role, not a permissions check on the model's output: the
model is told what's true for this account and asked to stay inside it,
same trust boundary as any other text a client sends us.

Same "blank key means not configured" convention as tts.py/ElevenLabs — a
missing GEMINI_API_KEY makes the widget say so rather than breaking.
"""

import time

import requests
from flask import current_app

MAX_MESSAGE_LEN = 800
MAX_HISTORY_TURNS = 6  # each turn is a user+model pair; older context is dropped, not summarized

_BASE_PROMPT = """You are the in-app help assistant for SafetyFirst, a PPE
(personal protective equipment) compliance checkpoint system. A camera
checks whether someone is wearing required safety gear before a gate opens
for them; sensors can report site hazards like gas that hold the gate
regardless of PPE. You help the person currently using it understand what
the product does and how to do things in it.

Rules:
- Only describe features that exist and that this specific account can
  reach (see below). Never invent a setting, page, or button.
- If asked to change something on their behalf (a setting, someone's
  account, an alert), explain that you can only describe how to do it —
  you cannot act on the site yourself.
- Keep answers short and concrete: a few sentences or a short list, not an
  essay. This is a help widget, not a report.
- If a question is unrelated to using SafetyFirst, say so briefly and
  redirect to what you can help with.
"""

SYSTEM_PROMPTS = {
    "guest": _BASE_PROMPT + """
This person is browsing as a guest — not signed up, no persistent
identity. What they can do:
- Try the live PPE detection demo on the "Try it" / visit-site page using
  their own camera — it shows a live verdict (granted/denied) and which
  required items are missing.
- Sign up for a real account (top of the sign-in page) to get a
  persistent identity and see their own history over time, which a guest
  session doesn't keep.
They cannot see admin settings, other people's data, alerts, or reports —
that needs a real account, and most of it needs an admin account
specifically. If they ask about those, tell them to sign up, or ask
whoever administers this site for access.
""",
    "operator": _BASE_PROMPT + """
This person has a real, signed-up account — a named worker or operator,
not a guest. What they can do:
- Everything a guest can (the live demo).
- See their own attendance/detection history: every time they were
  checked, whether they were granted or denied entry, and what PPE was
  missing on a denial.
- Their access is tied to their signed-up account, not a badge scan by
  itself — a badge/RFID scan (where hardware exists) looks up who they
  are, then the camera decides the verdict.
They cannot see or change site-wide settings, required PPE, sensor
thresholds, other people's records, alerts, reports, or the audit log —
those are admin-only. If asked about those, tell them to contact an
administrator rather than guessing at how to do it themselves.
""",
    "admin": _BASE_PROMPT + """
This person is an administrator with full access to the console. Pages
and what each does:
- Overview: live gate state, who's currently active, quick counts.
- Personnel: manage worker accounts, badges, roles.
- Alerts: hazard alerts (gas, smoke, etc.) — a critical one holds the gate
  for everyone until acknowledged. Sensor Thresholds sets warning/critical
  levels per sensor kind (e.g. gas in ppm or mV, direction above/below).
  Live Readings shows the latest value per sensor; Reading History charts
  values over time. "Simulate an alert" fires a test alert through the
  same path a real device uses.
- Settings: required PPE items (only ones the detection model can
  actually see — currently hardhat, safety vest, mask), and the
  confidence threshold (how sure the model must be before counting a
  detection; higher = fewer false violations but risks missing real
  ones). Changes apply on the next camera frame everywhere, no restart.
- Analytics: compliance rate, a daily granted/denied trend, a breakdown
  of which PPE is missing most often, an hour-of-day histogram, and
  per-worker compliance scorecards.
- Violations: every refusal, with the camera frame that caused it kept as
  evidence (auto-deleted after a retention window; the decision record
  itself is kept regardless).
- Reports: CSV exports for a chosen date range.
- Audit: an append-only log of who changed what policy/personnel/alert
  setting and when — nothing here can be edited or deleted, by design.
- GPS: where the checkpoint device is reporting its location from, if a
  GPS module is attached.
If asked to actually change a setting, explain which page and field to
use — you can guide them there, but the action itself has to happen in
the UI, not through this chat.
""",
}


def enabled():
    return bool(current_app.config["GEMINI_API_KEY"])


def ask(message, role, history=None):
    """Return (reply_text, error). history is a list of {role, text} dicts,
    oldest first, already trimmed by the caller to MAX_HISTORY_TURNS."""
    message = (message or "").strip()
    if not message:
        return None, "No message supplied"
    if len(message) > MAX_MESSAGE_LEN:
        return None, "Message too long"
    if not enabled():
        return None, "The help assistant is not configured on this server"

    system_prompt = SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["guest"])

    contents = []
    for turn in (history or [])[-(MAX_HISTORY_TURNS * 2):]:
        gemini_role = "model" if turn.get("role") == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": str(turn.get("text", ""))[:MAX_MESSAGE_LEN]}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    model = current_app.config["GEMINI_MODEL"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0.3},
    }

    # A 503 here means Google's own model is transiently overloaded, not
    # that anything is wrong with the request — confirmed by hand, three
    # identical requests in a row came back 503/200/503. Google's own docs
    # say to retry on this; one retry after a short pause turns a coin-flip
    # failure into something that only fails if it's unlucky twice in a row.
    res = None
    for attempt in range(2):
        try:
            res = requests.post(url, params={"key": current_app.config["GEMINI_API_KEY"]}, json=payload, timeout=20)
        except requests.RequestException as exc:
            current_app.logger.warning("Gemini request failed: %s", exc)
            return None, "Could not reach the help assistant"
        if res.status_code != 503 or attempt == 1:
            break
        time.sleep(1.5)

    if not res.ok:
        current_app.logger.warning("Gemini returned %s: %s", res.status_code, res.text[:200])
        return None, f"Help assistant returned {res.status_code}"

    try:
        data = res.json()
        reply = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError):
        # A prompt Gemini's safety filters blocked outright has no
        # candidates at all rather than an error status — same effect as
        # an error from the caller's point of view, so treat it as one.
        current_app.logger.warning("Gemini response had no usable reply: %s", res.text[:200])
        return None, "The help assistant couldn't answer that"

    return reply.strip(), None
