import logging
import os
import random
import string
import sys
from pathlib import Path
import pytest

# Ensure project root is on path so 'mm' can be imported when running in sandboxes/CI
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mm import LIPBot, MetricsTracker


class FakeAPI:
    def __init__(self, balance: float = 0.0, orders=None):
        self._balance = balance
        self._orders_by_ticker = orders or {}
        self._placed_orders = []
        self._canceled_orders = []

    # Minimal surface used by tests
    def get_balance(self):
        return self._balance

    def get_orders(self, ticker: str):
        return list(self._orders_by_ticker.get(ticker, []))

    def place_order(self, ticker: str, action: str, side: str, price: float, quantity: int, expiration_ts=None):
        order_id = f"fake-order-{len(self._placed_orders)}"
        self._placed_orders.append({
            'order_id': order_id,
            'ticker': ticker,
            'action': action,
            'side': side,
            'price': price,
            'quantity': quantity
        })
        return order_id

    def cancel_order(self, order_id: str):
        self._canceled_orders.append(order_id)
        return True

    # Optional helpers
    def set_orders(self, ticker: str, orders):
        self._orders_by_ticker[ticker] = list(orders)


@pytest.fixture
def test_logger(tmp_path):
    # Separate log per test run to avoid cross-talk
    name = "TEST_" + "".join(random.choices(string.ascii_uppercase, k=6))
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    # Clear any previous handlers
    for h in list(logger.handlers):
        logger.removeHandler(h)
    # File + Console handlers
    fh = logging.FileHandler(tmp_path / f"{name}.log")
    ch = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


@pytest.fixture
def bot_factory(test_logger):
    def _make_bot(balance: float = 100.0, max_position: int = 100, orders=None):
        api = FakeAPI(balance=balance, orders=orders)
        bot = LIPBot(
            logger=test_logger,
            api=api,
            max_position=max_position,
            position_limit_buffer=0.2,
            inventory_skew_factor=0.01,
            stop_event=None,
        )
        # Attach metrics so tests can introspect actions
        bot.metrics = MetricsTracker(strategy_name="TEST", market_ticker=None)
        # Ensure env does not interfere in deterministic tests
        os.environ.setdefault("LIP_RESERVE_FRAC", "0.15")
        os.environ.setdefault("LIP_MARKET_FRAC", "0.25")
        os.environ.setdefault("LIP_FEE_PER_CONTRACT", "0.00")
        return bot, api
    return _make_bot


