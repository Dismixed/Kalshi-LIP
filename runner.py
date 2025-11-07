import argparse
import logging
import yaml
from dotenv import load_dotenv
import os
import signal
import threading
import time
import traceback
import json

from mm import KalshiTradingAPI, LIPBot, AlertLevel

def load_config(config_file):
    with open(config_file, 'r') as f:
        return yaml.safe_load(f)

def build_logger(name_suffix: str, level_name: str = 'INFO') -> logging.Logger:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logger = logging.getLogger(f"Strategy_{name_suffix}")
    logger.propagate = False
    logger.setLevel(level)

    for h in list(logger.handlers):
        logger.removeHandler(h)

    log_filename = f"{name_suffix}.log"
    fh = logging.FileHandler(log_filename, encoding='utf-8')
    fh.setLevel(level)

    ch = logging.StreamHandler()
    ch.setLevel(level)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

def create_api(api_config, logger):
    return KalshiTradingAPI(
        email=os.getenv("KALSHI_EMAIL"),
        password=os.getenv("KALSHI_PASSWORD"),
        base_url="https://api.elections.kalshi.com/trade-api/v2",
        logger=logger,
    )

def _install_signal_handlers(stop_event: threading.Event):
    def handle_signal(signum, frame):
        if not stop_event.is_set():
            print("\nSignal received. Stopping gracefully... (press Ctrl-C again to force)")
            stop_event.set()
        else:
            raise KeyboardInterrupt
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple Kalshi Market Maker Runner")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    load_dotenv()
    configs = load_config(args.config)
    if not isinstance(configs, dict) or not configs:
        raise SystemExit("Config must be a mapping")

    # Support flat single-config (no strategies) and legacy multi-strategy
    if 'api' in configs and 'market_maker' in configs:
        config_name = 'DEFAULT'
        config = configs
    else:
        config_name, config = next(iter(configs.items()))

    api_cfg = config.get('api', {})
    mm_cfg = config.get('market_maker', {})
    log_level = config.get('log_level', 'INFO')

    logger = build_logger(f"{config_name}", level_name=log_level)
    logger.info(f"Starting strategy {config_name}")

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    api = create_api(api_cfg, logger)
    
    # Get circuit breaker config
    cb_cfg = config.get('circuit_breaker', {})
    
    bot = LIPBot(
        logger=logger,
        api=api,
        max_position=mm_cfg.get('max_position', 100),
        position_limit_buffer=mm_cfg.get('position_limit_buffer', 0.1),
        inventory_skew_factor=mm_cfg.get('inventory_skew_factor', 0.01),
        improve_once_per_touch=mm_cfg.get('improve_once_per_touch', True),
        improve_cooldown_seconds=mm_cfg.get('improve_cooldown_seconds', 0),
        min_quote_width_cents=mm_cfg.get('min_quote_width_cents', 0),
        stop_event=stop_event,
        max_consecutive_errors=cb_cfg.get('max_consecutive_errors', 10),
        pnl_threshold=cb_cfg.get('pnl_threshold', -100.0),
        max_inventory_imbalance=cb_cfg.get('max_inventory_imbalance', 0.8),
    )

    try:
        bot.run(config.get('dt', 1.0))
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    except Exception as e:
        logger.error(f"Runner error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Send critical alert on crash
        try:
            bot.alert_manager.send_alert(
                AlertLevel.CRITICAL,
                "bot_crash",
                f"Bot crashed with error: {str(e)}",
                {'error': str(e), 'traceback': traceback.format_exc()}
            )
        except Exception:
            pass
    finally:
        try:
            bot.export_metrics()
        except Exception:
            logger.warning("Failed to export metrics on shutdown")
        try:
            # Export circuit breaker status
            cb_status = bot.circuit_breaker.get_status()
            with open(f"{config_name}_circuit_breaker_status.json", 'w') as f:
                json.dump(cb_status, f, indent=2)
        except Exception:
            pass
        try:
            api.logout()
        except Exception:
            pass
        # small delay to flush logs
        time.sleep(0.2)