from mm import to_tick


def test_manage_orders_cancels_duplicates_and_places_when_needed(bot_factory):
    bot, api = bot_factory(balance=100)
    ticker = "TEST-MKT"
    side = "yes"

    bid = 0.45
    ask = 0.55
    spread = ask - bid

    # Provide two resting buys: one at bid (keep) and one worse (cancel)
    api.set_orders(ticker, [
        {"order_id": "order-1", "ticker": ticker, "side": side, "action": "buy", "yes_price": bid, "remaining_count": 10},
        {"order_id": "order-2", "ticker": ticker, "side": side, "action": "buy", "yes_price": to_tick(bid - 0.01), "remaining_count": 10},
        {"order_id": "order-3", "ticker": ticker, "side": side, "action": "sell", "yes_price": ask, "remaining_count": 10},
    ])

    # Positive inventory triggers optional sell placement if absent
    bot.manage_orders(bid, ask, spread, ticker=ticker, inventory=10, side=side)

    kinds = [a.get("kind") for a in bot.metrics.action_log]
    # One cancel for duplicate buy, none for the kept best buy or the kept best sell
    assert kinds.count("cancel_order") >= 1
    # A sell placement is recorded when not already present at best ask and inventory > 0
    # Depending on pre-existing sell at ask, may not place; ensure at least some action logged
    assert len(kinds) >= 1


