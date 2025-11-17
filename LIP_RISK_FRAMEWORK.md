# LIP Risk-Based Quoting Framework

This document describes the Liquidity Incentive Program (LIP) risk-based quoting framework implemented in the market maker.

## Overview

The LIP framework enables intelligent quote placement based on:
1. **Qualifying bands** - Which price levels are LIP-eligible
2. **Market intensity** - How crowded the market is
3. **Risk scoring** - Time decay + volatility from candlestick data
4. **Dynamic positioning** - Where to sit on the book based on risk

## Components

### 1. Qualifying Band Construction

For each market side (YES / NO):
- Walk the order book from best price outward
- Accumulate size until reaching `target_size`
- Each level gets a multiplier: `DF^ticks_from_best`
  - Top of book: `ticks_from_best = 0 → multiplier = 1.0`
  - One tick down: `DF^1` (e.g., 0.95)
  - Two ticks down: `DF^2` (e.g., 0.9025)

```python
# Example: Building a qualifying band
orderbook_levels = [(0.45, 100), (0.44, 150), (0.43, 200)]  # (price, size)
target_size = 300
discount_factor = 0.95

band = bot.build_qualifying_band(
    orderbook_levels=orderbook_levels,
    target_size=target_size,
    is_bid_side=True,
    discount_factor=discount_factor
)
# Returns: [
#   {'price': 0.45, 'size': 100, 'ticks_from_best': 0, 'multiplier': 1.0},
#   {'price': 0.44, 'size': 150, 'ticks_from_best': 1, 'multiplier': 0.95},
#   {'price': 0.43, 'size': 200, 'ticks_from_best': 2, 'multiplier': 0.9025}
# ]
```

### 2. LIP Intensity (Crowding Signal)

Measures how crowded the top of book is:
```
coverage_top = size_at_best / target_size
```

**Interpretation:**
- `< 0.3`: Sparse (opportunity to quote at top)
- `0.3 - 3.0`: Moderate (normal competitive environment)
- `> 3.0`: Heavily crowded (consider backing off or skipping)

```python
intensity = bot.compute_lip_intensity(band, target_size)
# Returns: 0.33 for 100/300 at best
```

### 3. Risk Score Computation

#### 3.1 Time Risk
Exponential decay based on hours to expiry:
```
TimeRisk = exp(-k * hours_to_expiry)
```

With `k ≈ 0.15`:
- 24 hours out: ~0.2 (low risk)
- 6 hours out: ~0.4 (moderate risk)
- 2 hours out: ~0.7 (high risk)
- 30 minutes out: ~0.9 (very high risk)

```python
time_risk = bot.compute_time_risk(ticker, k=0.15)
```

#### 3.2 Volatility Risk
Computed from candlestick data using logit returns:

1. Fetch candlesticks (5-minute intervals, 48-hour lookback)
2. Convert prices to logit space: `logit(p) = log(p / (1 - p))`
3. Compute returns: `r_t = logit_t - logit_{t-1}`
4. Apply EWMA to absolute returns to get `sigma`
5. Convert to percentile rank across markets (optional)
6. Scale: `vol_factor = 1 + gamma * vol_score` (gamma ≈ 2.0)

```python
vol_risk = bot.compute_volatility_risk(
    ticker,
    lookback_hours=48,
    ewma_alpha=0.3
)
```

#### 3.3 Combined Risk Score
```
risk_score = TimeRisk × vol_factor
```

**Interpretation:**
- `< 1.0`: Low risk (far from expiry, stable)
- `1.0 - 2.0`: Moderate risk (normal trading)
- `2.0 - 3.0`: High risk (close to expiry or volatile)
- `> 3.0`: Very high risk (consider skipping)

```python
risk_score = bot.compute_risk_score(
    ticker,
    vol_percentiles=None,  # Optional cross-sectional ranking
    gamma=2.0
)
```

### 4. Quote Level Determination

Decides how many ticks from top of book to quote:

```
max_ticks = floor(alpha * risk_score)
max_ticks = min(max_ticks, max_qual_ticks)  # Respect LIP band limits
```

With `alpha = 1.0`:
- Low risk (score ≈ 0.5): Can quote at top (`max_ticks ≈ 0`)
- Moderate risk (score ≈ 1.5): Can sit 1 tick back (`max_ticks ≈ 1`)
- High risk (score ≈ 2.5): Sit 2 ticks back (`max_ticks ≈ 2`)

**Inventory adjustments:**
- Long inventory: Back off from bids (add ticks)
- Short inventory: Back off from asks (add ticks)

```python
chosen_level = bot.determine_quote_level(
    qualifying_band=bid_band,
    risk_score=2.0,
    alpha=1.0,
    inventory=50,
    max_position=100,
    is_bid=True
)
# Returns: {'price': 0.44, 'size': 150, 'ticks_from_best': 1, 'multiplier': 0.95}
```

## Usage

### Basic Usage

```python
# Compute LIP-adjusted quotes for a market
result = bot.compute_lip_adjusted_quotes(
    ticker="TICKER-XYZ",
    orderbook=orderbook_dict,
    target_size=300,
    inventory=current_inventory,
    discount_factor=0.95,
    risk_threshold=3.0,
    alpha=1.0
)

# Check results
if result['skip_reason']:
    print(f"Skipping market: {result['skip_reason']}")
else:
    print(f"Bid: ${result['bid_price']} for {result['bid_size']} contracts")
    print(f"Ask: ${result['ask_price']} for {result['ask_size']} contracts")
    print(f"Risk Score: {result['risk_score']:.2f}")
    print(f"LIP Intensity (bid): {result['lip_intensity_bid']:.2f}")
```

### Environment Variables

Configure the LIP framework via environment variables:

```bash
# Enable/disable LIP risk-based quoting (default: enabled)
export LIP_RISK_ENABLED=1

# LIP discount factor for multipliers (default: 0.95)
export LIP_DISCOUNT_FACTOR=0.95

# Maximum acceptable risk score (default: 3.0)
export LIP_RISK_THRESHOLD=3.0

# Quote distance scaling factor (default: 1.0)
export LIP_RISK_ALPHA=1.0

# Time risk decay constant (default: 0.15)
export LIP_TIME_RISK_K=0.15

# Volatility scaling factor (default: 2.0)
export LIP_VOL_GAMMA=2.0
```

## Integration Example

To integrate into the trading loop, replace the standard `compute_quotes` call:

```python
# Old approach (simple quote adjustment)
bid, ask = self.compute_quotes(
    mkt_bid, mkt_ask, inventory,
    allow_improvement=True,
    min_width=self.min_quote_width
)

# New approach (LIP risk-adjusted)
if self.lip_enabled and target_size > 0:
    lip_result = self.compute_lip_adjusted_quotes(
        ticker=ticker,
        orderbook=orderbook,
        target_size=target_size,
        inventory=inventory,
        discount_factor=self.lip_discount_factor,
        risk_threshold=self.lip_risk_threshold,
        alpha=self.lip_risk_alpha
    )
    
    if lip_result['skip_reason']:
        self.logger.info(f"Skipping {ticker}: {lip_result['skip_reason']}")
        continue
    
    bid = lip_result['bid_price']
    ask = lip_result['ask_price']
    bid_size = lip_result['bid_size']
    ask_size = lip_result['ask_size']
else:
    # Fallback to standard quoting
    bid, ask = self.compute_quotes(mkt_bid, mkt_ask, inventory)
```

## Market Selection Strategy

Use LIP intensity and risk scores to rank markets:

```python
# Score markets for selection
market_scores = []
for ticker, target_size in candidate_markets:
    # Build qualifying bands
    bid_band = bot.build_qualifying_band(yes_bids, target_size, True)
    
    if not bid_band:
        continue
    
    # Compute metrics
    intensity = bot.compute_lip_intensity(bid_band, target_size)
    risk_score = bot.compute_risk_score(ticker)
    
    # Score: prefer moderate intensity, lower risk
    score = 0.0
    
    # Intensity scoring (prefer 0.3-3.0 range)
    if 0.3 <= intensity <= 3.0:
        intensity_score = 1.0
    elif intensity < 0.3:
        intensity_score = intensity / 0.3  # Scale up sparse markets
    else:
        intensity_score = 3.0 / intensity  # Down-weight crowded markets
    
    # Risk scoring (prefer lower risk)
    risk_score_norm = max(0.0, 1.0 - risk_score / 3.0)
    
    # Combined score
    score = intensity_score * 0.5 + risk_score_norm * 0.5
    
    market_scores.append((ticker, score, intensity, risk_score))

# Sort by score and select top markets
market_scores.sort(key=lambda x: x[1], reverse=True)
selected_markets = market_scores[:10]
```

## Advanced Features

### Cross-Sectional Volatility Ranking

For more sophisticated volatility scoring, compute percentile ranks across all candidate markets:

```python
# Compute raw volatilities for all markets
vol_map = {}
for ticker in candidate_tickers:
    vol_map[ticker] = bot.compute_volatility_risk(ticker)

# Convert to percentile ranks
sorted_vols = sorted(vol_map.values())
vol_percentiles = {}
for ticker, vol in vol_map.items():
    rank = sorted_vols.index(vol) / len(sorted_vols)
    vol_percentiles[ticker] = rank

# Use percentiles in risk score computation
for ticker in candidate_tickers:
    risk_score = bot.compute_risk_score(ticker, vol_percentiles=vol_percentiles)
```

### Dynamic Parameter Adjustment

Adjust parameters based on market conditions:

```python
# More aggressive in quiet markets
if market_volatility < 0.1:
    alpha = 0.5  # Quote closer to top
    risk_threshold = 3.5  # Accept higher risk
else:
    alpha = 1.5  # Back off more
    risk_threshold = 2.5  # Be more selective
```

## Performance Considerations

1. **Candlestick API calls**: Cache results for ~5 minutes to avoid excessive API usage
2. **Risk computation**: Compute once per market per cycle, store in dict
3. **Band construction**: Fast O(n) operation, can be done every cycle
4. **Volatility computation**: Most expensive operation, consider caching

## Testing

Test the framework with various market conditions:

```python
# Test with different risk scenarios
test_cases = [
    {"hours_to_expiry": 24, "volatility": 0.05, "expected_risk": "low"},
    {"hours_to_expiry": 6, "volatility": 0.15, "expected_risk": "moderate"},
    {"hours_to_expiry": 2, "volatility": 0.30, "expected_risk": "high"},
    {"hours_to_expiry": 0.5, "volatility": 0.50, "expected_risk": "very_high"},
]

for test in test_cases:
    # Mock expiry and volatility
    bot._market_end_ts[ticker] = time.time() + test["hours_to_expiry"] * 3600
    # ... mock candlestick data ...
    
    risk_score = bot.compute_risk_score(ticker)
    print(f"Scenario: {test}, Risk Score: {risk_score:.2f}")
```

## Monitoring & Logging

The framework logs key metrics:
- Qualifying band construction results
- LIP intensity signals
- Risk scores and their components
- Quote placement decisions
- Skip reasons

Monitor these logs to tune parameters and understand bot behavior.

## Future Enhancements

Potential improvements:
1. Machine learning for optimal `alpha` and `gamma` parameters
2. Real-time order flow analysis for intensity signals
3. Multi-timeframe volatility analysis
4. Adaptive risk thresholds based on portfolio PnL
5. Correlation-based position sizing across markets

