"""FastAPI server: live dashboard, voice endpoint, demo disturbance buttons.

    python -m driptwin.server --sim                      # no hardware
    python -m driptwin.server --port /dev/tty.usbmodem101 --camera 0
    python -m driptwin.server --monitor --camera 0       # webcam drop count only, no valve
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
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
    speak: bool = True


async def _reply(text: str, speak: bool) -> dict:
    if agent is None:
        reply = offline_explain(rt.twin)
    else:
        try:
            reply = await asyncio.to_thread(agent.ask, text)
        except Exception as e:      # network / API failure must not break the device
            reply = "I can't reach the assistant right now. " + offline_explain(rt.twin)
            print("[agent]", repr(e))
    out = {"reply": reply}
    if speak and voice.available():
        try:
            out["audio_b64"] = base64.b64encode(await voice.speak(reply)).decode()
        except Exception as e:
            print("[tts]", repr(e))
    return out


@app.post("/api/ask")
async def ask(body: Say):
    return await _reply(body.text, body.speak)


# "assistant", "hey assistant", "Cartesia AI assistant"... at the start of an utterance.
# The leading "a" is optional because the browser can clip the first syllable.
WAKE = re.compile(r"^\W*(?:(?:hey|hi|ok|okay)\W+)?(?:cartesia\W+)?(?:a\.?\s?i\.?\W+)?"
                  r"(?:a?ss?ist[ae]n(?:t|ts|ce)|a\s+sis\s?tant|assist\s+and)\b\W*", re.I)


def split_wake(transcript: str) -> tuple[bool, str]:
    """(wake word heard, the question after it)."""
    m = WAKE.match(transcript)
    return (True, transcript[m.end():].strip()) if m else (False, "")


@app.post("/api/voice")
async def voice_in(audio: UploadFile, wake: bool = Form(False)):
    """Push-to-talk sends wake=false. Hands-free mode sends wake=true for every
    utterance it hears, and only ones starting with the wake word reach the agent."""
    if not voice.available():
        return JSONResponse({"error": "CARTESIA_API_KEY not set"}, 400)
    try:
        transcript = await voice.transcribe(await audio.read(), audio.filename or "speech.webm")
    except Exception as e:
        print(f"[voice] speech-to-text failed: {e!r}")
        return JSONResponse({"error": f"speech-to-text failed: {e}"}, 502)
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
    print(f"[voice] heard {transcript!r}: asking the agent")
    out = await _reply(transcript, True)
    print(f"[voice] reply {out['reply']!r}" + ("" if "audio_b64" in out else " (no audio!)"))
    return {"transcript": transcript, **out}


@app.post("/api/announce")
async def announce(body: Say):
    """Speak a device event verbatim (alarms must not wait on the LLM)."""
    if not voice.available():
        return {}
    return {"audio_b64": base64.b64encode(await voice.speak(body.text)).decode()}


class OrderIn(BaseModel):
    rate_ml_h: float
    drop_factor: int
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
