from mm import LIPBot


def _bot(bot_factory):
    bot, _ = bot_factory()
    return bot


def test_compute_quotes_tick_and_order(bot_factory):
    bot = _bot(bot_factory)
    touch_bid, touch_ask = 0.40, 0.45
    bid, ask = bot.compute_quotes(touch_bid, touch_ask, inventory=0)
    assert 0.01 <= bid < ask <= 0.99
    # Anchored to touch with possible one-tick improvement
    assert bid >= 0.39 and ask <= 0.46


def test_compute_quotes_inventory_skew_positive_inventory(bot_factory):
    bot = _bot(bot_factory)
    touch_bid, touch_ask = 0.40, 0.44
    bid, ask = bot.compute_quotes(touch_bid, touch_ask, inventory=10, theta=0.005)
    # With positive inventory, current implementation leans away: bid down, ask up
    assert bid <= touch_bid
    assert ask >= touch_ask


def test_compute_quotes_inventory_skew_negative_inventory(bot_factory):
    bot = _bot(bot_factory)
    touch_bid, touch_ask = 0.40, 0.44
    bid, ask = bot.compute_quotes(touch_bid, touch_ask, inventory=-10, theta=0.005)
    # With negative inventory, lean opposite: bid up, ask down
    assert bid >= touch_bid
    assert ask <= touch_ask


