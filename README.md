# DripTwin

**Pump-like safety for gravity IV drips, using a webcam, a small microcontroller and a servo.**

Gravity IV drips are used everywhere infusion pumps are too expensive or unavailable: field
hospitals, rural clinics, disaster response and veterinary care. Today they're set by hand and checked by eye.
DripTwin watches the drip chamber, counts every drop, works out *why* the flow changed, corrects
the flow, and answers the nurse by voice.

Built at **Cal Hacks 13.0** (October 2026).

## What it does

- **Counts drops with a webcam.** OpenCV detects each drop splashing into the chamber.
  On a hand-counted 2-minute recording, it caught **25 of 25 drops with no false counts**.
- **Explains changes with a physics "digital twin".** A model of the IV line works backwards
  from the drop rate to estimate the bag height. It can tell a lowered bag apart from a kinked line
  or an empty bag.
- **Corrects the flow.** A servo pinch valve adjusts the tubing to hold the ordered rate.
- **Talks to the nurse, in English or Spanish.** Say *"Assistant, how many drops so far?"* (or
  *"Asistente, ¿cuántas gotas van?"*) and it answers out loud. Orders are read back and need a
  spoken confirmation before anything starts.
- **Speaks up when something is wrong.** A kinked line, an empty bag or free flow is announced
  out loud the moment it's detected, so a nurse across the room hears it.
- **Stays safe without the AI.** Hard limits live in the microcontroller firmware. The AI has no
  valve control at all, and the valve holds its position if the laptop disconnects.

## Built with

| | |
|---|---|
| **AI agent** | [Claude](https://www.anthropic.com) (Anthropic API, tool use), with Gemini as a fallback |
| **Voice** | [Cartesia](https://cartesia.ai): the nurse gives commands and asks questions by voice (Ink-Whisper speech-to-text), and DripTwin answers and announces alarms out loud (Sonic text-to-speech, streamed so speech starts in about 0.3 s). English and Spanish |
| **Data** | [Tiger Data](https://www.tigerdata.com) / TimescaleDB hypertables (SQLite when offline) |
| **Vision** | Python, OpenCV, NumPy |
| **Backend** | FastAPI, WebSockets, uvicorn |
| **Frontend** | Plain HTML, CSS and JavaScript (no build step) |
| **Hardware** | Raspberry Pi Pico (MicroPython), SG90 servo pinch valve, buzzer, USB webcam |

## Requirements

- Python 3.10 or newer and a USB webcam
- **Cartesia API key** (`CARTESIA_API_KEY`): the voice of DripTwin. With it, a nurse can run
  DripTwin completely hands-free: give commands (*"Assistant, start 120 mL per hour"*), ask
  questions (*"Assistant, how many drops so far?"*), and hear every answer spoken back.
  Without it, questions can still be typed into the dashboard.
- **Anthropic API key** (`ANTHROPIC_API_KEY`): the Claude assistant that understands the
  request and decides what to do. Without it, DripTwin gives short scripted status replies.
- Optional: `DATABASE_URL` for Tiger Data / TimescaleDB logging (SQLite is used otherwise)
- Optional hardware: Raspberry Pi Pico, SG90 servo pinch valve, buzzer

Put the keys in a `.env` file in the project folder:

```
CARTESIA_API_KEY=...
ANTHROPIC_API_KEY=...
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m driptwin.server --sim          # no hardware: simulated drip
```

Open **http://localhost:8000**. Other modes:

```bash
.venv/bin/python -m driptwin.vision --camera 0 --calibrate      # draw a box over the drip chamber, press s
.venv/bin/python -m driptwin.server --monitor --camera 0        # webcam only: counts drops
.venv/bin/python -m driptwin.server --port /dev/tty.usbmodem101 --camera 0   # full system with the Pico
```

## How it works

```
webcam → drop detector → drip rate → digital twin → pinch valve (via Pico firmware)
                                          ↓
                    dashboard + voice assistant (Claude + Cartesia) + log (Tiger Data)
```

| Part | File | Idea |
|---|---|---|
| Vision | `driptwin/vision.py` | Frame-to-frame change in a box over the splash zone, with a self-tuning threshold |
| Rate | `driptwin/rate.py` | Median time between drops, so one missed drop barely matters |
| Twin | `driptwin/physics.py` | `flow = (bag height − vein pressure) / (tubing + valve resistance)`, solved backwards for bag height |
| Control | `driptwin/twin.py` | Sets the valve from the model. A sudden change in bag height means the bag moved. If drops stop, it opens the valve briefly to tell a kink from an empty bag |
| Safety | `firmware/main.py` | Starts closed, limits valve travel and speed, holds position if the laptop goes quiet, hardware watchdog |
| Agent | `driptwin/agent.py` | Claude with narrow tools, order read-back and confirmation, no valve tool |

## Tests

```bash
.venv/bin/python -m pytest -q tests                                   # control scenarios + detector
.venv/bin/python -m driptwin.groundtruth record --camera 0 --name run1 # record and mark drops by hand
.venv/bin/python -m driptwin.groundtruth eval recordings/run1.mp4     # score the detector
```

> Hackathon prototype. Not a medical device and not for use on patients.
