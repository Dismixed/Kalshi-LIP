"""
Tests for automatic cashout of resolved markets (1c or 99c).
"""
from mm import to_tick


def test_cashout_yes_position_at_99c(bot_factory):
    """Test that a YES position is automatically sold when market hits 99c"""
    bot, api = bot_factory(balance=100)
    ticker = "RESOLVED-YES"
    side = "yes"
    
    # Market at 99c (bid=0.99, ask=0.99)
    mkt_bid = 0.99
    mkt_ask = 0.99
    inventory = 50  # We have 50 YES shares
    
    # Orderbook showing YES at 99c
    yes_asks = [(0.99, 100)]  # YES asks (from var_true)
    yes_bids = [(0.99, 100)]  # YES bids (from var_false)
    
    # Track if place_order was called
    order_placed = []
    def mock_place_order(ticker, action, side, price, quantity, expiration_ts):
        order_placed.append({
            'ticker': ticker,
            'action': action,
            'side': side,
            'price': price,
            'quantity': quantity
        })
        return "mock-order-id-123"
    
    # Mock cancel_order
    orders_canceled = []
    def mock_cancel_order(order_id):
        orders_canceled.append(order_id)
        return True
    
    api.place_order = mock_place_order
    api.cancel_order = mock_cancel_order
    
    # Call the cashout check
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    # Verify cashout was triggered
    assert result is True, "Should return True when cashout is triggered"
    assert len(order_placed) == 1, "Should place one cashout order"
    
    # Verify order details
    order = order_placed[0]
    assert order['ticker'] == ticker
    assert order['action'] == 'sell'  # Selling YES position
    assert order['side'] == side
    assert order['price'] == mkt_bid  # Selling at market bid (0.99)
    assert order['quantity'] == 50  # Selling all inventory


def test_cashout_yes_position_at_1c(bot_factory):
    """Test that a YES position is sold when market hits 1c (loss scenario)"""
    bot, api = bot_factory(balance=100)
    ticker = "RESOLVED-NO"
    side = "yes"
    
    # Market at 1c (bid=0.01, ask=0.01)
    mkt_bid = 0.01
    mkt_ask = 0.01
    inventory = 30  # We have 30 YES shares (this is a losing position)
    
    # Orderbook showing NO at 99c (YES at 1c)
    yes_asks = [(0.01, 100)]  # YES asks (from var_true)
    yes_bids = [(0.01, 100)]  # YES bids (from var_false)
    
    order_placed = []
    def mock_place_order(ticker, action, side, price, quantity, expiration_ts):
        order_placed.append({
            'ticker': ticker,
            'action': action,
            'side': side,
            'price': price,
            'quantity': quantity
        })
        return "mock-order-id-456"
    
    orders_canceled = []
    def mock_cancel_order(order_id):
        orders_canceled.append(order_id)
        return True
    
    api.place_order = mock_place_order
    api.cancel_order = mock_cancel_order
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    assert result is True
    assert len(order_placed) == 1
    
    order = order_placed[0]
    assert order['action'] == 'sell'
    assert order['price'] == mkt_bid  # Selling at 1c
    assert order['quantity'] == 30


def test_no_cashout_when_no_inventory(bot_factory):
    """Test that no cashout happens when we have no inventory"""
    bot, api = bot_factory(balance=100)
    ticker = "RESOLVED-YES"
    side = "yes"
    
    mkt_bid = 0.99
    mkt_ask = 0.99
    inventory = 0  # No position
    
    # Orderbook showing YES at 99c
    yes_asks = [(0.99, 100)]
    yes_bids = [(0.99, 100)]
    
    order_placed = []
    api.place_order = lambda *args, **kwargs: order_placed.append(args) or "mock-id"
    api.cancel_order = lambda order_id: True
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    # Should not trigger cashout when no inventory
    assert result is False
    assert len(order_placed) == 0


def test_no_cashout_at_normal_prices(bot_factory):
    """Test that no cashout happens at normal market prices"""
    bot, api = bot_factory(balance=100)
    ticker = "NORMAL-MKT"
    side = "yes"
    
    # Normal market prices
    mkt_bid = 0.45
    mkt_ask = 0.55
    inventory = 25
    
    # Orderbook showing normal prices
    yes_asks = [(0.55, 100)]
    yes_bids = [(0.45, 100)]
    
    order_placed = []
    api.place_order = lambda *args, **kwargs: order_placed.append(args) or "mock-id"
    api.cancel_order = lambda order_id: True
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    # Should not trigger cashout at normal prices
    assert result is False
    assert len(order_placed) == 0


def test_cashout_cancels_existing_orders(bot_factory):
    """Test that existing orders are canceled before placing cashout order"""
    bot, api = bot_factory(balance=100)
    ticker = "RESOLVED-MKT"
    side = "yes"
    
    # Set up existing orders
    api.set_orders(ticker, [
        {"order_id": "order-1", "side": "yes", "action": "buy", "yes_price": 50, "remaining_count": 10},
        {"order_id": "order-2", "side": "yes", "action": "sell", "yes_price": 60, "remaining_count": 20},
    ])
    
    mkt_bid = 0.99
    mkt_ask = 0.99
    inventory = 40
    
    # Orderbook showing YES at 99c
    yes_asks = [(0.99, 100)]
    yes_bids = [(0.99, 100)]
    
    orders_canceled = []
    def mock_cancel_order(order_id):
        orders_canceled.append(order_id)
        return True
    
    order_placed = []
    def mock_place_order(ticker, action, side, price, quantity, expiration_ts):
        order_placed.append({'action': action})
        return "cashout-order-id"
    
    api.cancel_order = mock_cancel_order
    api.place_order = mock_place_order
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    assert result is True
    # Should cancel both existing orders
    assert len(orders_canceled) == 2
    assert "order-1" in orders_canceled
    assert "order-2" in orders_canceled
    # Then place the cashout order
    assert len(order_placed) == 1
    assert order_placed[0]['action'] == 'sell'


def test_cashout_negative_inventory_at_1c(bot_factory):
    """Test cashout for NO position (negative inventory) when market hits 1c"""
    bot, api = bot_factory(balance=100)
    ticker = "RESOLVED-NO"
    side = "yes"
    
    # Market at 1c - NO position wins
    mkt_bid = 0.01
    mkt_ask = 0.01
    inventory = -40  # We have a NO position (represented as negative)
    
    # Orderbook showing NO at 99c (YES at 1c)
    yes_asks = [(0.01, 100)]
    yes_bids = [(0.01, 100)]
    
    order_placed = []
    def mock_place_order(ticker, action, side, price, quantity, expiration_ts):
        order_placed.append({
            'action': action,
            'quantity': quantity,
            'price': price
        })
        return "mock-order-id"
    
    api.place_order = mock_place_order
    api.cancel_order = lambda order_id: True
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    assert result is True
    assert len(order_placed) == 1
    
    order = order_placed[0]
    assert order['action'] == 'buy'  # Buy back the NO position
    assert order['quantity'] == 40  # Close out all 40 contracts
    assert order['price'] == mkt_ask  # Buy at ask (~1c)


def test_cashout_at_edge_threshold_97(bot_factory):
    """Test that market at 97c (EDGE_HIGH threshold) is detected as resolved"""
    bot, api = bot_factory(balance=100)
    ticker = "EDGE-HIGH"
    side = "yes"
    
    # Market at 97c (exactly at EDGE_HIGH threshold)
    mkt_bid = 0.97
    mkt_ask = 0.98
    inventory = 25
    
    # Orderbook showing YES at 97c
    yes_asks = [(0.97, 100)]
    yes_bids = [(0.97, 100)]
    
    order_placed = []
    api.place_order = lambda *args, **kwargs: order_placed.append(args) or "mock-id"
    api.cancel_order = lambda order_id: True
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    # Should trigger cashout at 97c threshold
    assert result is True
    assert len(order_placed) == 1


def test_cashout_at_edge_threshold_03(bot_factory):
    """Test that market at 3c (EDGE_LOW threshold) is detected as resolved"""
    bot, api = bot_factory(balance=100)
    ticker = "EDGE-LOW"
    side = "yes"
    
    # Market at 3c (exactly at EDGE_LOW threshold)
    mkt_bid = 0.02
    mkt_ask = 0.03
    inventory = 30
    
    # Orderbook showing YES at 3c
    yes_asks = [(0.03, 100)]
    yes_bids = [(0.03, 100)]
    
    order_placed = []
    api.place_order = lambda *args, **kwargs: order_placed.append(args) or "mock-id"
    api.cancel_order = lambda order_id: True
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    # Should trigger cashout at 3c threshold (resolved to NO)
    assert result is True
    assert len(order_placed) == 1


def test_no_cashout_just_below_edge_threshold(bot_factory):
    """Test that market just below 97c threshold is NOT detected as resolved"""
    bot, api = bot_factory(balance=100)
    ticker = "BELOW-EDGE"
    side = "yes"
    
    # Market at 96c (below 97c threshold)
    mkt_bid = 0.96
    mkt_ask = 0.962
    inventory = 20
    
    # Orderbook showing YES at 96c
    yes_asks = [(0.96, 100)]
    yes_bids = [(0.96, 100)]
    
    order_placed = []
    api.place_order = lambda *args, **kwargs: order_placed.append(args) or "mock-id"
    api.cancel_order = lambda order_id: True
    
    result = bot.check_and_cashout_resolved_market(ticker, side, yes_asks, yes_bids, mkt_bid, mkt_ask, inventory)
    
    # Should NOT trigger cashout at 96c (below 97c threshold)
    assert result is False
    assert len(order_placed) == 0

