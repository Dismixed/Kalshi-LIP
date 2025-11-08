# Automatic Cashout for Resolved Markets (1c/99c)

## Overview
This feature automatically detects and cashes out positions when markets reach extreme prices (1c or 99c), which indicates they are effectively resolved.

## Implementation

### New Method: `check_and_cashout_resolved_market()`
Location: `mm.py`, in the `LIPBot` class

This method:
1. **Detects resolved markets** by checking if the market bid/ask is at extreme prices:
   - **99c**: Market effectively resolved to YES
   - **1c**: Market effectively resolved to NO

2. **Determines cashout action** based on current inventory:
   - **YES position at 99c**: Sell at market (best bid) to lock in profits
   - **YES position at 1c**: Sell at market (best bid) to minimize losses
   - **NO position at 1c**: Buy back at market (best ask) to lock in profits
   - **NO position at 99c**: Buy back at market (best ask) to minimize losses

3. **Executes cashout**:
   - Cancels all existing orders for the ticker
   - Places a market order to close the entire position
   - Logs the action with clear indicators (🎯 for resolved market, ✅ for successful cashout)
   - Records metrics for tracking

### Integration into Main Loop
Location: `mm.py`, in the `run()` method

The check is integrated into the main trading loop:
- Called after fetching market touch data and position
- Executed **before** normal order management
- If cashout is triggered, normal order management is skipped for that market

```python
# Check if market is resolved (1c or 99c) and cash out if needed
if self.check_and_cashout_resolved_market(ticker, side, mkt_bid, mkt_ask, inventory):
    # Market is resolved and we attempted to cash out - skip normal order management
    self.logger.info(f"Skipping normal order management for resolved market {ticker}")
    continue
```

## Detection Logic

### 99c Market (Resolved to YES)
```
if mkt_bid >= 0.99 or mkt_ask >= 0.99:
    - inventory > 0  → SELL YES position at mkt_bid
    - inventory < 0  → BUY back NO position at mkt_ask
```

### 1c Market (Resolved to NO)
```
if mkt_bid <= 0.01 or mkt_ask <= 0.01:
    - inventory < 0  → BUY back NO position at mkt_ask
    - inventory > 0  → SELL YES position at mkt_bid
```

## Benefits

1. **Automatic Profit Taking**: Locks in profits on winning positions immediately
2. **Loss Minimization**: Exits losing positions quickly to free up capital
3. **Capital Efficiency**: Closes out positions so capital can be deployed elsewhere
4. **Reduced Risk**: No manual monitoring needed for resolved markets
5. **Clean Exit**: Cancels all orders before placing cashout order to avoid conflicts

## Metrics & Logging

### Logging
The feature provides detailed logging:
```
🎯 RESOLVED MARKET DETECTED: {ticker} at 99c/1c
   Inventory: {inventory}, Bid: {bid}, Ask: {ask}
   Cashing out: {action} at {price}
   Canceled order {order_id} before cashout
   ✅ Cashout order placed: {action} {size} @ {price}, order_id: {oid}
```

### Metrics
The action is recorded with type `"cashout_resolved"` including:
- Action (buy/sell)
- Side (yes/no)
- Price
- Size
- Current inventory
- Market bid/ask at time of cashout

## Testing

Six comprehensive tests cover all scenarios:
1. **YES position at 99c**: Confirms sell order placed
2. **YES position at 1c**: Confirms sell order placed (loss scenario)
3. **NO position at 1c**: Confirms buy order placed (win scenario)
4. **Zero inventory**: Confirms no action taken
5. **Normal prices**: Confirms no action taken
6. **Existing orders**: Confirms orders are canceled before cashout

All tests pass (see `tests/test_cashout.py`).

## Example Scenarios

### Scenario 1: Winning YES Position
- Market maker bought YES at $0.45
- Market resolves, hits $0.99
- Bot automatically sells at $0.99, locking in ~$0.54 profit per contract

### Scenario 2: Losing YES Position
- Market maker bought YES at $0.60
- Market resolves against, hits $0.01
- Bot automatically sells at $0.01, limiting loss to ~$0.59 per contract

### Scenario 3: Winning NO Position
- Market maker effectively sold NO (negative inventory)
- Market hits $0.01 (NO wins)
- Bot buys back at $0.01, locking in profits

## Configuration

No configuration needed - the feature is automatically active for all markets. It respects the existing `my_positions` configuration to avoid interfering with personal positions.

## Future Enhancements

Potential improvements:
1. Configurable threshold (e.g., 2c/98c instead of 1c/99c)
2. Partial cashout options (e.g., close 50% at 95c, 50% at 99c)
3. More sophisticated exit strategies based on volume
4. Alert notifications when cashouts occur

