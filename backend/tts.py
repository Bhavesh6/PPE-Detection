"""Spoken gate announcements via ElevenLabs, cached on disk.

The gate speaks a small, fixed set of sentences — "Access granted." fires
on every clean pass, "Access denied" variants repeat all day — so each
phrase is synthesized once and served from disk after that, rather than
paying ElevenLabs and waiting on the network for a sentence it has already
said. Text is the cache key (with the voice, since the same sentence in a
different voice is a different file, not a hit).

If no API key is configured, or a request to ElevenLabs fails, this simply
has nothing to serve. The frontend already falls back to the browser's own
speechSynthesis when the request comes back empty, so a missing or
exhausted key never blocks the gate itself — it only makes the gate sound
robotic again, which is the state everyone was already used to.

A 402 "Free users cannot use library voices via the API" here doesn't mean
the plan needs upgrading — it means ELEVENLABS_VOICE_ID points at a voice
that's only been viewed or favorited in the Voice Library, not actually
added to the account. Open the voice at elevenlabs.io/app/voice-library,
click "Add to my voices" so it shows up on /app/voices, then use that same
ID — free-tier API access works fine for a voice added that way.
"""

import hashlib
import os

import requests
from flask import current_app

MODEL_ID = "eleven_turbo_v2_5"  # spoken once at a gate, not narrated for later — latency over polish


def enabled():
    return bool(current_app.config["ELEVENLABS_API_KEY"])


def _cache_dir():
    path = current_app.config["SPEECH_CACHE_DIR"]
    os.makedirs(path, exist_ok=True)
    return path


def _cache_file(text, voice_id):
    digest = hashlib.sha256(f"{voice_id}:{text}".encode("utf-8")).hexdigest()[:32]
    return os.path.join(_cache_dir(), f"{digest}.mp3")


def synthesize(text):
    """Return (audio_bytes, error). Cached to disk after the first call per phrase."""
    text = (text or "").strip()
    if not text:
        return None, "No text supplied"
    if len(text) > 300:
        return None, "Text too long"
    if not enabled():
        return None, "ElevenLabs is not configured"

    voice_id = current_app.config["ELEVENLABS_VOICE_ID"]
    path = _cache_file(text, voice_id)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read(), None

    try:
        res = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": current_app.config["ELEVENLABS_API_KEY"],
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": MODEL_ID,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        current_app.logger.warning("ElevenLabs request failed: %s", exc)
        return None, "Could not reach ElevenLabs"

    if not res.ok:
        current_app.logger.warning("ElevenLabs returned %s: %s", res.status_code, res.text[:200])
        return None, f"ElevenLabs returned {res.status_code}"

    audio = res.content
    try:
        with open(path, "wb") as f:
            f.write(audio)
    except OSError:
        current_app.logger.exception("Could not cache synthesized speech")
    return audio, None
