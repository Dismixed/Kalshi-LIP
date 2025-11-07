# Kalshi LIP Bot

This project implements a LIP (Liquidity Improvement Program) bot for Kalshi markets. It provides a configurable LIP quoting loop with risk controls and logging. The current runner executes a single strategy per process using the configuration in `config.yaml`.

## Local Setup

1. Clone the repository
2. (Recommended) Use Python 3.11 and a virtualenv, then install dependencies:
   ```
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your Kalshi credentials:
   ```
   KALSHI_EMAIL=your_email
   KALSHI_PASSWORD=your_password
   ```
4. Create or modify the `config.yaml` file with your LIP bot configuration (see below).
5. Run the script:
   ```
   python runner.py --config config.yaml
   ```

## Configuration

The `config.yaml` file defines a single strategy. Key sections:

- `api`: Parameters related to data selection. Leave empty to auto-scan liquid markets.
- `market_maker`: Core LIP quoting and risk parameters (section name retained as `market_maker`).
- `circuit_breaker`: Safety limits to stop the bot under adverse conditions.
- `dt`: Time step (seconds) for the bot's main loop.
- `log_level`: `DEBUG|INFO|WARN|ERROR`.

Example:

```yaml
api:
  # no specific market; the bot will scan liquid markets
market_maker:
  max_position: 100                 # inventory cap
  position_limit_buffer: 0.2        # start leaning before the cap
  inventory_skew_factor: 0.01       # tilt quotes as inventory grows
  improve_once_per_touch: true      # one nudge per external touch change
  improve_cooldown_seconds: 0       # cooldown between nudges (0 disables)
  min_quote_width_cents: 0          # optional spread floor in cents

circuit_breaker:
  max_consecutive_errors: 10        # trip after N consecutive API errors
  pnl_threshold: -100.0             # stop if PnL drops below this ($)
  max_inventory_imbalance: 0.9      # stop if inventory > 90% of max

dt: 1.0                              # main loop refresh
log_level: INFO
```

Note: The current `runner.py` executes one strategy per run. To run multiple strategies, start multiple processes (each with its own config or environment) or orchestrate separate deployments.

## Deploying on fly.io

1. Install the flyctl CLI: [Install flyctl](https://fly.io/docs/hands-on/install-flyctl/)
2. Login to fly.io:
   ```
   flyctl auth login
   ```
3. Navigate to your project directory and initialize your fly.io app:
   ```
   flyctl launch
   ```
   Follow the prompts, but don't deploy yet.
4. Set your Kalshi credentials as secrets:
   ```
   flyctl secrets set KALSHI_EMAIL=your_email
   flyctl secrets set KALSHI_PASSWORD=your_password
   ```
5. Ensure your `config.yaml` file is in the project directory and contains all the strategies you want to run.
6. Deploy the app:
   ```
   flyctl deploy
   ```

The deployment runs `runner.py` using your `config.yaml`.

## Monitoring

The bot logs to a file named after the config (e.g., `DEFAULT.log`) and also to stdout. You can monitor app logs via fly.io:

```
flyctl logs
```

For more details on monitoring and alerting, see `MONITORING_QUICKSTART.md`.

## Outputs

During runtime and shutdown, the bot exports metrics and traces to CSV/JSON files in the project directory. Filenames are prefixed with the strategy identifier, for example:

- `Strategy_DEFAULT_actions.csv`
- `Strategy_DEFAULT_latencies.csv`
- `Strategy_DEFAULT_loops.csv`
- `Strategy_DEFAULT_metrics.json`
- `DEFAULT_circuit_breaker_status.json`

## Testing

Run the test suite with:

```
pytest
```
