<!-- 4189af7d-d3cc-45b1-b0ae-b6093a9cc4ff f6f567a7-2d5a-48f3-99ec-216a70654460 -->
# Websocket Orderbook Management and Background Discovery

## Overview

Restructure the market maker to:

1. Run market discovery in a separate background thread (non-blocking)
2. Use websocket orderbook updates for all tracked markets
3. Immediately adjust sell orders based on orderbook changes when inventory > 0
4. Add configurable limit on total markets with active orders

## Key Changes

### 1. Create WebSocketOrderbookTracker Class (new)

Similar to existing `WebSocketFillTracker` (lines 499-789), create a new class to:

- Connect to Kalshi websocket orderbook API: `wss://api.elections.kalshi.com/trade-api/ws/v2`
- Subscribe to multiple market tickers using the orderbook channel (per docs)
- Receive `orderbook_snapshot` and `orderbook_delta` messages
- Maintain local orderbook state for each tracked market
- Call a callback function when orderbook changes (with new best bid/ask)

### 2. Implement Reactive Sell Order Management

When websocket orderbook update received for market with inventory:

- Extract new `touch_ask` (best bid in the orderbook = where we sell)
- Calculate desired ask price using existing logic from `compute_quotes()` (lines 3666-3673):
  - `spread > 0.07`: ask = `touch_ask - 0.02`
  - `spread > 0.03`: ask = `touch_ask - 0.01`
  - else: ask = `touch_ask`
- Compare with current sell order price
- If different: cancel existing sell order and place new one at correct price
- Use rate limiting to avoid excessive order replacements (e.g., max 1 update per 500ms per market)

### 4. Main Loop Sell Order Delegation

Modify `manage_orders()` method (line 4141):

- Check if market is being managed by websocket orderbook tracker
- If yes: skip sell order management entirely (websocket handles it)
- If no: fall back to existing sell order logic
- Main loop still handles buy orders as before

### 5. Background Market Discovery Thread

Create `_run_market_discovery()` method:

- Runs in separate daemon thread (similar to websocket threads)
- Calls `api.get_valid_markets()` on configurable interval (new config param: `discovery_interval_seconds`)
- Updates shared thread-safe data structure (use `threading.Lock`)
- Filters out markets already tracked or with historical toxicity
- Respects max markets limit

### 4. Main Loop Modifications (`run()` method, line 3202)

**Remove** from main loop (lines 3327-3434):

- Market discovery block that calls `get_valid_markets()`
- Adding new markets logic

**Keep** in main loop:

- Fetching open orders and positions
- Processing tracked markets with `_process_single_market()`
- But check against `max_markets_with_orders` before processing

**Add**:

- Start websocket orderbook tracker (lines 3209-3218, similar to fill tracker)
- Start discovery thread
- Pull from discovery queue to add new markets (non-blocking)

### 5. Configuration Updates (`config.yaml`)

Add new parameters:

```yaml
market_maker:
  max_markets_with_orders: 20  # Limit concurrent markets
  discovery_interval_seconds: 10  # How often to discover new markets
  orderbook_update_cooldown_ms: 500  # Min time between order updates per market
```

### 6. Key Files to Modify

- `mm.py`:
  - Add `WebSocketOrderbookTracker` class (~300 lines)
  - Add `_run_market_discovery()` method (~100 lines)
  - Modify `LIPBot.__init__()` to accept new config params
  - Modify `LIPBot.run()` to start threads and remove inline discovery
  - Add `_handle_orderbook_update()` callback method
  - Add thread-safe discovery queue with lock
- `runner.py`: Pass new config params to `LIPBot` (lines 93-109)
- `config.yaml`: Add new configuration parameters

## Implementation Order

1. Create `WebSocketOrderbookTracker` class with subscription logic
2. Add orderbook update callback to adjust sell orders
3. Extract market discovery into `_run_market_discovery()` method
4. Modify main `run()` loop to use discovery queue
5. Add max markets limit enforcement
6. Update configuration files
7. Test with limited markets first

### To-dos

- [ ] Create WebSocketOrderbookTracker class with Kalshi WS orderbook API
- [ ] Add callback to adjust sell orders on orderbook updates
- [ ] Extract discovery into separate background thread method
- [ ] Update main run() loop to remove inline discovery
- [ ] Enforce max_markets_with_orders limit
- [ ] Add new config params to runner.py and config.yaml