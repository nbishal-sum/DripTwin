"""A physical simulator of a gravity IV set, so the whole stack (controller,
agent, dashboard) runs with no hardware — and as a demo backup.

It also renders synthetic drip-chamber frames so the OpenCV detector can be
tested end to end without a webcam.
"""

from __future__ import annotations

import random

import numpy as np

from .config import CONFIG, Config
from .physics import flow_ml_min


class SimPlant:
    def __init__(self, drop_factor: int = 20, head_cm: float = 80.0,
                 bag_volume_ml: float = 1000.0, cfg: Config = CONFIG, seed: int = 1):
        self.cfg = cfg
        self.drop_factor = drop_factor
        self.mount_head_cm = head_cm      # bag hook height above the site
        self.bag_ml = bag_volume_ml
        self.bag_volume0 = bag_volume_ml
        self.extra_r = 0.0                # kink / occlusion
        self.u = 1.0                      # actual valve position (after slew)
        self.u_cmd = 1.0
        self._accum = 0.0
        self._rng = random.Random(seed)
        self.creep_per_min = 0.0          # clamp relaxation, valve units / minute

    # ---- disturbances (wired to dashboard buttons) ----
    def lower_bag(self, cm: float) -> None:
        self.mount_head_cm -= cm

    def kink(self, on: bool = True) -> None:
        self.extra_r = 1e9 if on else 0.0

    @property
    def head_cm(self) -> float:
        # Fluid surface drops as the bag drains.
        drained = (self.bag_volume0 - self.bag_ml) / 1000.0
        if self.bag_ml <= 0:
            return 0.0
        return self.mount_head_cm - drained * self.cfg.physics.bag_height_cm_per_l

    def command(self, u: float) -> None:
        self.u_cmd = u

    def step(self, t: float, dt: float) -> list[float]:
        """Advance physics by dt; returns drop timestamps that occurred."""
        max_du = self.cfg.safety.max_slew_per_s * dt
        self.u += max(-max_du, min(max_du, self.u_cmd - self.u))
        self.u = min(1.0, max(0.0, self.u + self.creep_per_min * dt / 60.0))
        q = flow_ml_min(self.head_cm, self.u, self.extra_r, self.cfg.physics)
        dv = min(self.bag_ml, q * dt / 60.0)
        self.bag_ml -= dv
        self._accum += dv
        drops = []
        drop_vol = 1.0 / self.drop_factor
        while self._accum >= drop_vol:
            self._accum -= drop_vol
            # drop detaches at a slightly random point in the step
            drops.append(t - dt * self._rng.random() * 0.5)
        return sorted(drops)


class SyntheticChamber:
    """Renders a drip chamber: a pendant drop grows at the orifice, detaches,
    falls, and lands in the pool. Grayscale 320x240."""

    W, H = 240, 320

    def __init__(self, fps: float = 30.0, noise: float = 6.0):
        self.fps = fps
        self.noise = noise
        self._fall: list[float] = []      # y positions of falling drops
        self._rng = np.random.default_rng(0)

    def frame(self, growth: float, detached: bool) -> np.ndarray:
        """growth in [0,1] = pendant drop size; detached = a drop fell this frame."""
        import cv2
        img = np.full((self.H, self.W), 35, np.uint8)
        cv2.rectangle(img, (60, 10), (180, 310), 70, 2)            # chamber walls
        cv2.rectangle(img, (110, 10), (130, 40), 120, -1)          # orifice
        cv2.rectangle(img, (62, 250), (178, 308), 90, -1)          # fluid pool
        if detached:
            self._fall.append(50.0)
        r = int(3 + 9 * growth)
        cv2.circle(img, (120, 40 + r), r, 210, -1)                 # pendant drop
        v = 900.0 / self.fps                                       # px per frame
        nxt = []
        for y in self._fall:
            y += v
            if y < 250:
                cv2.ellipse(img, (120, int(y)), (6, 9), 0, 0, 360, 200, -1)
                nxt.append(y)
        self._fall = nxt
        img =img.astype(np.int16) + self._rng.normal(0, self.noise, img.shape).astype(np.int16)
        return np.clip(img, 0, 255).astype(np.uint8)
