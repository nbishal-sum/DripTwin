"""Cartesia speech: Ink-Whisper for speech-to-text, Sonic for text-to-speech.
Audio is captured and played in the browser, so the laptop needs no audio deps."""

import os

import httpx

API = "https://api.cartesia.ai"
VERSION = "2026-08-14"
TTS_MODEL = os.environ.get("CARTESIA_TTS_MODEL", "sonic-3.6")
VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4")


def available() -> bool:
    return bool(os.environ.get("CARTESIA_API_KEY"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['CARTESIA_API_KEY']}",
            "Cartesia-Version": VERSION}


async def transcribe(audio: bytes, filename: str = "speech.webm") -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/stt", headers=_headers(),
                         files={"file": (filename, audio)},
                         data={"model": "ink-whisper", "language": "en"})
        r.raise_for_status()
        return r.json()["text"].strip()


async def speak(text: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/tts/bytes", headers=_headers(), json={
            "model_id": TTS_MODEL,
            "transcript": text,
            "voice": {"id": VOICE_ID},
            "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
            "language": "en",
        })
        r.raise_for_status()
        return r.content
