"""Hydraulic model of a gravity IV set and the online 'digital twin' estimator.

The trick that makes DripTwin explainable: given the valve position we
commanded and the drop rate we measure, the model back-solves the *implied
head* H. Each failure mode leaves a different fingerprint on H:

  bag lowered/raised   -> H steps by the height change, flow responds to valve
  bag draining         -> H creeps down a few cm per litre
  kink / occlusion     -> flow collapses; opening the valve does not bring it back
  empty bag            -> H falls toward P_ven as infused volume -> bag volume
"""

from dataclasses import dataclass
import math

from .config import PhysicsParams, CONFIG


def drop_flow_ml_s(f: float) -> float:
    """Measured on our drip set: flow Q (mL/s) at drop frequency f (drops/s).
    ~63 uL per drop when slow; drops grow when they come fast (~78 uL at 3/s),
    so there is no fixed drops-per-mL."""
    return 0.06341 * f + 0.02416 * f**5 / (f**4 + 2.688**4)


def drop_freq_for_flow(q_ml_s: float) -> float:
    """Inverse of drop_flow_ml_s (monotonic, so bisection)."""
    if q_ml_s <= 0:
        return 0.0
    lo, hi = 0.0, 50.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if drop_flow_ml_s(mid) < q_ml_s else (lo, mid)
    return (lo + hi) / 2


def ml_h_to_gtt_min(rate_ml_h: float) -> float:
    return drop_freq_for_flow(rate_ml_h / 3600.0) * 60.0


def gtt_min_to_ml_h(gtt_min: float) -> float:
    return drop_flow_ml_s(gtt_min / 60.0) * 3600.0


def clamp_resistance(u: float, p: PhysicsParams = CONFIG.physics) -> float:
    if u >= p.u_occlude:
        return math.inf
    gap = 1.0 - max(0.0, u)
    return p.kc * (1.0 / gap**3 - 1.0)


def flow_ml_min(head_cm: float, u: float, extra_r: float = 0.0,
                p: PhysicsParams = CONFIG.physics) -> float:
    """Forward model. extra_r models a kink or partial occlusion."""
    dp = head_cm * p.rho_g - p.p_venous_cmh2o
    if dp <= 0:
        return 0.0
    r = p.r0 + clamp_resistance(u, p) + extra_r
    return 0.0 if math.isinf(r) else dp / r


def implied_head(q_ml_min: float, u: float, p: PhysicsParams = CONFIG.physics) -> float:
    """Inverse model: what head would produce this flow at this valve position?"""
    rc = clamp_resistance(u, p)
    if math.isinf(rc):
        return math.nan
    return (q_ml_min * (p.r0 + rc) + p.p_venous_cmh2o) / p.rho_g


def valve_for_flow(head_cm: float, q_target_ml_min: float,
                   p: PhysicsParams = CONFIG.physics) -> float:
    """Model inverse: valve position that yields q_target at the given head.
    Returns 0.0 (wide open) if the target is unreachable."""
    dp = head_cm * p.rho_g - p.p_venous_cmh2o
    if q_target_ml_min <= 0:
        return 1.0
    if dp <= 0:
        return 0.0
    rc = dp / q_target_ml_min - p.r0
    if rc <= 0:
        return 0.0
    gap = (p.kc / (rc + p.kc)) ** (1.0 / 3.0)
    return min(p.u_occlude - 1e-3, max(0.0, 1.0 - gap))


def max_flow_ml_min(head_cm: float, p: PhysicsParams = CONFIG.physics) -> float:
    return flow_ml_min(head_cm, 0.0, p=p)


@dataclass
class TwinState:
    head_cm: float              # filtered implied head
    head_baseline_cm: float     # head at last 'settled' point
    head_fast_cm: float         # lightly filtered
    infused_ml: float
    bag_volume_ml: float
    head_inst_cm: float = 0.0   # latest unfiltered implied head (step detection)

    @property
    def remaining_ml(self) -> float:
        return max(0.0, self.bag_volume_ml - self.infused_ml)


class HeadEstimator:
    """Tracks implied head with a fast and slow EWMA. Only updates while the
    rate estimate is trustworthy and the valve is not occluding."""

    def __init__(self, initial_head_cm: float, bag_volume_ml: float,
                 p: PhysicsParams = CONFIG.physics):
        self.p = p
        self.state = TwinState(initial_head_cm, initial_head_cm, initial_head_cm,
                               0.0, bag_volume_ml, initial_head_cm)

    def update(self, q_ml_min: float, u: float, dt: float, confident: bool) -> TwinState:
        s = self.state
        s.infused_ml += q_ml_min * dt / 60.0
        if confident and q_ml_min > 0:
            h = implied_head(q_ml_min, u, self.p)
            if not math.isnan(h):
                s.head_inst_cm = h
                s.head_fast_cm += min(1.0, dt / 3.0) * (h - s.head_fast_cm)
                s.head_cm += min(1.0, dt / 12.0) * (h - s.head_cm)
        return s

    def rebaseline(self) -> None:
        self.state.head_baseline_cm = self.state.head_fast_cm
        self.state.head_cm = self.state.head_fast_cm
