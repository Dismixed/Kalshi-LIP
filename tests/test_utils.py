from mm import to_tick, to_cents


def test_to_tick_rounds_and_clamps():
    assert to_tick(0.004) == 0.01
    assert to_tick(0.995) == 0.99
    assert to_tick(0.234) == 0.23
    assert to_tick(0.235) == 0.24


def test_to_cents_uses_to_tick():
    assert to_cents(0.234) == 23
    assert to_cents(0.235) == 24
    assert to_cents(0.999) == 99
    assert to_cents(0.0001) == 1


