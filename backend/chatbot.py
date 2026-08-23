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

# Answers for a server with no language model configured.
#
# The widget used to reply "The help assistant is not configured on this
# server", which is true and useless: it reads as a broken feature rather
# than an unset key, and it is the first thing anyone poking around the
# console tries. Most of what people actually ask is "where is X" — a
# question this file already knows the answer to, without a model.
#
# Each entry is (roles, keywords, answer). Matching is a keyword count, so
# the phrasing does not have to be guessed at. Entries are scoped by role
# so a worker is not sent to a page they cannot open.
_GUIDE = [
    (("admin",), ("camera", "cctv", "cam", "feed", "stream", "tile"),
     "Site Cameras shows live tiles from the site's fixed cameras, relayed "
     "through the checkpoint device rather than exposed directly. A tile "
     "that says it is paused means the device has not got a frame from that "
     "camera yet — usually the camera's address is wrong or it is off the "
     "network. The camera that decides the gate verdict is a different one: "
     "that is on the checkpoint itself, and you see it on Gate Control."),
    (("admin", "operator", "guest"), ("gate", "verdict", "granted", "denied", "check", "scan"),
     "Gate Control runs the live check: the camera looks for the required "
     "PPE and shows granted or denied, with the missing items listed. That "
     "is the same decision the physical checkpoint makes."),
    (("admin",), ("alert", "hazard", "gas", "smoke", "threshold", "sensor", "reading"),
     "Alerts covers three things. Hazard alerts are raised by the sensor "
     "nodes — a critical one holds the gate for everyone until it is "
     "acknowledged. Sensor Thresholds sets the warning and critical level "
     "per sensor kind. Live Readings shows the latest value from each "
     "sensor, and Reading History charts them over time."),
    (("admin",), ("ppe", "required", "hardhat", "helmet", "vest", "mask",
                  "confidence", "threshold", "policy", "setting"),
     "Checkpoint Policy is where you choose which PPE is required and set "
     "the detection confidence threshold. Only equipment the model can "
     "actually see is offered — hardhat, safety vest, mask. Raising the "
     "confidence means fewer false refusals but a higher chance of missing "
     "a real one. Changes apply on the next camera frame, no restart."),
    (("admin",), ("notice", "contractor", "supervisor", "subcontractor",
                  "letter", "formal", "dispute", "acknowledge"),
     "Safety Notices is how a refusal reaches someone who has no login — a "
     "subcontractor's supervisor, an agency. Issue one against a worker "
     "with the refusals it concerns; it gets a reference and a due date. "
     "The recipient opens a link that needs no account, reads it with the "
     "evidence, and either accepts it with a note on what they will do or "
     "disputes it with a reason. Send it by email if mail is configured, "
     "otherwise use Mark sent after sending the link yourself."),
    (("admin",), ("report", "csv", "export", "download", "spreadsheet"),
     "Reports exports gate decisions as CSV for a date range you choose. "
     "Safety Notices has its own exports too — one notice as JSON, or the "
     "whole list as CSV."),
    (("admin",), ("analytic", "compliance", "rate", "trend", "chart",
                  "graph", "statistic", "scorecard"),
     "Analytics has the compliance rate, a daily granted/denied trend, a "
     "breakdown of which PPE is missing most often, an hour-of-day "
     "histogram, and per-worker scorecards."),
    (("admin",), ("worker", "personnel", "staff", "badge", "rfid", "card",
                  "employee", "account", "role"),
     "Personnel manages worker accounts, their badges and their roles. A "
     "badge scan looks up who someone is; the camera then decides whether "
     "they get in."),
    (("admin",), ("audit", "change log", "who changed", "history of changes"),
     "The Change Log is an append-only record of who changed which policy, "
     "person or alert setting, and when. Nothing in it can be edited or "
     "deleted, by design."),
    (("admin",), ("gps", "location", "where", "site location", "map",
                  "coordinates", "position"),
     "Site Location shows where the checkpoint reports itself to be. With a "
     "GNSS module attached it updates itself and the badge reads \"Live from "
     "device\"; otherwise it stays wherever an admin set it by hand. A "
     "module needs a clear view of the sky before it can report anything."),
    (("admin",), ("violation", "refusal", "evidence", "capture", "photo", "frame"),
     "Captures lists every refusal with the camera frame that caused it, "
     "kept as evidence. The images are deleted after a retention window; "
     "the decision record itself is kept regardless."),
    (("operator", "guest"), ("history", "my record", "my check", "past", "attendance"),
     "Your records page lists every time you were checked, whether you were "
     "let in, and what was missing if you were not. Any safety notice "
     "issued about you appears there too."),
]


def _offline_answer(message, role, page):
    """Answer from the built-in guide when no model is configured.

    Deliberately not dressed up as the real assistant: it says what it is,
    so nobody mistakes a keyword match for a conversation and asks it a
    follow-up it cannot handle.
    """
    text = (message or "").lower()

    best, best_score = None, 0
    for roles, keywords, answer in _GUIDE:
        if role not in roles:
            continue
        score = sum(1 for word in keywords if word in text)
        if score > best_score:
            best, best_score = answer, score

    here = PAGE_CONTEXT.get(page)
    note = ("\n\nThis is the built-in guide — the full assistant needs a "
            "GEMINI_API_KEY or GROQ_API_KEY set on the server.")

    if best:
        return best + note

    topics = ("Alerts and sensor thresholds, Checkpoint Policy, Safety "
              "Notices, Site Cameras, Site Location, Personnel, Captures, "
              "Analytics, Reports, and the Change Log."
              if role == "admin" else
              "the live gate check and your own records.")
    where = f"You are on {here}. " if here else ""
    return (f"{where}I can point you to: {topics} Ask about any of those by "
            f"name.{note}")

# What the person is actually looking at when they ask. Without this,
# "how do I set this up?" on the Alerts page is unanswerable — the model
# has no idea what "this" is. Keyed by the page's filename, since that's
# what the browser can cheaply report and it doesn't change with routing.
PAGE_CONTEXT = {
    "admin.html": "the Overview page — live gate state, who is currently active, and quick counts",
    "alerts.html": "the Alerts page — hazard alerts, sensor thresholds (warning/critical per sensor kind), live readings, and reading history charts",
    "analytics.html": "the Analytics page — compliance rate, daily granted/denied trend, missing-PPE breakdown, hour-of-day histogram, per-worker scorecards",
    "audit.html": "the Change Log page — the append-only record of who changed what policy, personnel, or alert setting and when",
    "cctv.html": "the Site Cameras page — live tiles from the site's fixed cameras, relayed by the checkpoint device",
    "gps.html": "the Site Location page — where the checkpoint device reports its GPS position",
    "history.html": "their own records page — this person's past gate checks and verdicts, and any safety notices issued to them",
    "kiosk.html": "the Checkpoint display — the fullscreen gate screen a worker stands in front of",
    "notice.html": "a single safety notice, opened from its link — the recipient reads it here and answers it; no sign-in, the link itself is the access",
    "notices.html": "the Safety Notices page — issuing a refusal notice to an outside contractor or supervisor, tracking whether it was opened, and reading the reply",
    "pi-home.html": "the Device Home page — the checkpoint device's own landing screen",
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
- See any safety notices issued about them, on that same records page. A
  notice is the formal write-up of a refusal, sent to whoever is
  responsible for them on site. They can read it and see what it says,
  but the reply belongs to the person it was addressed to, not to them.
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
- Site Cameras: live tiles from the site's fixed cameras, relayed through
  the checkpoint device rather than exposed directly.
- Safety Notices: the way a refusal leaves this system and reaches
  someone who does not have a login — a subcontractor's supervisor, a
  site manager, an agency. How it works:
  * Issue one against a worker, attaching the refusals it concerns. It
    gets a reference like SN-2026-0041 and a due date.
  * The recipient gets a link. No account, no password: the link itself
    is the access, it only opens that one notice, and it expires.
  * Send it by email if a mail server and public URL are configured;
    otherwise use "Mark sent" after sending the link yourself, so the
    record still says when it went out.
  * The recipient reads the notice, sees the evidence frames, and
    answers it — either accepting it, with a note on what they will do
    about it, or disputing it. A dispute must say why; that is the
    point of it.
  * Status is one of: issued (not opened yet), opened, overdue (past due
    and still unanswered), acknowledged, disputed, or withdrawn.
    "Withdraw" stops the link working, for a notice sent in error.
  * Each notice can be exported as JSON, and the whole list as CSV, so
    another system can consume it without screen-scraping.
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
        # Answer from the built-in guide rather than refusing. "Not
        # configured" is accurate and useless — most questions here are
        # "where is X", which does not need a model to answer.
        return _offline_answer(message, role, page), None

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
