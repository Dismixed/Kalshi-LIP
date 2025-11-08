# Resolved Market Logic Update

## Summary
Updated the resolved market detection logic to use orderbook-based thresholds instead of exact 1c/99c prices, making the detection more reliable and acting earlier on markets that are effectively resolved.

## Changes Made

### 1. New Thresholds
- **EDGE_HIGH = 0.985** (98.5c) - Markets at or above this price are treated as "99c"
- **EDGE_LOW = 0.015** (1.5c) - Markets at or below this price are treated as "1c"

These thresholds allow the bot to detect and exit resolved markets earlier, before they hit the absolute extremes.

### 2. Orderbook-Based Detection
The new logic analyzes the full orderbook instead of just the best bid/ask:

```python
def _best_bid(self, levels):
    """Extract best bid from orderbook levels [(price, count), ...]"""
    if not levels:
        return None
    prices = [float(p) for p, cnt in levels if p is not None and (cnt or 0) > 0]
    return max(prices) if prices else None

def resolved_from_bids(self, yes_bids, no_bids):
    """
    Determine if market is resolved based on orderbook bids.
    Returns (resolved: bool, side: 'yes'|'no'|None).
    """
```

### 3. Resolution Detection Logic
The updated logic:

1. **Extracts best bids** from YES and NO orderbook levels
2. **Infers asks** from opposite bids (YES ask = 1 - NO bid, NO ask = 1 - YES bid)
3. **Checks resolution conditions**:
   - **Resolved to YES**: YES bid ≥ 98.5c OR YES ask ≤ 1.5c
   - **Resolved to NO**: NO bid ≥ 98.5c OR NO ask ≤ 1.5c
4. **Handles complementary extremes**: When both YES and NO trigger (e.g., YES at 99c, NO at 1c), the logic recognizes these as compatible signals pointing to the same resolution, not conflicting signals

### 4. Updated Function Signature
```python
# Old signature
check_and_cashout_resolved_market(ticker, side, mkt_bid, mkt_ask, inventory)

# New signature
check_and_cashout_resolved_market(ticker, side, yes_bids, no_bids, mkt_bid, mkt_ask, inventory)
```

### 5. Integration Changes
The main trading loop now:
1. Fetches the full orderbook for each ticker
2. Extracts YES and NO bid levels
3. Passes them to the resolved market check

```python
# Fetch orderbook for resolved market detection
try:
    orderbook = self.api.get_orderbook(ticker)
    yes_bids = orderbook.get("var_true", [])
    no_bids = orderbook.get("var_false", [])
except Exception as e:
    self.logger.warning(f"Failed to get orderbook for {ticker}: {e}")
    yes_bids, no_bids = [], []

# Check if market is resolved and cash out if needed
if self.check_and_cashout_resolved_market(ticker, side, yes_bids, no_bids, mkt_bid, mkt_ask, inventory):
    self.logger.info(f"Skipping normal order management for resolved market {ticker}")
    continue
```

## Benefits

1. **Earlier Detection**: Acts at 98.5c/1.5c instead of waiting for 99c/1c
2. **More Reliable**: Uses full orderbook data instead of just touch prices
3. **Handles Edge Cases**: Properly recognizes complementary extremes (YES at 99c + NO at 1c) as pointing to the same resolution
4. **Conflicting Signal Handling**: Detects truly conflicting orderbook states and skips trading to avoid errors

## Test Coverage

Added 9 comprehensive tests:
1. ✅ YES position cashout at 99c
2. ✅ YES position cashout at 1c (loss scenario)
3. ✅ NO position cashout at 1c (win scenario)
4. ✅ No cashout when inventory is zero
5. ✅ No cashout at normal prices
6. ✅ Cancels existing orders before cashout
7. ✅ **NEW**: Cashout at 98.5c threshold (EDGE_HIGH)
8. ✅ **NEW**: Cashout at 1.5c threshold (EDGE_LOW)
9. ✅ **NEW**: No cashout at 98c (just below threshold)

All tests pass successfully.

## Additional Feature: Inventory Imbalance Exclusion

Resolved markets are now **excluded from inventory imbalance checks**. This prevents false circuit breaker trips when holding large positions in markets that are effectively settled and awaiting cashout.

### Implementation
In the periodic PnL and inventory check (every 60 seconds), the bot now:
1. Fetches the orderbook for each tracked market
2. Checks if the market is resolved using `resolved_from_bids()`
3. **Skips** the inventory imbalance check if the market is resolved
4. Logs a debug message: `"Skipping inventory check for resolved market {ticker}"`

### Why This Matters
- Resolved markets have minimal risk (outcome is nearly certain)
- Positions in resolved markets are awaiting automatic cashout
- Including them in inventory imbalance calculations could trigger false circuit breaker trips
- Allows the bot to hold larger positions in resolved markets while waiting for settlement

### Code Location
Lines 1827-1845 in `mm.py` (periodic PnL and inventory check section)

## Files Modified

1. **mm.py**:
   - Added `_best_bid()` helper method
   - Added `resolved_from_bids()` method with new logic
   - Updated `check_and_cashout_resolved_market()` signature and implementation
   - Updated main trading loop to fetch orderbook and pass to cashout check
   - **Updated inventory imbalance check to exclude resolved markets**

2. **tests/test_cashout.py**:
   - Updated all existing tests to include `yes_bids` and `no_bids` parameters
   - Added 3 new tests for edge threshold behavior

## Backward Compatibility

⚠️ **Breaking Change**: The `check_and_cashout_resolved_market()` method signature has changed. Any external code calling this method will need to be updated to pass the orderbook data.

