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
- **Measures flow, not just drops.** Drop size changes with drip speed, so DripTwin converts
  drops to mL with a curve measured on our own drip set instead of a fixed drops-per-mL.
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
.venv/bin/python -m driptwin.selfcheck                          # before a demo: test the API keys and voice
```

## Using the voice assistant

- **Hands-free, always on.** The dashboard listens all the time, but only acts when you start
  with the wake word: *"Assistant, ..."* or *"Asistente, ..."*. Anything else is ignored.
- **Hold to talk.** You can also hold the mic button, or the space bar, and speak without the wake word.
- **Pick the language** (English or Español) in the dashboard. DripTwin listens and replies in that language.
- **It never speaks on its own**, except for real alarms: occlusion (kinked line), free flow,
  empty bag, and lost connection to the laptop. Warnings stay on screen. Typed questions get a
  written answer only.
- **Orders need confirmation.** *"Assistant, run saline at 100 mL an hour, 100 mL total"* is read back,
  and nothing starts until you say *"yes"*.

## From drops to mL

Our drip set's drops get bigger as they fall faster, so a fixed drops-per-mL would be about 27% off.
We measured the flow `Q` (mL/s) against the drop frequency `f` (drops/s) and use the fitted curve:

```
Q(f) = 0.06341·f + 0.02416·f⁵ / (f⁴ + 2.688⁴)
```

The controller inverts it to set the target, so 100 mL/h means 26.3 drops/min and 150 mL/h means
39.4 drops/min (`driptwin/physics.py`). The curve belongs to this drip set, so a different set needs measuring again.

## Data log (Tiger Data)

With `DATABASE_URL` set, DripTwin writes to Tiger Data (TimescaleDB):

- `telemetry` (a hypertable, every 2 s): drip rate, target, valve position, estimated bag height, mL given, mode
- `events`: orders, confirmations, alarms, probes and bag moves, including how each order was confirmed

Each run gets an `infusion_id`: `inf-...` for real runs and `sim-...` for the simulator.
Example query for the rate per minute of the latest run:

```sql
SELECT time_bucket('1 minute', time) AS minute, avg(rate_gtt_min) AS drops_per_min
FROM telemetry
WHERE infusion_id = (SELECT infusion_id FROM telemetry ORDER BY time DESC LIMIT 1)
GROUP BY minute ORDER BY minute;
```

### Holding a flow on the rig

```bash
.venv/bin/python -m driptwin.flowctl 95          # hold 95 mL/h; type a new rate + Enter to change it, Ctrl+C to stop
.venv/bin/python -m driptwin.flowctl 95 --camera 1   # count drops with the webcam instead of a running monitor server
```

It loads `firmware/main.py` into the Pico's RAM (the Pico must be at a blank REPL), starts with the
valve shut, opens fully to free the tube, then closes in on the right angle: it keeps the last
too-fast and too-slow angles and tries halfway between them. On exit it opens the valve.

Known limit of our current pinch valve: near its cut-off the flow drops from about 40 to about
7 drops/min within about 0.1°, finer than an SG90 can position, so steady rates in between can't be
held. A valve whose flow changes over several degrees (longer lever, cam, wider or softer pinch)
fixes this; the controller needs no change.

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

Optional keys in `valve_cal.json` describe how the rig behaves (values from our bench rig):

| Key | Ours | Meaning |
|---|---|---|
| `open_deg` / `closed_deg` | 40 / 68 | Valve travel. 40° frees a squashed tube, 68° shuts it |
| `backlash_deg` | 1.5 | Opening moves go this far past the target, then close onto it (the tube sticks) |
| `settle_s` | 8 | Wait after a move before trusting drops (the drip chamber buffers changes) |
| `rate_window` | 16 | Rate = mean interval over this many drops; for drips that come in bursts near the pinch |
| `step_deg` | 0.5 | Largest correction per step |
| `stall_s` | 25 | Gap that counts as "drops stopped" |
| `head_alert_cm` | 30 | Implied bag-height jump reported as "bag moved" |

`--min-interval 0.8` on `valvecal` ignores a second count within 0.8 s (a splash's ripple).

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
.venv/bin/python -m pytest -q tests                                   # control, detector and voice rules
.venv/bin/python -m driptwin.groundtruth record --camera 0 --name run1 # record and mark drops by hand
.venv/bin/python -m driptwin.groundtruth eval recordings/run1.mp4     # score the detector
```

> Hackathon prototype. Not a medical device and not for use on patients.
