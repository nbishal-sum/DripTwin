"""FastAPI server: live dashboard, voice endpoint, demo disturbance buttons.

    python -m driptwin.server --sim                      # no hardware
    python -m driptwin.server --port /dev/tty.usbmodem101 --camera 0
    python -m driptwin.server --monitor --camera 0       # webcam drop count only, no valve
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import voice
from .agent import Agent, GeminiAgent, offline_explain
from .runtime import Runtime
from .twin import Order, OrderRejected

WEB = Path(__file__).resolve().parent.parent / "web"
ENV_FILE = WEB.parent / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=value lines from .env into os.environ. Variables already set in
    the shell win, and empty values are skipped so they don't mask a real key."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.removeprefix("export ").split("=", 1)
        key, val = key.strip(), val.strip().strip("'\"")
        if val and not os.environ.get(key):
            os.environ[key] = val
ARGS = argparse.Namespace(sim=True, port=None, camera=0, head=None, monitor=False)
rt: Runtime
agent: Agent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global rt, agent
    rt = Runtime(sim=ARGS.sim, port=ARGS.port, camera=ARGS.camera, head_cm=ARGS.head,
                 monitor=ARGS.monitor)
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        agent = Agent(rt.twin, clock=rt.now, on_log=rt.log, lock=rt.lock)
    elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        agent = GeminiAgent(rt.twin, clock=rt.now, on_log=rt.log, lock=rt.lock)
    print(f"[agent] {agent.NAME if agent else 'none (no API key): scripted status replies'}")
    task = asyncio.create_task(rt.run())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")     # dashboard CSS


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue()
    rt.subscribers.add(q)
    try:
        await sock.send_json({"type": "hello", "agent": agent is not None,
                              "agent_name": agent.NAME if agent else "Claude",
                              "voice": voice.available(), "store": rt.store.kind,
                              "sim": rt.sim, "monitor": rt.monitor})
        while True:
            await sock.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        rt.subscribers.discard(q)


@app.get("/video.mjpg")
async def video():
    async def gen():
        last = None
        while True:
            jpg = rt.device.jpeg
            if jpg is not None and jpg is not last:
                last = jpg
                yield b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(1 / 20)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=f")


@app.get("/frame.jpg")
def frame():
    """Latest annotated frame. The dashboard polls this rather than the MJPEG
    stream, which some browsers (Safari) stop updating after a few seconds."""
    jpg = rt.device.jpeg
    if jpg is None:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class Say(BaseModel):
    text: str
    lang: str = "en"


def _lang(lang: str) -> str:
    return lang if lang in voice.LANGUAGES else "en"


async def _reply(text: str, lang: str) -> dict:
    """Agent reply plus how long it took. Speech is fetched separately from
    /api/speak, so the browser can start playing it while it streams."""
    t = time.perf_counter()
    if agent is None:
        reply = offline_explain(rt.twin)
    else:
        try:
            reply = await asyncio.to_thread(agent.ask, text, lang)
        except Exception as e:      # network / API failure must not break the device
            reply = "I can't reach the assistant right now. " + offline_explain(rt.twin)
            print("[agent]", repr(e))
    return {"reply": reply, "agent_ms": round((time.perf_counter() - t) * 1000)}


@app.post("/api/ask")
async def ask(body: Say):
    return await _reply(body.text, _lang(body.lang))


# "assistant", "hey assistant", "Cartesia AI assistant", "oye asistente"... at the start of an
# utterance. The leading "a" is optional because the browser can clip the first syllable.
WAKE = re.compile(r"^\W*(?:(?:hey|hi|ok|okay|oye|hola)\W+)?(?:cartesia\W+)?(?:a\.?\s?i\.?\W+)?"
                  r"(?:a?ss?ist[ae]n(?:t|ts|ce)|a\s+sis\s?tant|assist\s+and|a?sis?tente)\b[^\w¿¡]*", re.I)   # keeps Spanish ¿ ¡


def split_wake(transcript: str) -> tuple[bool, str]:
    """(wake word heard, the question after it)."""
    m = WAKE.match(transcript)
    return (True, transcript[m.end():].strip()) if m else (False, "")


@app.post("/api/voice")
async def voice_in(audio: UploadFile, wake: bool = Form(False), lang: str = Form("en")):
    """Push-to-talk sends wake=false. Hands-free mode sends wake=true for every
    utterance it hears, and only ones starting with the wake word reach the agent."""
    if not voice.available():
        return JSONResponse({"error": "CARTESIA_API_KEY not set"}, 400)
    lang = _lang(lang)
    t = time.perf_counter()
    try:
        transcript = await voice.transcribe(await audio.read(), audio.filename or "speech.webm", lang)
    except Exception as e:
        print(f"[voice] speech-to-text failed: {e!r}")
        return JSONResponse({"error": f"speech-to-text failed: {e}"}, 502)
    stt_ms = round((time.perf_counter() - t) * 1000)
    if not transcript:
        print("[voice] heard nothing")
        return {"transcript": "", "reply": ""}
    if wake:
        heard, question = split_wake(transcript)
        if not heard:
            print(f"[voice] heard {transcript!r}: ignored, no wake word")
            return {"transcript": transcript, "ignored": True}
        if not question:                  # just "assistant": the browser beeps and waits
            print(f"[voice] heard {transcript!r}: wake word, waiting for the question")
            return {"transcript": transcript, "wake": True, "reply": ""}
        transcript = question
    print(f"[voice] heard {transcript!r} ({lang}, {stt_ms} ms): asking the agent")
    out = await _reply(transcript, lang)
    print(f"[voice] reply {out['reply']!r} ({out['agent_ms']} ms)")
    return {"transcript": transcript, "stt_ms": stt_ms, **out}


async def _stream(text: str, lang: str, urgent: bool = False):
    """Raw PCM straight from Cartesia. The first chunk is awaited here so a
    failure is a clean 502 the browser can fall back from, not a cut-off stream."""
    if not voice.available():
        return JSONResponse({"error": "CARTESIA_API_KEY not set"}, 400)
    chunks = voice.speak_stream(text, lang, urgent)
    try:
        first = await anext(chunks)
    except Exception as e:
        print("[tts]", repr(e))
        return JSONResponse({"error": f"text-to-speech failed: {e}"}, 502)

    async def body():
        yield first
        try:
            async for c in chunks:
                yield c
        except Exception as e:
            print("[tts] stream cut off:", repr(e))
    return StreamingResponse(body(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(voice.SAMPLE_RATE),
                                      "Cache-Control": "no-store"})


@app.post("/api/speak")
async def speak(body: Say):
    return await _stream(body.text, _lang(body.lang))


# The only speech nobody asked for: real alarms, spoken the moment they fire. Short, says what
# to do, never waits on the LLM. Warnings and notices stay on screen; "Assistant, what
# happened?" gets the details.
ANNOUNCE = {
    "occlusion": ("Alarm. No flow. The line may be kinked or clamped. Check the tubing and the IV site.",
                  "Alarma. No hay flujo. La vía puede estar doblada o pinzada. Revise el tubo y el sitio de la vía."),
    "free_flow": ("Alarm. Free flow. The valve is closed. Check the tubing is seated in the clamp.",
                  "Alarma. Flujo libre. La válvula está cerrada. Revise que el tubo esté bien puesto en la pinza."),
    "empty_bag": ("Alarm. The bag looks empty. The valve is closed. Replace the bag.",
                  "Alarma. La bolsa parece vacía. La válvula está cerrada. Cambie la bolsa."),
    "fw_host_lost": ("Alarm. The clamp controller lost contact and is holding its position. Check the line by hand.",
                     "Alarma. El controlador de la pinza perdió la conexión y mantiene su posición. Revise la vía a mano."),
}


class Announce(BaseModel):
    kind: str
    lang: str = "en"


@app.post("/api/announce")
async def announce(body: Announce):
    phrases = ANNOUNCE.get(body.kind)
    if phrases is None:
        return Response(status_code=204)     # not an alarm: stays silent
    lang = _lang(body.lang)
    return await _stream(phrases[lang == "es"], lang, urgent=True)


class OrderIn(BaseModel):
    rate_ml_h: float
    vtbi_ml: float
    bag_volume_ml: float
    drug: str = "fluid"


@app.post("/api/order")
def order(body: OrderIn):
    """Manual entry fallback, same validation as the agent path."""
    try:
        with rt.lock:
            rt.twin.start(Order(**body.model_dump()), rt.now())
    except OrderRejected as e:
        return JSONResponse({"error": str(e)}, 400)
    rt.log("order", {**body.model_dump(), "confirmation": "manual entry"})
    return {"ok": True}


@app.post("/api/{action}")
def control(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"error": "unknown"}, 404)
    with rt.lock:
        getattr(rt.twin, action)(rt.now())
    return {"mode": rt.twin.mode.value}


@app.post("/api/sim/{action}")
def sim(action: str):
    return {"result": rt.device.disturb(action)}


def main():
    import uvicorn
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="simulated plant + synthetic camera")
    ap.add_argument("--port", help="Pico serial port, e.g. /dev/tty.usbmodem101")
    ap.add_argument("--monitor", action="store_true",
                    help="webcam drop counting only: no Pico, no valve")
    ap.add_argument("--camera", default="0", help="webcam index or video file; 'none' to disable")
    ap.add_argument("--head", type=float, help="bag height above the site in cm")
    ap.add_argument("--http", type=int, default=8000)
    a = ap.parse_args()
    if sum(map(bool, (a.sim, a.port, a.monitor))) != 1:
        ap.error("pass exactly one of --sim, --port or --monitor")
    ARGS.sim, ARGS.port, ARGS.head, ARGS.monitor = a.sim, a.port, a.head, a.monitor
    ARGS.camera = None if a.camera == "none" else int(a.camera) if a.camera.isdigit() else a.camera
    if a.monitor and ARGS.camera is None:
        ap.error("--monitor needs a camera")
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", a.http)) == 0:
            raise SystemExit(
                f"Port {a.http} is already in use: another DripTwin server is probably still running "
                f"(often one paused with Ctrl+Z). Stop it with:  pkill -9 -f driptwin.server\n"
                f"or start this one on another port:  --http {a.http + 1}")
    uvicorn.run(app, host="0.0.0.0", port=a.http)


if __name__ == "__main__":
    main()
