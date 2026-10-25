"""Cartesia speech: Ink-Whisper for speech-to-text, Sonic for text-to-speech.
Audio is captured and played in the browser, so the laptop needs no audio deps.

Speech is streamed (Sonic over SSE) as raw 16-bit PCM: the browser starts
playing the first chunk ~0.2 s after the request instead of waiting for the
whole reply to be synthesised."""

import base64
import json
import os
from typing import AsyncIterator

import httpx

API = "https://api.cartesia.ai"
VERSION = "2026-08-14"
TTS_MODEL = os.environ.get("CARTESIA_TTS_MODEL", "sonic-3.6")
VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4")
# Skylar (the default) speaks Spanish too; set this for a native Spanish voice.
VOICE_ID_ES = os.environ.get("CARTESIA_VOICE_ID_ES", VOICE_ID)
SAMPLE_RATE = 24000
LANGUAGES = ("en", "es")


def available() -> bool:
    return bool(os.environ.get("CARTESIA_API_KEY"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['CARTESIA_API_KEY']}",
            "Cartesia-Version": VERSION}


async def transcribe(audio: bytes, filename: str = "speech.webm", language: str = "en") -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/stt", headers=_headers(),
                         files={"file": (filename, audio)},
                         data={"model": "ink-whisper", "language": language})
        r.raise_for_status()
        return r.json()["text"].strip()


async def speak_stream(text: str, language: str = "en", urgent: bool = False) -> AsyncIterator[bytes]:
    """Yields raw PCM (s16le, mono, SAMPLE_RATE) as Sonic generates it.
    Alarms are spoken louder and a little faster."""
    body = {
        "model_id": TTS_MODEL,
        "transcript": text,
        "voice": {"id": VOICE_ID_ES if language == "es" else VOICE_ID},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "language": language,
    }
    if urgent:
        body["generation_config"] = {"volume": 1.6, "speed": 1.05}
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("POST", f"{API}/tts/sse", headers=_headers(), json=body) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"Cartesia TTS {r.status_code}: {(await r.aread())[:300]!r}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                if ev["type"] == "chunk":
                    yield base64.b64decode(ev["data"])
                elif ev["type"] == "error":
                    raise RuntimeError(f"Cartesia TTS: {ev.get('message')}")
                elif ev["type"] == "done":
                    return
