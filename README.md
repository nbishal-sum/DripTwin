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
- **Talks to the nurse, hands-free.** Say *"Assistant, set 120 mL per hour, 20-drop set"* with
  gloves on: Cartesia Ink 2's turn detection hears when you've finished, no button needed.
  Hold-to-talk stays available for noisy rooms.
- **Reads every order back.** The assistant repeats the order with the drop rate and waits for
  *"confirm"*. The device itself refuses to start an order confirmed in the same breath as it was
  read back, or without a confirming word from the nurse.
- **Speaks alarms instantly.** "Line blocked" is spoken straight from the device event, with no AI
  in the path, in an urgent voice, over a connection that's already open. Answers use a calm voice
  and start playing while Claude is still writing them.
- **Says numbers and units clearly.** "mL/h", "gtt/min" and drug names are rewritten and given
  pronunciations before speech, because a misheard rate is a medication-error risk.
- **Speaks Spanish.** Switch the voice to Español, or keep English and have alarms repeated in Spanish.
- **Stays safe without the AI.** Hard limits live in the microcontroller firmware. The AI has no
  valve control at all, and the valve holds its position if the laptop disconnects.

## Built with

| | |
|---|---|
| **AI agent** | [Claude](https://www.anthropic.com) (Anthropic API, tool use), with Gemini as a fallback |
| **Voice** | [Cartesia](https://cartesia.ai): Ink 2 streaming speech-to-text with turn detection (hands-free), Ink-Whisper (hold-to-talk), Sonic streaming text-to-speech with emotion/speed control, a pronunciation dictionary, and Spanish |
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

Optional voice settings: `CARTESIA_VOICE_ID`, `CARTESIA_TTS_MODEL` (default `sonic-3.6`),
`CARTESIA_ALARM_EMOTION` (e.g. `angry`; by default Sonic takes the tone from the alarm wording) and
`CARTESIA_PRONUNCIATION_DICT_ID` (otherwise the dictionary is created on first run and its id
cached in `.cartesia_dict.json`).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m driptwin.server --sim          # no hardware: simulated drip
```

Open **http://localhost:8000**. Other modes:

```bash
.venv/bin/python -m driptwin.vision --camera 0 --calibrate      # a = find the splash box (or drag one), s = save
.venv/bin/python -m driptwin.server --monitor --camera 0        # webcam only: counts drops
.venv/bin/python -m driptwin.server --port /dev/tty.usbmodem101 --camera 0   # full system with the Pico
```

### Valve calibration

The pinch valve's response depends on the print and the tubing, so measure it once per build.
Run this on a blank Pico (no `main.py`) with the line dripping:

```bash
.venv/bin/python -m driptwin.valvecal --port COM5 --camera 1 --angles 0:90:10      # coarse: where does it pinch?
.venv/bin/python -m driptwin.valvecal --port COM5 --camera 1 --angles 72:58:1      # fine: the steep part
```

It writes `valve_cal.json` (angle → drops/min → mL/h). With `--ws ws://127.0.0.1:8000/ws` it reads
drops from a running monitor server instead of opening the camera. The full system loads the file at
start-up and uses the measured curve in place of the generic valve model. Set `OPEN_DEG` / `CLOSED_DEG`
in `firmware/main.py` to the same angles before copying the firmware to the Pico.

The controller waits for fresh drops after every move, then steps toward the angle the table gives
for the target flow. It holds once within ±5 % of the target and starts correcting again only past ±8 %.

## How it works

```
webcam → drop detector → drip rate → digital twin → pinch valve (via Pico firmware)
                                          ↓
                    dashboard + voice assistant (Claude + Cartesia) + log (Tiger Data)
```

| Part | File | Idea |
|---|---|---|
| Vision | `driptwin/vision.py` | Frame-to-frame change in a box over the splash zone (found automatically if none is saved), with a self-tuning threshold |
| Rate | `driptwin/rate.py` | Median time between drops, so one missed drop barely matters |
| Twin | `driptwin/physics.py` | `flow = (bag height − vein pressure) / (tubing + valve resistance)`, solved backwards for bag height |
| Control | `driptwin/twin.py` | Sets the valve from the model (measured valve table if `valve_cal.json` exists), holds inside a ±5 % band. A sudden change in bag height means the bag moved. If drops stop, it opens the valve briefly to tell a kink from an empty bag |
| Safety | `firmware/main.py` | Starts closed, limits valve travel and speed, holds position if the laptop goes quiet, hardware watchdog |
| Agent | `driptwin/agent.py` | Claude with narrow tools, order read-back and confirmation, no valve tool |
| Voice | `driptwin/voice.py` | Sonic over one persistent WebSocket, calm/alarm voice styles, unit rewriting and pronunciation dictionary, spoken alarm lines in English and Spanish |

## Tests

```bash
.venv/bin/python -m pytest -q tests                                   # control scenarios + detector
.venv/bin/python -m driptwin.groundtruth record --camera 0 --name run1 # record and mark drops by hand
.venv/bin/python -m driptwin.groundtruth eval recordings/run1.mp4     # score the detector
```

> Hackathon prototype. Not a medical device and not for use on patients.
