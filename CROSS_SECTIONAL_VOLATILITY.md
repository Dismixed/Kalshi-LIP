# Cross-Sectional Volatility Ranking

## Overview

The cross-sectional volatility ranking feature automatically computes and caches volatility percentiles across all tracked markets, enabling more intelligent risk scoring and market selection.

## How It Works

### 1. Periodic Refresh

Every `LIP_VOL_REFRESH_INTERVAL` seconds (default: 300 = 5 minutes), the system:

1. **Computes raw volatility** for all tracked markets
   - Uses `compute_volatility_risk()` for each ticker
   - Fetches 48 hours of 5-minute candlesticks
   - Computes logit returns and EWMA volatility

2. **Ranks markets** by volatility
   - Sorts volatilities from lowest to highest
   - Converts to percentile ranks [0, 1]
   - 0 = lowest volatility, 1 = highest volatility

3. **Caches results**
   - Stores raw volatilities in `_vol_cache`
   - Stores percentiles in `_vol_percentiles`
   - Both accessible for 5 minutes before refresh

### 2. Automatic Integration

Risk scoring **automatically uses** cached percentiles when available:

```python
# No need to manually manage percentiles!
risk_score = bot.compute_risk_score(ticker)
# ↑ Automatically uses cached percentiles if available
```

The `compute_risk_score()` method intelligently falls back:
1. First tries explicit `vol_percentiles` parameter (if provided)
2. Then tries cached `_vol_percentiles` (from cross-sectional ranking)
3. Finally falls back to raw volatility with heuristic scaling

### 3. Benefits

**More Accurate Risk Scoring:**
- Volatility is scored relative to other markets, not absolute
- A market with σ=0.2 is "high vol" if others are at 0.05-0.1
- Same market is "medium vol" if others are at 0.2-0.4

**Better Market Selection:**
- Identify truly calm vs. truly volatile markets
- Avoid markets that are outliers in volatility
- Focus on markets with appropriate risk levels

**Adaptive Behavior:**
- Percentiles update as market conditions change
- Bot naturally adjusts to market regime shifts
- No manual parameter tuning needed

## Configuration

### Environment Variables

```bash
# Volatility refresh interval (seconds)
# Default: 300 (5 minutes)
export LIP_VOL_REFRESH_INTERVAL=300

# Volatility scaling factor (used with percentiles)
# Default: 2.0
export LIP_VOL_GAMMA=2.0
```

### Tuning Recommendations

**Frequent Updates (more responsive):**
```bash
export LIP_VOL_REFRESH_INTERVAL=120  # 2 minutes
```
- Pros: Quickly adapts to changing conditions
- Cons: More API calls, higher computation cost

**Infrequent Updates (more stable):**
```bash
export LIP_VOL_REFRESH_INTERVAL=600  # 10 minutes
```
- Pros: Fewer API calls, lower cost
- Cons: Slower to adapt to regime changes

**Recommended (balanced):**
```bash
export LIP_VOL_REFRESH_INTERVAL=300  # 5 minutes (default)
```

## Usage Examples

### Example 1: Automatic Usage (Recommended)

```python
# Just enable LIP and use normally
# Percentiles are computed and used automatically
result = bot.compute_lip_adjusted_quotes(
    ticker="TICKER-XYZ",
    orderbook=orderbook,
    target_size=300,
    inventory=inventory
)
# ↑ Uses cached percentiles automatically
```

### Example 2: Check Cached Percentiles

```python
# Get percentile for a specific market
percentile = bot.get_volatility_percentile("TICKER-XYZ")
if percentile is not None:
    print(f"Volatility percentile: {percentile:.2%}")
    # 0.10 = 10th percentile (calm market)
    # 0.50 = 50th percentile (median)
    # 0.90 = 90th percentile (volatile market)
```

### Example 3: Manual Cross-Sectional Ranking

```python
# Force a refresh for specific tickers
candidate_tickers = ["TICKER-A", "TICKER-B", "TICKER-C"]
bot._refresh_cross_sectional_volatility(candidate_tickers)

# Now use the percentiles
for ticker in candidate_tickers:
    percentile = bot.get_volatility_percentile(ticker)
    risk_score = bot.compute_risk_score(ticker)
    print(f"{ticker}: p{percentile*100:.0f}, risk={risk_score:.2f}")
```

### Example 4: Market Selection by Volatility

```python
# Select markets with moderate volatility
moderate_markets = []

for ticker in candidate_tickers:
    percentile = bot.get_volatility_percentile(ticker)
    
    if percentile is not None and 0.25 <= percentile <= 0.75:
        # Market is in 25th-75th percentile (not extreme)
        moderate_markets.append(ticker)
    
print(f"Found {len(moderate_markets)} moderate volatility markets")
```

### Example 5: Visualize Volatility Distribution

```python
# Get all cached volatilities
volatilities = list(bot._vol_cache.values())
percentiles = list(bot._vol_percentiles.values())

# Print statistics
print(f"Volatility range: [{min(volatilities):.4f}, {max(volatilities):.4f}]")
print(f"Median volatility: {sorted(volatilities)[len(volatilities)//2]:.4f}")

# Print markets by quartile
for ticker, pct in sorted(bot._vol_percentiles.items(), key=lambda x: x[1]):
    if pct < 0.25:
        quartile = "Q1 (low vol)"
    elif pct < 0.50:
        quartile = "Q2"
    elif pct < 0.75:
        quartile = "Q3"
    else:
        quartile = "Q4 (high vol)"
    
    print(f"{ticker}: p{pct*100:.0f} {quartile}")
```

## Implementation Details

### Data Structures

```python
# Instance variables (in LIPBot.__init__)
self._vol_cache: Dict[str, float] = {}  # ticker -> raw σ
self._vol_percentiles: Dict[str, float] = {}  # ticker -> percentile [0, 1]
self._last_vol_refresh_ts = 0.0
self._vol_refresh_interval = 300.0  # seconds
```

### Key Methods

#### `_refresh_cross_sectional_volatility(candidate_tickers: List[str])`
Main refresh method that:
1. Checks if refresh is needed (based on interval)
2. Computes raw volatility for all candidates
3. Ranks and converts to percentiles
4. Caches results
5. Logs summary statistics

#### `get_volatility_percentile(ticker: str) -> Optional[float]`
Simple accessor to get cached percentile for a ticker.

#### `compute_risk_score(ticker: str, use_cached_percentiles: bool = True)`
Updated to automatically use cached percentiles when available.

### Refresh Logic

```python
# In run() method, every loop iteration:
if self.lip_enabled and (now_ts - self._last_vol_refresh_ts) >= self._vol_refresh_interval:
    candidate_tickers = list(tracked_markets.keys())
    if candidate_tickers:
        self._refresh_cross_sectional_volatility(candidate_tickers)
```

## Performance Characteristics

### Computational Cost

**Initial Computation:**
- O(N * M) where N = markets, M = candlesticks per market
- For 50 markets × 576 candlesticks (48h @ 5min): ~29k data points
- Takes ~5-15 seconds depending on API latency

**Subsequent Queries:**
- O(1) lookup in cache
- Instant retrieval of percentiles

### API Usage

**During Refresh:**
- 1 candlestick API call per market
- For 50 markets: 50 API calls every 5 minutes
- ~600 calls/hour during active trading

**Between Refreshes:**
- 0 API calls (uses cache)

### Memory Footprint

- ~50 bytes per market for percentiles
- ~10 KB per market for candlestick data (temporary)
- For 50 markets: ~500 KB total

## Monitoring & Debugging

### Log Messages

The system logs useful information during refresh:

```
[INFO] Refreshing cross-sectional volatility for 42 markets...
[INFO] Volatility stats: min=0.0523, median=0.1247, max=0.3891
[INFO] Most volatile markets:
[INFO]   TICKER-XYZ: σ=0.3891 (p95)
[INFO]   TICKER-ABC: σ=0.3421 (p90)
[INFO]   TICKER-DEF: σ=0.2987 (p85)
```

### Debugging Tips

**Check if percentiles are being used:**
```python
# Enable debug logging
logger.setLevel(logging.DEBUG)

# Look for these log messages:
# "Using cached percentile: 0.756"
# "Using raw volatility σ=0.1234 -> score=0.247"
```

**Manually trigger refresh:**
```python
# Force immediate refresh
bot._last_vol_refresh_ts = 0.0
bot._refresh_cross_sectional_volatility(list(tracked_markets.keys()))
```

**Inspect cache contents:**
```python
# Check what's in the cache
print(f"Cached volatilities: {len(bot._vol_cache)}")
print(f"Cached percentiles: {len(bot._vol_percentiles)}")
print(f"Last refresh: {time.time() - bot._last_vol_refresh_ts:.0f}s ago")

# View specific entries
for ticker in ["TICKER-A", "TICKER-B"]:
    raw_vol = bot._vol_cache.get(ticker)
    percentile = bot._vol_percentiles.get(ticker)
    print(f"{ticker}: σ={raw_vol:.4f}, p={percentile:.2%}")
```

## Troubleshooting

### Problem: Percentiles not being used

**Symptoms:** Risk scores seem inconsistent, logs show "Using raw volatility"

**Solution:**
1. Check if LIP is enabled: `bot.lip_enabled == True`
2. Verify refresh interval not too long: Check `LIP_VOL_REFRESH_INTERVAL`
3. Ensure markets are being tracked: `len(tracked_markets) > 0`
4. Manually trigger refresh to test

### Problem: Too many API calls

**Symptoms:** API rate limit errors, slow performance

**Solution:**
```bash
# Increase refresh interval
export LIP_VOL_REFRESH_INTERVAL=600  # 10 minutes

# Or reduce number of tracked markets
# (only track markets you're actively quoting)
```

### Problem: Percentiles seem stale

**Symptoms:** Rankings don't reflect current market conditions

**Solution:**
```bash
# Decrease refresh interval
export LIP_VOL_REFRESH_INTERVAL=120  # 2 minutes

# Or manually trigger refresh more frequently
bot._refresh_cross_sectional_volatility(tickers)
```

### Problem: All markets have similar percentiles

**Symptoms:** Percentiles clustered around 0.5, not spread out

**Cause:** Markets actually have similar volatilities (working as intended)

**Verification:**
```python
# Check raw volatility spread
vols = list(bot._vol_cache.values())
spread = max(vols) - min(vols)
print(f"Volatility spread: {spread:.4f}")

# If spread < 0.05, markets truly are similar
# This is fine - it just means uniform market conditions
```

## Best Practices

### 1. Let it Run Automatically

The default configuration works well:
- 5-minute refresh interval balances API usage and freshness
- Automatic integration means no code changes needed
- Cache warm-up happens naturally during trading

### 2. Monitor Initial Refresh

First refresh after startup takes longer:
- All volatilities computed from scratch
- Watch logs for completion
- Subsequent refreshes are incremental (cached)

### 3. Use for Market Selection

Combine volatility percentiles with other signals:

```python
def score_market(ticker):
    percentile = bot.get_volatility_percentile(ticker)
    risk_score = bot.compute_risk_score(ticker)
    lip_intensity = bot.compute_lip_intensity(...)
    
    # Prefer low-medium volatility markets
    if percentile and percentile > 0.8:
        vol_score = 0.3  # Penalize high vol
    elif percentile and percentile < 0.2:
        vol_score = 1.0  # Reward low vol
    else:
        vol_score = 0.7  # Medium vol is okay
    
    return vol_score * 0.4 + other_factors * 0.6
```

### 4. Adapt to Market Regimes

During high-volatility regimes (e.g., news events):
- Percentiles automatically adjust
- Markets that were "high vol" become "medium vol"
- Bot naturally adapts risk taking

### 5. Use for Risk Limits

Implement portfolio-level vol limits:

```python
# Don't take on too many high-vol positions
high_vol_positions = sum(
    1 for ticker in portfolio
    if bot.get_volatility_percentile(ticker) and 
       bot.get_volatility_percentile(ticker) > 0.75
)

if high_vol_positions > 3:
    # Skip additional high-vol markets
    pass
```

## Advanced: Custom Ranking Algorithms

You can implement custom ranking logic:

```python
def custom_volatility_ranking(tickers, lookback_days=2):
    """Custom ranking with different parameters"""
    vol_map = {}
    
    for ticker in tickers:
        # Use custom lookback period
        vol = bot.compute_volatility_risk(
            ticker,
            lookback_hours=lookback_days * 24,
            ewma_alpha=0.2  # Different smoothing
        )
        vol_map[ticker] = vol
    
    # Custom ranking (e.g., log-scale)
    import math
    log_vols = {t: math.log(v + 0.001) for t, v in vol_map.items()}
    sorted_vols = sorted(log_vols.values())
    
    percentiles = {}
    for ticker, log_vol in log_vols.items():
        rank = sorted_vols.index(log_vol)
        percentiles[ticker] = rank / (len(sorted_vols) - 1)
    
    return percentiles

# Use custom ranking
custom_pct = custom_volatility_ranking(candidate_tickers)
risk_score = bot.compute_risk_score(ticker, vol_percentiles=custom_pct)
```

## Summary

Cross-sectional volatility ranking provides:

✅ **Automatic** - Works out of the box, no code changes needed  
✅ **Intelligent** - Relative rankings better than absolute values  
✅ **Efficient** - Caching minimizes API calls  
✅ **Adaptive** - Updates as market conditions change  
✅ **Integrated** - Seamlessly used by risk scoring  

Enable it, configure the refresh interval, and let it run!

---

**Added**: November 2025  
**Status**: ✅ Production-Ready  
**Related Docs**: `LIP_RISK_FRAMEWORK.md`, `LIP_IMPLEMENTATION_SUMMARY.md`

