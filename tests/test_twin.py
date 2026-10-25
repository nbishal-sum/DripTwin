import pytest

from scenario import run
from driptwin.physics import (flow_ml_min, implied_head, valve_for_flow,
                              drop_flow_ml_s, gtt_min_to_ml_h, ml_h_to_gtt_min)
from driptwin.sim import SimPlant
from driptwin.twin import DripTwin, Order, OrderRejected, validate_order


def kinds(twin):
    return [e.kind for e in twin.events]


def settled_rate(log, after):
    rows = [r for r in log if r[0] > after]
    return sum(r[1] for r in rows) / len(rows)


TARGET = ml_h_to_gtt_min(120)     # the default scenario order, ~31.5 drops/min


def test_order_conversion():
    # slow drops are ~63 uL; fast ones grow, so 3 drops/s gives more than 3x 63 uL/s
    assert drop_flow_ml_s(0.5) / 0.5 == pytest.approx(0.0634, rel=0.01)
    assert drop_flow_ml_s(3.0) == pytest.approx(0.2343, rel=0.01)
    assert ml_h_to_gtt_min(100) == pytest.approx(26.3, rel=0.01)
    for rate in (5, 100, 500):
        assert gtt_min_to_ml_h(ml_h_to_gtt_min(rate)) == pytest.approx(rate, rel=1e-6)


def test_model_inverse_roundtrip():
    for head in (50, 80, 120):
        for q in (0.5, 2.0, 5.0):
            u = valve_for_flow(head, q)
            assert flow_ml_min(head, u) == pytest.approx(q, rel=1e-3)
            assert implied_head(q, u) == pytest.approx(head, rel=1e-3)


@pytest.mark.parametrize("bad", [Order(1000, 500, 1000), Order(120, 2000, 1000),
                                 Order(120, 0, 1000)])
def test_unsafe_orders_rejected(bad):
    with pytest.raises(OrderRejected):
        validate_order(bad)


def test_reaches_target():
    twin, _, log = run(60)
    assert settled_rate(log, 20) == pytest.approx(TARGET, rel=0.04)
    assert kinds(twin) == ["order_started"]


def test_bag_lowered_is_diagnosed_and_corrected():
    twin, _, log = run(180, [(60, lambda p: p.lower_bag(25))])
    ev = [e for e in twin.events if e.kind == "head_change"]
    assert len(ev) == 1
    assert ev[0].data["delta_cm"] == pytest.approx(-25, abs=3)
    assert settled_rate(log, 120) == pytest.approx(TARGET, rel=0.05)


def test_kink_raises_occlusion_and_restores_valve():
    twin, plant, _ = run(120, [(60, lambda p: p.kink(True))])
    assert "probe" in kinds(twin) and "occlusion" in kinds(twin)
    assert "empty_bag" not in kinds(twin)
    probe = next(e for e in twin.events if e.kind == "probe")
    # valve went back to its pre-probe position: no bolus when the kink clears
    assert twin.u > 0.5 and abs(twin.u - plant.u_cmd) < 1e-9
    assert probe.t > 60


def test_brief_pinch_clears_without_alarm():
    twin, _, _ = run(150, [(60, lambda p: p.kink(True)), (70, lambda p: p.kink(False))])
    assert "occlusion" not in kinds(twin)


def test_empty_bag_is_not_called_a_kink():
    plant = SimPlant(bag_volume_ml=80)
    twin, _, _ = run(3600, order=Order(120, 100, 100), plant=plant)
    assert "empty_bag" in kinds(twin)
    assert "occlusion" not in kinds(twin)
    assert twin.u == 1.0


def test_bag_too_low_asks_to_raise_it():
    twin, _, _ = run(120, [(40, lambda p: p.lower_bag(60))], order=Order(400, 1000, 1000))
    assert "low_head" in kinds(twin)


def test_clamp_creep_is_compensated():
    plant = SimPlant()
    plant.creep_per_min = -0.01   # tubing relaxes open -> flow would rise
    _, _, log = run(600, plant=plant)
    assert settled_rate(log, 300) == pytest.approx(TARGET, rel=0.06)


def test_vtbi_completion_closes_valve():
    twin, _, _ = run(1000, order=Order(300, 50, 1000))
    assert twin.mode.value == "complete" and twin.u == 1.0


def test_monitor_mode_narrates_drops():
    twin = DripTwin(monitor_only=True)
    with pytest.raises(OrderRejected):
        twin.start(Order(120, 1000, 1000), 0.0)
    t, period = 0.0, 1.5                       # 40 drops/min
    while t < 60:
        t = round(t + 0.1, 6)
        if t >= 40:                           # flow speeds up to 60 drops/min
            period = 1.0
        if abs(t / period - round(t / period)) < 1e-6 and t < 90:
            twin.on_drop(t)
        twin.tick(t)
    for _ in range(150):                      # then drops stop for 15 s
        t = round(t + 0.1, 6)
        twin.tick(t)
    assert kinds(twin) == ["drops_detected", "rate_change", "drops_stopped"]
    change = twin.events[1].data
    assert change["from"] == pytest.approx(40, rel=0.05) and change["to"] == pytest.approx(60, rel=0.05)
