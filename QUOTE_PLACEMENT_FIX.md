# Quote Placement Fix - No Longer Always Below Touch

## Problem
The bot was **always placing orders below the touch** (below best bid for bids, below best ask for asks) instead of improving or joining the market.

## Root Cause
In `compute_lip_adjusted_quotes()`, the bot was using **existing orderbook levels from other traders** as the basis for quote placement, rather than the actual **Best Bid/Offer (BBO)** touch prices.

### Original Flawed Logic
```python
# Old code was building qualifying bands from existing orders
yes_bids = [(to_tick(p), sz) for (p, sz) in (orderbook.get("var_true") or [])]
bid_band = self.build_qualifying_band(yes_bids, target_size, ...)
```

This meant:
- If existing orders were at [45¢, 44¢, 43¢]
- The bot treated 45¢ as "best" (ticks_from_best=0)
- But the actual BBO bid might have been 46¢!
- Result: Bot placed orders at 45¢, which is BELOW the touch

## Solution

### 1. Extract Actual BBO and Check LIP Target
Now the bot explicitly extracts the current market BBO and checks if the target is already met:
```python
bbo_bid = max(p for p, _ in yes_bids)  # Actual best bid in market
bbo_ask = min(p for p, _ in yes_asks)  # Actual best ask in market

# Check if LIP target is already met
best_bid_size = sum(sz for p, sz in yes_bids if p == bbo_bid)
if best_bid_size >= target_size:
    # Skip this market - target already met
    return
```

### 2. Generate Levels Relative to BBO
Instead of using existing orders, generate potential quote levels relative to the BBO.

**NEVER improves the touch - only joins or sits behind:**

**For Bids:**
- `ticks_from_best = 0` → BBO (JOIN the touch)
- `ticks_from_best = 1` → BBO - 1¢ (1 tick passive)
- `ticks_from_best = 2` → BBO - 2¢ (2 ticks passive)

**For Asks:**
- `ticks_from_best = 0` → BBO (JOIN the touch)
- `ticks_from_best = 1` → BBO + 1¢ (1 tick passive)
- `ticks_from_best = 2` → BBO + 2¢ (2 ticks passive)

```python
# Generate bid levels starting from BBO
bid_levels = []
for ticks in range(0, max_levels):  # Start at 0 (join), never improve
    price = to_tick(bbo_bid - ticks * tick_size)  # 0 → join, 1+ → passive
    if 0.01 <= price <= 0.99:
        bid_levels.append({
            'price': price,
            'size': target_size,
            'ticks_from_best': ticks,
            'multiplier': discount_factor ** ticks
        })
```

### 3. Risk-Based Discrete Buckets
Updated logic to use **discrete risk buckets** with tunable thresholds:

```python
# Get tunable thresholds from environment
medium_risk_threshold = float(os.getenv("LIP_MEDIUM_RISK_THRESHOLD", "1.5"))
high_risk_threshold = float(os.getenv("LIP_HIGH_RISK_THRESHOLD", "2.5"))

# Categorize risk
if risk_score < medium_risk_threshold:
    # LOW RISK: Sit at touch (ticks=0)
    target_ticks = 0
elif risk_score < high_risk_threshold:
    # MEDIUM RISK: One tick behind (ticks=1)
    target_ticks = 1
else:
    # HIGH RISK: Skip market entirely
    return  # No quotes placed
```

**Tunable via environment variables:**
```bash
export LIP_MEDIUM_RISK_THRESHOLD=1.5  # Default: below this = LOW
export LIP_HIGH_RISK_THRESHOLD=2.5    # Default: above this = SKIP
```

### 4. Inventory Adjustments
When carrying inventory, the bot backs off appropriately:
- **Long inventory** → Less aggressive on bids (sit further back)
- **Short inventory** → Less aggressive on asks (sit further back)

```python
if is_bid and inventory > 0:
    target_ticks += int(inventory_factor * 3)  # Back off from aggressive bidding
```

## Behavior Changes

### Before Fix
- ❌ Always placed orders at or below existing order levels
- ❌ Never improved or joined the actual touch
- ❌ Risk score didn't affect quote aggressiveness properly

### After Fix - Discrete Risk Buckets
- ✅ **Skips markets where LIP target is already met** (best bid size ≥ target)
- ✅ **LOW risk** (< 1.5 default) → **Joins the touch** (ticks=0)
- ✅ **MEDIUM risk** (1.5-2.5 default) → **One tick behind** (ticks=1)
- ✅ **HIGH risk** (≥ 2.5 default) → **Skips market** (no quotes)
- ✅ **Never improves the touch** - only joins or sits behind
- ✅ **Tunable thresholds** via environment variables
- ✅ **Risk scores logged** for every market
- ✅ Inventory skew properly reduces aggressiveness

## Logging
Added detailed logging to show LIP target status, risk scores, and quote placement decisions:

**LIP Target Check:**
```
[INFO] KXMARKET-TICKER: LIP target already met: best bid size 150 >= target 100
```

**Risk Score Logging:**
```
[INFO] KXMARKET-TICKER: Risk score = 0.847 [LOW] → sit at touch
[INFO] KXMARKET-TICKER: Risk score = 1.923 [MEDIUM] → sit 1 tick behind
[INFO] KXMARKET-TICKER: Risk score = 2.678 [HIGH] → SKIP
```

**Quote Placement Logging:**
```
[INFO] KXMARKET-TICKER: BID 0.45 JOINS touch (BBO=0.45)
[INFO] KXMARKET-TICKER: BID 0.44 passive (BBO=0.45, 1 tick(s) behind)
```

## Files Modified
- `mm.py`:
  - `compute_lip_adjusted_quotes()` (lines ~3646-3788)
  - `determine_quote_level()` (lines ~2602-2663)

## Testing
To verify the fix is working:

1. **Check risk score logs** - every market should show:
   ```
   [INFO] TICKER: Risk score = X.XXX [LOW/MEDIUM/HIGH] → action
   ```

2. **Verify discrete bucket behavior**:
   - Risk < 1.5 (LOW) → See "sit at touch" + BID/ASK "JOINS touch"
   - Risk 1.5-2.5 (MEDIUM) → See "sit 1 tick behind" + BID/ASK "passive (1 tick behind)"
   - Risk ≥ 2.5 (HIGH) → See "SKIP" + no order placement

3. **Verify prices**:
   - Bid prices are **at or below** BBO bid (join or passive, never improve)
   - Ask prices are **at or above** BBO ask (join or passive, never improve)

4. **Tune thresholds** - test different values:
   ```bash
   export LIP_MEDIUM_RISK_THRESHOLD=1.0  # More aggressive (more markets at touch)
   export LIP_HIGH_RISK_THRESHOLD=3.0    # Less filtering (quote more markets)
   ```

5. **Monitor inventory effects** - confirm inventory backing off works correctly

6. **Production calibration** - use risk score logs to optimize thresholds for your strategy

