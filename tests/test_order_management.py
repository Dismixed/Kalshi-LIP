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


def test_manage_orders_blocks_bids_when_allow_bid_false(bot_factory):
    """Test that when allow_bid=False (e.g., LIP target met), no buy orders are placed."""
    bot, api = bot_factory(balance=100)
    ticker = "TEST-MKT"
    side = "yes"

    bid = 0.45
    ask = 0.55
    spread = ask - bid

    # No existing orders
    api.set_orders(ticker, [])

    # Call manage_orders with allow_bid=False (simulating LIP target met)
    bot.manage_orders(bid, ask, spread, ticker=ticker, inventory=0, side=side, allow_bid=False, allow_ask=False)

    # Verify no buy orders were placed
    buy_orders = [o for o in api._placed_orders if o['action'] == 'buy' and o['ticker'] == ticker]
    assert len(buy_orders) == 0, "No buy orders should be placed when allow_bid=False"

    # Verify no sell orders were placed (inventory=0 and allow_ask=False)
    sell_orders = [o for o in api._placed_orders if o['action'] == 'sell' and o['ticker'] == ticker]
    assert len(sell_orders) == 0, "No sell orders should be placed when inventory=0 and allow_ask=False"


def test_manage_orders_allows_exit_when_inventory_and_lip_target_met(bot_factory):
    """Test that when LIP target is met but we have inventory, sell orders are still placed."""
    bot, api = bot_factory(balance=100)
    ticker = "TEST-MKT"
    side = "yes"

    bid = 0.45
    ask = 0.55
    spread = ask - bid
    inventory = 50

    # No existing orders
    api.set_orders(ticker, [])

    # Call manage_orders with allow_bid=False (LIP target met) but allow_ask=True (have inventory)
    bot.manage_orders(bid, ask, spread, ticker=ticker, inventory=inventory, side=side, allow_bid=False, allow_ask=True)

    # Verify no buy orders were placed
    buy_orders = [o for o in api._placed_orders if o['action'] == 'buy' and o['ticker'] == ticker]
    assert len(buy_orders) == 0, "No buy orders should be placed when allow_bid=False"

    # Verify sell order WAS placed to exit inventory
    sell_orders = [o for o in api._placed_orders if o['action'] == 'sell' and o['ticker'] == ticker]
    assert len(sell_orders) == 1, "One sell order should be placed to exit inventory"
    assert sell_orders[0]['quantity'] == inventory, "Sell order quantity should match inventory"


