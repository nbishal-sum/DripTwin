"""Control with a measured valve table (driptwin.valvecal) instead of the generic model."""
import json
from dataclasses import replace

import pytest

from scenario import run
from driptwin.config import CONFIG
from driptwin.physics import flow_ml_min, load_valve, ml_h_to_gtt_min, valve_for_flow
from driptwin.sim import SimPlant
from driptwin.twin import DripTwin, Order

# a pinch valve as it measures: flat far from the tube, then steep near shut
ANGLES = [0, 10, 20, 30, 40, 50, 60, 65, 70, 75, 80, 90]
ML_H = [400, 398, 396, 390, 380, 360, 320, 260, 170, 70, 0, 0]


def cal_file(tmp_path, angles=ANGLES, ml_h=ML_H, **extra):
    f = tmp_path / "valve_cal.json"
    f.write_text(json.dumps({"head_cm": 80, "rows": [{"angle": a, "ml_h": q}
                                                     for a, q in zip(angles, ml_h)], **extra}))
    return f


def test_table_is_read_and_inverted(tmp_path):
    p = load_valve(cal_file(tmp_path))
    v = p.valve
    assert (v.open_deg, v.closed_deg) == (0, 80)
    assert flow_ml_min(80, 0.0, p=p) == pytest.approx(400 / 60)
    assert flow_ml_min(80, 1.0, p=p) == 0.0
    u70 = (70 - 0) / 80
    assert flow_ml_min(80, u70, p=p) == pytest.approx(170 / 60)
    assert valve_for_flow(80, 170 / 60, p) == pytest.approx(u70)
    for q in (1.0, 3.0, 4.5):                      # round trip at another head too
        assert flow_ml_min(60, valve_for_flow(60, q, p), p=p) == pytest.approx(q, rel=1e-6)


def test_noisy_table_is_made_monotonic(tmp_path):
    p = load_valve(cal_file(tmp_path, ml_h=[400, 405, 396, 390, 395, 360, 320, 260, 170, 70, 0, 0]))
    assert list(p.valve.q_ml_min) == sorted(p.valve.q_ml_min, reverse=True)


def test_reversed_valve_direction(tmp_path):
    p = load_valve(cal_file(tmp_path, ml_h=list(reversed(ML_H))))
    assert (p.valve.open_deg, p.valve.closed_deg) == (90, 10)


@pytest.mark.parametrize("shift", [0.0, 0.04])     # 0.04 = real valve 3 deg off the table
def test_reaches_target_band_with_measured_valve(tmp_path, shift):
    p = load_valve(cal_file(tmp_path))
    cfg = replace(CONFIG, physics=p)
    real = replace(p, valve=replace(p.valve, u=tuple(min(1.0, u + shift) if 0 < u < 1 else u
                                                     for u in p.valve.u)))
    plant = SimPlant(cfg=replace(CONFIG, physics=real))
    twin, _, log = run(120, plant=plant, twin=DripTwin(cfg, head_cm=80.0))
    target = ml_h_to_gtt_min(120, 20)
    late = [r[1] for r in log if r[0] > 70]
    assert sum(late) / len(late) == pytest.approx(target, rel=cfg.band_out)
    assert twin.mode.value == "running"


def test_sticky_tube_is_approached_from_open(tmp_path):
    """The rig's tube reopens only after extra travel. Opening moves overshoot by
    backlash_deg and close back, so the valve lands where the table says even
    when the bag is lowered (open) and raised again (close)."""
    p = load_valve(cal_file(tmp_path, backlash_deg=6))
    cfg = replace(CONFIG, physics=p)
    plant = SimPlant(cfg=cfg)
    plant.stick_u = 6 / 80
    twin, _, log = run(400, [(120, lambda pl: pl.lower_bag(20)), (260, lambda pl: pl.lower_bag(-20))],
                       plant=plant, twin=DripTwin(cfg, head_cm=80.0))
    target = ml_h_to_gtt_min(120, 20)
    for a, b in ((60, 120), (200, 260), (340, 400)):
        rows = [r[1] for r in log if a < r[0] < b]
        assert sum(rows) / len(rows) == pytest.approx(target, rel=cfg.band_out)
    assert "probe" not in [e.kind for e in twin.events]
