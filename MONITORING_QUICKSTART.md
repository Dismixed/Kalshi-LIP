# Monitoring & Safety Features - Quick Start

## What's New

Your market maker bot now has production-grade monitoring and safety systems:

### 🛡️ Circuit Breaker
- **Auto-stops trading** on consecutive API errors (default: 10)
- **Auto-stops trading** if PnL drops below threshold (default: -$100)
- **Auto-stops trading** on high inventory imbalance (default: >80%)
- Status saved to `<strategy>_circuit_breaker_status.json`

### 📊 Enhanced Metrics

**Tracked automatically:**
- ✅ PnL (realized + unrealized per market)
- ✅ Inventory exposure per market
- ✅ Order success rate (%)
- ✅ API error count
- ✅ Quote latency (ms)
- ✅ Spread width

**Exported to:**
- `Strategy_<NAME>_metrics.json` - Summary
- `Strategy_<NAME>_trading.jsonl` - All events (structured JSON)

### 🚨 Alert System

**Alerts logged to `alerts.jsonl`:**
- Bot startup/shutdown
- Circuit breaker trips
- High inventory imbalance warnings
- API disconnections
- Bot crashes

### 📝 Structured Logging

Every trading action logged as JSON:
```bash
tail -f Strategy_DEFAULT_trading.jsonl | jq .
```

**Events logged:**
- `order_sent` - Order submitted
- `order_acknowledged` - Exchange confirmed
- `order_rejected` - Exchange rejected
- `order_canceled` - Order canceled
- `fill` - Order filled
- `inventory_change` - Position changed
- `pnl_snapshot` - Periodic PnL
- `api_error` - API failures

## Configuration

Edit `config.yaml`:

```yaml
# Circuit breaker settings
circuit_breaker:
  max_consecutive_errors: 10    # API error threshold
  pnl_threshold: -100.0         # PnL stop-loss ($)
  max_inventory_imbalance: 0.8  # Inventory threshold (0.8 = 80%)
```

## Quick Commands

### Monitor in Real-Time

```bash
# Watch alerts
tail -f alerts.jsonl

# Watch trading events (formatted)
tail -f Strategy_DEFAULT_trading.jsonl | jq .

# Watch main log
tail -f DEFAULT.log
```

### Check Status

```bash
# Circuit breaker status
cat DEFAULT_circuit_breaker_status.json | jq .

# Latest metrics
cat Strategy_DEFAULT_metrics.json | jq .summary

# Count API errors
cat Strategy_DEFAULT_trading.jsonl | jq 'select(.event_type == "api_error")' | wc -l

# Order success rate
cat Strategy_DEFAULT_metrics.json | jq .summary.order_success_rate_pct
```

### Find Problems

```bash
# Show critical alerts only
cat alerts.jsonl | jq 'select(.level == "critical")'

# Show recent API errors
cat Strategy_DEFAULT_trading.jsonl | jq 'select(.event_type == "api_error")' | tail -10

# Show fills from last hour
cat Strategy_DEFAULT_trading.jsonl | \
  jq "select(.event_type == \"fill\" and .timestamp > $(date -u -d '1 hour ago' +%s))"
```

## Auto-Restart Setup

### Option 1: Systemd (Linux)

```bash
# Install service
sudo cp kalshi-market-maker.service /etc/systemd/system/
sudo nano /etc/systemd/system/kalshi-market-maker.service  # Edit paths

# Start service
sudo systemctl daemon-reload
sudo systemctl enable kalshi-market-maker
sudo systemctl start kalshi-market-maker

# Check status
sudo systemctl status kalshi-market-maker
sudo journalctl -u kalshi-market-maker -f
```

### Option 2: Supervisor

```bash
# Install
sudo apt-get install supervisor

# Setup
sudo cp supervisor-kalshi-mm.conf /etc/supervisor/conf.d/
sudo nano /etc/supervisor/conf.d/supervisor-kalshi-mm.conf  # Edit paths

# Start
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start kalshi-market-maker
```

## What to Watch

### ⚠️ Warning Signs

1. **Order success rate < 90%**
   ```bash
   cat Strategy_DEFAULT_metrics.json | jq .summary.order_success_rate_pct
   ```

2. **API errors increasing**
   ```bash
   cat Strategy_DEFAULT_trading.jsonl | \
     jq 'select(.event_type == "api_error")' | \
     jq -r '.timestamp' | \
     awk '{print strftime("%Y-%m-%d %H:%M:%S", $1)}' | \
     uniq -c
   ```

3. **High quote latency (>500ms)**
   ```bash
   cat Strategy_DEFAULT_metrics.json | jq .summary.avg_quote_latency_ms
   ```

4. **Circuit breaker tripped**
   ```bash
   cat DEFAULT_circuit_breaker_status.json | jq .is_open
   # false = tripped, true = ok
   ```

### 🎯 Healthy Status

- Order success rate: >95%
- API errors: <5 per hour
- Quote latency: <200ms
- Circuit breaker: open (is_open: true)
- PnL: above threshold
- Inventory: balanced across markets

## Emergency Actions

### Circuit Breaker Tripped

1. **Check reason:**
   ```bash
   cat DEFAULT_circuit_breaker_status.json | jq .trip_reason
   ```

2. **Fix issue** (API credentials, balance, code bug)

3. **Restart bot:**
   ```bash
   sudo systemctl restart kalshi-market-maker
   # or
   sudo supervisorctl restart kalshi-market-maker
   ```

### Bot Crashed

```bash
# Check crash alert
cat alerts.jsonl | jq 'select(.category == "bot_crash")' | tail -1

# Check logs
tail -100 DEFAULT.log

# Systemd will auto-restart - check status
sudo systemctl status kalshi-market-maker
```

### High Inventory

```bash
# Check current positions
cat Strategy_DEFAULT_trading.jsonl | \
  jq 'select(.event_type == "inventory_change")' | \
  tail -5

# May need to manually close positions or adjust max_position in config
```

## Files Generated

| File | Purpose |
|------|---------|
| `alerts.jsonl` | All alerts (critical/warning/info) |
| `Strategy_<NAME>_trading.jsonl` | Structured event log |
| `Strategy_<NAME>_metrics.json` | Summary statistics |
| `<NAME>_circuit_breaker_status.json` | Circuit breaker state |
| `Strategy_<NAME>_loops.csv` | Loop-by-loop data |
| `Strategy_<NAME>_actions.csv` | Order actions |
| `Strategy_<NAME>_latencies.csv` | Latency measurements |
| `<NAME>.log` | Standard log output |

## Next Steps

1. **Test locally** - Run bot and watch logs
2. **Deploy with auto-restart** - Use systemd/supervisor
3. **Set up monitoring dashboard** - Parse JSON logs
4. **Configure alerts** - Add email/SMS notifications
5. **Tune thresholds** - Adjust circuit breaker settings

See `DEPLOYMENT_MONITORING.md` for detailed documentation.

## Support Checklist

When troubleshooting, collect:
- [ ] `alerts.jsonl` (last 100 lines)
- [ ] `<NAME>_circuit_breaker_status.json`
- [ ] `Strategy_<NAME>_metrics.json`
- [ ] `<NAME>.log` (last 500 lines)
- [ ] Recent entries from `Strategy_<NAME>_trading.jsonl`

```bash
# Quick diagnostics bundle
tar -czf diagnostics.tar.gz \
  alerts.jsonl \
  DEFAULT_circuit_breaker_status.json \
  Strategy_DEFAULT_metrics.json \
  DEFAULT.log \
  Strategy_DEFAULT_trading.jsonl
```

