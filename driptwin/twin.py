"""The DripTwin core: order -> model-inverse controller -> diagnostics.

Runs a fixed-period tick. Each tick:
  1. estimate drop rate (fresh drops only, since the last valve move)
  2. update the digital twin's implied head
  3. classify what is going on (head change, occlusion, empty bag, ...)
  4. choose a valve position (model inverse, slew-limited)

Pure logic, no I/O: the same class drives the simulator, the real Pico,
and the unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
import math

from .config import CONFIG, Config
from .physics import (HeadEstimator, ml_h_to_gtt_min, valve_for_flow,
                      max_flow_ml_min)
from .rate import DropLog, RateEstimator, RateEstimate


class Mode(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PROBING = "probing"            # testing whether opening the valve restores flow
    PAUSED = "paused"
    COMPLETE = "complete"
    ALARM_OCCLUSION = "alarm_occlusion"
    ALARM_EMPTY = "alarm_empty"
    ALARM_FREE_FLOW = "alarm_free_flow"
    ALARM_LOW_HEAD = "alarm_low_head"


ALARMS = {Mode.ALARM_OCCLUSION, Mode.ALARM_EMPTY, Mode.ALARM_FREE_FLOW}


@dataclass
class Order:
    rate_ml_h: float
    drop_factor: int
    vtbi_ml: float          # volume to be infused
    bag_volume_ml: float
    drug: str = "fluid"

    @property
    def target_gtt_min(self) -> float:
        return ml_h_to_gtt_min(self.rate_ml_h, self.drop_factor)

    @property
    def target_ml_min(self) -> float:
        return self.rate_ml_h / 60.0


@dataclass
class Event:
    t: float
    kind: str
    severity: str           # info | warn | alarm
    message: str
    data: dict = field(default_factory=dict)


class OrderRejected(ValueError):
    pass


def validate_order(order: Order, cfg: Config = CONFIG) -> None:
    s = cfg.safety
    if not (s.min_rate_ml_h <= order.rate_ml_h <= s.max_rate_ml_h):
        raise OrderRejected(
            f"rate {order.rate_ml_h} mL/h outside {s.min_rate_ml_h}-{s.max_rate_ml_h} mL/h")
    if order.drop_factor not in s.allowed_drop_factors:
        raise OrderRejected(
            f"drop factor {order.drop_factor} not in {s.allowed_drop_factors} gtt/mL")
    if not (0 < order.vtbi_ml <= min(order.bag_volume_ml, s.max_volume_ml)):
        raise OrderRejected(
            f"VTBI {order.vtbi_ml} mL must be >0 and <= bag volume {order.bag_volume_ml} mL")


class DripTwin:
    def __init__(self, cfg: Config = CONFIG, head_cm: float | None = None,
                 monitor_only: bool = False):
        self.cfg = cfg
        self.monitor_only = monitor_only   # webcam only, no valve: count drops, never infuse
        self.drop_log = DropLog()
        self.rater = RateEstimator(cfg.rate_window_drops)
        self.head0 = head_cm or cfg.default_head_cm
        self.est = HeadEstimator(self.head0, 1000.0, cfg.physics)
        self.order: Order | None = None
        self.mode = Mode.IDLE
        self.u = 1.0                     # start closed
        self.t = 0.0
        self.t_move = 0.0                # last significant valve move
        self.t_started = 0.0
        self.events: list[Event] = []
        self.rate = RateEstimate(0.0, False, math.inf, 0)
        self._probe: dict | None = None
        self._settled = False
        self._history: list[dict] = []
        self._flowing, self._ref_rate = False, 0.0     # monitor-mode narration state

    # ---- inputs -----------------------------------------------------------
    def on_drop(self, t: float) -> None:
        self.drop_log.add(t)

    def start(self, order: Order, t: float) -> None:
        if self.monitor_only:
            raise OrderRejected("monitor-only mode: no valve is connected, so drops are "
                                "counted but an infusion can't be run")
        validate_order(order, self.cfg)
        self.order = order
        self.est = HeadEstimator(self.head0, order.bag_volume_ml, self.cfg.physics)
        self.mode = Mode.RUNNING
        self.t_started = t
        self._settled = False
        # Feed-forward from the model: start where physics says we should be.
        self._move(valve_for_flow(self.head0, order.target_ml_min, self.cfg.physics), t)
        self._emit(t, "order_started", "info",
                   f"Started {order.rate_ml_h:g} mL/h with a {order.drop_factor} gtt/mL set "
                   f"= {order.target_gtt_min:.1f} drops/min, VTBI {order.vtbi_ml:g} mL.",
                   asdict(order))

    def pause(self, t: float) -> None:
        if self.order:
            self.mode = Mode.PAUSED
            self._move(1.0, t)
            self._emit(t, "paused", "info", "Infusion paused, valve closed.")

    def resume(self, t: float) -> None:
        if self.order and self.mode in (Mode.PAUSED, *ALARMS, Mode.ALARM_LOW_HEAD):
            self.mode = Mode.RUNNING
            self._probe = None
            self._settled = False
            self._move(valve_for_flow(self.est.state.head_cm, self.order.target_ml_min,
                                      self.cfg.physics), t)
            self._emit(t, "resumed", "info", "Infusion resumed.")

    def set_head_hint(self, head_cm: float, t: float) -> None:
        """Nurse tells us the bag height; resets the twin's prior."""
        self.est.state.head_cm = self.est.state.head_fast_cm = head_cm
        self.est.state.head_inst_cm = head_cm
        self.est.state.head_baseline_cm = head_cm
        self._emit(t, "head_hint", "info", f"Bag height set to {head_cm:.0f} cm above the site.")

    # ---- main tick --------------------------------------------------------
    def tick(self, t: float) -> float:
        dt = t - self.t if self.t else self.cfg.control_period_s
        self.t = t
        self.rate = self.rater.estimate(self.drop_log.drops, t, since=self.t_move)
        if self.rate.changepoint is not None and self.rate.changepoint > self.t_move:
            # Flow stepped: forget pre-step intervals so the twin sees the new regime only.
            self.t_move = self.rate.changepoint
            self.rate = self.rater.estimate(self.drop_log.drops, t, since=self.t_move)
        if self.monitor_only:
            self._narrate(t)
            return self.u
        o = self.order
        if o is None or self.mode in (Mode.IDLE, Mode.PAUSED, Mode.COMPLETE):
            return self.u

        q = self.rate.gtt_min / o.drop_factor           # mL/min
        s = self.est.update(q, self.u, dt, self.rate.confident)
        self._record(t, q)

        if s.infused_ml >= o.vtbi_ml:
            self.mode = Mode.COMPLETE
            self._move(1.0, t)
            self._emit(t, "complete", "info",
                       f"VTBI reached: {s.infused_ml:.0f} mL infused. Valve closed.")
            return self.u

        if self.mode in ALARMS:
            return self.u

        self._check_free_flow(t, o)
        if self.mode in ALARMS:
            return self.u
        if self._check_stopped(t, o):
            return self.u
        self._check_head_change(t)
        self._control(t, o)
        return self.u

    # ---- control ----------------------------------------------------------
    def _control(self, t: float, o: Order) -> None:
        if not self.rate.confident:
            return
        err = (self.rate.gtt_min - o.target_gtt_min) / o.target_gtt_min
        head = self.est.state.head_inst_cm
        if abs(err) < 0.03:
            if not self._settled:
                self._settled = True
                self.est.rebaseline()
            if self.mode == Mode.ALARM_LOW_HEAD:
                self.mode = Mode.RUNNING
            return
        self._settled = False
        u_star = valve_for_flow(head, o.target_ml_min, self.cfg.physics)
        if u_star <= 0.0 and max_flow_ml_min(head, self.cfg.physics) < 0.95 * o.target_ml_min:
            p = self.cfg.physics
            need = o.target_ml_min * p.r0 + p.p_venous_cmh2o
            if self.mode != Mode.ALARM_LOW_HEAD:
                self.mode = Mode.ALARM_LOW_HEAD
                self._emit(t, "low_head", "warn",
                           f"Valve fully open but the bag is too low to reach "
                           f"{o.rate_ml_h:g} mL/h. Raise the bag about "
                           f"{max(0, need - head):.0f} cm (to ~{need:.0f} cm above the site).",
                           {"head_cm": head, "needed_cm": need})
        # Under-relax to avoid chasing noise in the head estimate.
        self._move(self.u + 0.7 * (u_star - self.u), t)

    def _move(self, u_target: float, t: float) -> None:
        s = self.cfg.safety
        u_target = min(s.u_max, max(s.u_min, u_target))
        du = abs(u_target - self.u)
        if du > 0.002:
            # Data is only fresh once the slew-limited servo has arrived and the
            # chamber has settled; drops before that reflect an in-between valve.
            self.t_move = t + du / s.max_slew_per_s + 1.0
        self.u = u_target   # slew limiting is enforced by the device layer + firmware

    # ---- diagnostics ------------------------------------------------------
    def _check_head_change(self, t: float) -> None:
        if not self.rate.confident:
            return
        s = self.est.state
        delta = s.head_inst_cm - s.head_baseline_cm
        if abs(delta) > 10.0 and not self._recent("head_change", t, 20):
            direction = "lowered" if delta < 0 else "raised"
            self._emit(t, "head_change", "warn",
                       f"Flow changed with no valve change. The twin's implied head moved "
                       f"{delta:+.0f} cm (from {s.head_baseline_cm:.0f} to {s.head_inst_cm:.0f} cm): "
                       f"the bag was probably {direction} about {abs(delta):.0f} cm, or the "
                       f"patient moved. Re-adjusting the clamp.",
                       {"delta_cm": delta, "from_cm": s.head_baseline_cm, "to_cm": s.head_inst_cm})
            s.head_baseline_cm = s.head_fast_cm = s.head_inst_cm

    def _check_stopped(self, t: float, o: Order) -> bool:
        expected_interval = 60.0 / o.target_gtt_min
        stall = max(4 * expected_interval, 8.0)
        age = self.rate.last_drop_age_s
        if t - self.t_started < stall + 4:
            return False
        if age < stall:
            if self._probe:
                self._emit(t, "probe_cleared", "info",
                           "Drops resumed during the flow check; no alarm.")
                self.mode, self._probe = Mode.RUNNING, None
            return False

        remaining = self.est.state.remaining_ml
        if remaining < max(50.0, 0.05 * o.bag_volume_ml):
            self.mode = Mode.ALARM_EMPTY
            self._move(1.0, t)
            self._emit(t, "empty_bag", "alarm",
                       f"Drops stopped with only ~{remaining:.0f} mL estimated left in the bag "
                       f"({self.est.state.infused_ml:.0f} mL infused). Bag is likely empty. "
                       f"Valve closed to keep the line from running dry.",
                       {"remaining_ml": remaining})
            return True

        if self._probe is None:
            # Active test: open the valve a bit. A low bag would respond; a kink won't.
            self._probe = {"u0": self.u, "t0": t}
            self.mode = Mode.PROBING
            self._move(max(0.0, self.u - 0.25), t)
            self._emit(t, "probe", "warn",
                       f"No drops for {age:.0f} s with ~{remaining:.0f} mL left. "
                       f"Opening the valve slightly to tell a kink from a low bag.")
            return True

        if t - self._probe["t0"] > max(2 * expected_interval, 6.0):
            u0 = self._probe["u0"]
            self._probe = None
            self.mode = Mode.ALARM_OCCLUSION
            # Restore the pre-probe position so clearing the kink doesn't cause a bolus.
            self._move(u0, t)
            self._emit(t, "occlusion", "alarm",
                       f"OCCLUSION: no flow even with the valve opened, and ~{remaining:.0f} mL "
                       f"is still in the bag, so this is a kink or clamp, not an empty bag. "
                       f"Check the line from bag to patient and the cannula site. Valve "
                       f"returned to its previous setting to prevent a bolus when it clears.",
                       {"remaining_ml": remaining})
        return True

    def _check_free_flow(self, t: float, o: Order) -> None:
        r = self.rate.gtt_min
        hard = self.cfg.safety.hard_max_gtt_min
        if (self.rate.confident and r > 1.6 * o.target_gtt_min and self.u < 0.05) or r > hard:
            self.mode = Mode.ALARM_FREE_FLOW
            self._move(1.0, t)
            self._emit(t, "free_flow", "alarm",
                       f"FREE FLOW: {r:.0f} drops/min vs target {o.target_gtt_min:.0f}. "
                       f"Valve closed. Check that the tubing is seated in the pinch valve.",
                       {"rate_gtt_min": r})

    def _narrate(self, t: float) -> None:
        """Monitor mode has no valve, so the useful output is telling people
        what the drops are doing: started, stopped, or rate changed."""
        r = self.rate
        if not self._flowing:
            if r.confident:
                self._flowing, self._ref_rate = True, r.gtt_min
                self._emit(t, "drops_detected", "info",
                           f"Drops detected: about {r.gtt_min:.0f} drops per minute.",
                           {"rate_gtt_min": r.gtt_min})
            return
        stall = max(10.0, 3 * 60.0 / self._ref_rate)
        if r.last_drop_age_s > stall:
            self._flowing = False
            self._emit(t, "drops_stopped", "warn",
                       f"No drops for {r.last_drop_age_s:.0f} seconds. The drip has stopped, "
                       f"or the camera has lost sight of the drop.",
                       {"last_drop_age_s": r.last_drop_age_s})
        elif (r.confident and abs(r.gtt_min - self._ref_rate) > 0.2 * self._ref_rate
              and not self._recent("rate_change", t, 10)):
            self._emit(t, "rate_change", "warn",
                       f"Drip rate changed from {self._ref_rate:.0f} to {r.gtt_min:.0f} "
                       f"drops per minute.", {"from": self._ref_rate, "to": r.gtt_min})
            self._ref_rate = r.gtt_min

    # ---- helpers ----------------------------------------------------------
    def _recent(self, kind: str, t: float, within_s: float) -> bool:
        return any(e.kind == kind and t - e.t < within_s for e in self.events[-20:])

    def _emit(self, t: float, kind: str, severity: str, message: str, data: dict | None = None):
        self.events.append(Event(t, kind, severity, message, data or {}))

    def _record(self, t: float, q: float) -> None:
        self._history.append({"t": round(t, 2), "gtt_min": round(self.rate.gtt_min, 2),
                              "u": round(self.u, 3),
                              "head_cm": round(self.est.state.head_fast_cm, 1)})
        del self._history[:-600]

    def snapshot(self) -> dict:
        o, s = self.order, self.est.state
        return {
            "t": self.t,
            "mode": self.mode.value,
            "monitor_only": self.monitor_only,
            "order": asdict(o) if o else None,
            "target_gtt_min": o.target_gtt_min if o else None,
            "rate_gtt_min": round(self.rate.gtt_min, 2),
            "rate_ml_h": round(self.rate.gtt_min * 60 / o.drop_factor, 1) if o else None,
            "rate_confident": self.rate.confident,
            "last_drop_age_s": None if math.isinf(self.rate.last_drop_age_s)
                               else round(self.rate.last_drop_age_s, 1),
            "valve_u": round(self.u, 3),
            "head_cm": round(s.head_fast_cm, 1),
            "head_baseline_cm": round(s.head_baseline_cm, 1),
            "infused_ml": round(s.infused_ml, 1),
            "remaining_ml": round(s.remaining_ml, 1),
            "drops_counted": self.drop_log.count,
        }
