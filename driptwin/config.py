"""Central configuration. Everything safety-relevant lives in SAFETY and is
mirrored in firmware/main.py — firmware is the authority, the host copy only
lets us refuse bad commands early and explain why."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SafetyLimits:
    # Order limits (what the agent / UI may request)
    min_rate_ml_h: float = 5.0
    max_rate_ml_h: float = 500.0
    max_volume_ml: float = 3000.0

    # Valve: u = 0 fully open, 1 fully closed (pinched)
    u_min: float = 0.0
    u_max: float = 1.0
    max_slew_per_s: float = 0.15       # max valve travel per second
    # Free-flow guard: the twin closes the valve above this camera-counted drop rate
    hard_max_gtt_min: float = 180.0
    # Firmware watchdog: no heartbeat for this long -> hold + alarm
    heartbeat_timeout_s: float = 3.0


@dataclass(frozen=True)
class PhysicsParams:
    """Lumped hydraulic model of a gravity set.

    Q [mL/min] = (H - P_ven) / (R0 + Rc(u))
    H      head: bag fluid surface above the cannula [cmH2O]
    P_ven  venous back-pressure [cmH2O]
    R0     tubing + cannula resistance with the clamp wide open [cmH2O/(mL/min)]
    Rc(u)  pinch-valve resistance; slot flow ~ 1/gap^3, gap = 1 - u
    """
    rho_g: float = 1.0                   # cmH2O per cm of water column
    p_venous_cmh2o: float = 8.0
    r0: float = 5.0
    kc: float = 1.0
    u_occlude: float = 0.97              # beyond this the tube is sealed
    bag_height_cm_per_l: float = 15.0    # fluid surface drop as a 1 L bag drains


@dataclass(frozen=True)
class Config:
    safety: SafetyLimits = field(default_factory=SafetyLimits)
    physics: PhysicsParams = field(default_factory=PhysicsParams)
    control_period_s: float = 0.5
    rate_window_drops: int = 8           # inter-drop intervals in the rate estimate
    default_head_cm: float = 80.0


CONFIG = Config()
