"""Run the real OpenCV detector on rendered drip-chamber frames with known drops."""
import numpy as np
import pytest

from driptwin.sim import SyntheticChamber
from driptwin.vision import DropDetector, ROI, ROIConfig

ROIS = ROIConfig(ROI(95, 30, 50, 50))


WARMUP_S = 3.5   # detector learns the scene for 90 frames before counting


def render_and_count(period_s, seconds=60, fps=30, noise=6.0):
    ch = SyntheticChamber(fps=fps, noise=noise)
    det = DropDetector(ROIS)
    frames_per_drop = round(period_s * fps)
    true = counted = 0
    for n in range(int(seconds * fps)):
        phase = (n % frames_per_drop) / frames_per_drop
        detached = n > 0 and n % frames_per_drop == 0
        fired = det.process(ch.frame(phase, detached), n / fps)
        if n / fps >= WARMUP_S:
            true += detached
            counted += fired
    return true, counted


@pytest.mark.parametrize("period", [0.75, 1.5, 3.0])     # 80, 40, 20 drops/min
def test_counts_match(period):
    true, counted = render_and_count(period)
    assert abs(counted - true) <= 1


def test_noisy_scene():
    true, counted = render_and_count(1.5, noise=14.0)
    assert abs(counted - true) <= 2


def test_no_false_drops_when_static():
    ch, det = SyntheticChamber(), DropDetector(ROIS)
    assert sum(det.process(ch.frame(0.5, False), n / 30) for n in range(600)) == 0
