import pytest

from driptwin.server import ANNOUNCE, split_wake


@pytest.mark.parametrize("heard, question", [
    ("Assistant, how many drops so far?", "how many drops so far?"),
    ("Hey assistant. What's the rate?", "What's the rate?"),
    ("Asistente, ¿cuántas gotas van?", "¿cuántas gotas van?"),
    ("Oye asistente, pausa la infusión.", "pausa la infusión."),
    ("Asistente.", ""),
])
def test_wake_word_english_and_spanish(heard, question):
    assert split_wake(heard) == (True, question)


def test_no_wake_word_is_ignored():
    assert split_wake("how many drops so far?")[0] is False


def test_every_alarm_has_both_languages():
    for kind, (en, es) in ANNOUNCE.items():
        assert en and es and en != es, kind


def test_only_alarms_are_spoken_unprompted():
    # every scenario's events: only alarm-severity ones may have a spoken phrase, and all of them must
    from scenario import run
    from driptwin.sim import SimPlant
    from driptwin.twin import Order
    events = []
    for kw in ({"disturbances": [(60, lambda p: p.kink(True))], "seconds": 120},
               {"disturbances": [(60, lambda p: p.lower_bag(25))], "seconds": 180},
               {"order": Order(120, 100, 100), "plant": SimPlant(bag_volume_ml=80), "seconds": 3600},
               {"order": Order(300, 50, 1000), "seconds": 1000}):
        twin, _, _ = run(**kw)
        events += twin.events
    assert {e.kind for e in events if e.severity == "alarm"} >= {"occlusion", "empty_bag"}
    for e in events:
        assert (e.kind in ANNOUNCE) == (e.severity == "alarm"), e.kind
    assert set(ANNOUNCE) == {"occlusion", "free_flow", "empty_bag", "fw_host_lost"}
