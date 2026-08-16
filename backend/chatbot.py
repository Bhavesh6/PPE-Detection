"""In-app help chatbot, Gemini first with Groq as a fallback.

Answers "how do I..." questions about SafetyFirst itself, scoped to what
the asking account can actually see and do — a guest gets pointed at the
visitor demo and sign-up, a named operator gets their own history and
badge/verdict explained, an admin gets the full console. The scoping is a
system prompt per role, not a permissions check on the model's output: the
model is told what's true for this account and asked to stay inside it,
same trust boundary as any other text a client sends us.

Two providers, tried in order, because both free tiers have daily caps and
running out mid-demo is the failure that actually matters here — they're
unlikely to be exhausted at the same moment. Either key may be blank; only
a request with no working provider at all reports itself unconfigured,
same "blank key means not configured" convention as tts.py/ElevenLabs.
"""

import time

import requests
from flask import current_app

MAX_MESSAGE_LEN = 800
MAX_HISTORY_TURNS = 6  # each turn is a user+model pair; older context is dropped, not summarized

# What the person is actually looking at when they ask. Without this,
# "how do I set this up?" on the Alerts page is unanswerable — the model
# has no idea what "this" is. Keyed by the page's filename, since that's
# what the browser can cheaply report and it doesn't change with routing.
PAGE_CONTEXT = {
    "admin.html": "the Overview page — live gate state, who is currently active, and quick counts",
    "alerts.html": "the Alerts page — hazard alerts, sensor thresholds (warning/critical per sensor kind), live readings, and reading history charts",
    "analytics.html": "the Analytics page — compliance rate, daily granted/denied trend, missing-PPE breakdown, hour-of-day histogram, per-worker scorecards",
    "audit.html": "the Change Log page — the append-only record of who changed what policy, personnel, or alert setting and when",
    "gps.html": "the Site Location page — where the checkpoint device reports its GPS position",
    "history.html": "their own records page — this person's past gate checks and verdicts",
    "reports.html": "the Reports page — CSV exports of gate decisions for a chosen date range",
    "settings.html": "the Checkpoint Policy page — which PPE items are required, and the detection confidence threshold",
    "violations.html": "the Captures page — every refusal, with the camera frame kept as evidence",
    "visit-site.html": "the Gate Control page — the live camera check that decides whether the gate opens",
}

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


def _build_system_prompt(role, page):
    """Role prompt plus, if we know it, what they're currently looking at."""
    prompt = SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["guest"])
    hint = PAGE_CONTEXT.get((page or "").strip().lower())
    if hint:
        prompt += (
            f"\nRight now they are looking at {hint}. If their question is "
            "vague about location (\"this\", \"here\", \"this page\"), assume "
            "they mean that. Don't mention the page name unless it's useful."
        )
    return prompt


def _trim_history(history):
    """Normalize to [{role, text}] and cap length. Anything the client sends
    is untrusted shape as much as untrusted content, so nothing here assumes
    the keys or types are what they should be."""
    turns = []
    for turn in (history or [])[-(MAX_HISTORY_TURNS * 2):]:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text", ""))[:MAX_MESSAGE_LEN].strip()
        if not text:
            continue
        role = "assistant" if turn.get("role") == "assistant" else "user"
        turns.append({"role": role, "text": text})
    return turns


def _ask_gemini(system_prompt, turns, message):
    """Return (reply, error, exhausted). `exhausted` means the daily quota
    is gone, which is the signal to try the next provider rather than to
    give up — a plain error isn't, since a broken request would fail the
    same way everywhere."""
    key = current_app.config["GEMINI_API_KEY"]
    if not key:
        return None, None, True

    contents = [
        {"role": "model" if t["role"] == "assistant" else "user", "parts": [{"text": t["text"]}]}
        for t in turns
    ]
    contents.append({"role": "user", "parts": [{"text": message}]})

    model = current_app.config["GEMINI_MODEL"]
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        # 400 was too tight and truncated structured answers mid-sentence —
        # this model spends a real chunk of its budget on internal reasoning
        # before any visible text comes out (seen directly: thoughtsTokenCount
        # of 90+ on a two-word answer), so the visible reply needs real
        # headroom on top of that.
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.3},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # A 503 means Google's own model is transiently overloaded, not that
    # anything is wrong with the request — confirmed by hand, identical
    # requests in a row came back 503/200/503. Their docs say to retry.
    res = None
    backoff = (1.0, 2.5)
    for attempt in range(3):
        try:
            res = requests.post(url, params={"key": key}, json=payload, timeout=20)
        except requests.RequestException as exc:
            current_app.logger.warning("Gemini request failed: %s", exc)
            return None, "Could not reach the assistant", False
        if res.status_code != 503:
            break
        if attempt < len(backoff):
            time.sleep(backoff[attempt])

    if res.status_code == 429:
        current_app.logger.warning("Gemini quota exhausted: %s", res.text[:200])
        return None, None, True
    if not res.ok:
        current_app.logger.warning("Gemini returned %s: %s", res.status_code, res.text[:300])
        if res.status_code == 503:
            return None, "The assistant is busy right now — ask again in a moment.", False
        return None, "The assistant couldn't answer that just now.", False

    try:
        return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip(), None, False
    except (KeyError, IndexError, ValueError):
        # A prompt Gemini's safety filters blocked outright has no candidates
        # at all rather than an error status — same effect from here, so
        # treat it as an error. Not "exhausted": another provider would
        # likely refuse it too, and retrying costs the user a wait for
        # nothing.
        current_app.logger.warning("Gemini gave no usable reply: %s", res.text[:200])
        return None, "The assistant couldn't answer that.", False


def _ask_groq(system_prompt, turns, message):
    """Same contract as _ask_gemini. Groq speaks the OpenAI chat format, so
    the message shape differs from Gemini's contents/parts."""
    key = current_app.config["GROQ_API_KEY"]
    if not key:
        return None, None, True

    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": t["role"], "content": t["text"]} for t in turns]
    messages.append({"role": "user", "content": message})

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": current_app.config["GROQ_MODEL"],
                "messages": messages,
                "max_tokens": 700,
                "temperature": 0.3,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        current_app.logger.warning("Groq request failed: %s", exc)
        return None, "Could not reach the assistant", False

    if res.status_code == 429:
        current_app.logger.warning("Groq quota exhausted: %s", res.text[:200])
        return None, None, True
    if not res.ok:
        current_app.logger.warning("Groq returned %s: %s", res.status_code, res.text[:300])
        return None, "The assistant couldn't answer that just now.", False

    try:
        return res.json()["choices"][0]["message"]["content"].strip(), None, False
    except (KeyError, IndexError, ValueError):
        current_app.logger.warning("Groq gave no usable reply: %s", res.text[:200])
        return None, "The assistant couldn't answer that.", False


def enabled():
    return bool(current_app.config["GEMINI_API_KEY"] or current_app.config["GROQ_API_KEY"])


def ask(message, role, history=None, page=None):
    """Return (reply_text, error).

    Tries each configured provider in turn, moving on only when one reports
    its quota gone — a genuine error (bad request, blocked prompt) fails the
    same way everywhere, so retrying it just makes the user wait longer for
    the same answer.
    """
    message = (message or "").strip()
    if not message:
        return None, "No message supplied"
    if len(message) > MAX_MESSAGE_LEN:
        return None, "Message too long"
    if not enabled():
        return None, "The help assistant is not configured on this server"

    system_prompt = _build_system_prompt(role, page)
    turns = _trim_history(history)

    last_error = None
    for provider in (_ask_gemini, _ask_groq):
        reply, error, exhausted = provider(system_prompt, turns, message)
        if reply:
            return reply, None
        if not exhausted:
            return None, error
        last_error = error

    # Every provider is out of quota (or none is configured, though enabled()
    # already ruled that out above).
    return None, last_error or (
        "The assistant has hit its daily free-tier limit. It'll work again "
        "tomorrow, or sooner on a larger quota."
    )
