import abc
from re import L, M
import time
from typing import Dict, List, Tuple, Optional
import threading
import requests
import logging
import uuid
import math
import os
from decimal import Decimal, ROUND_HALF_UP
import kalshi_python
from kalshi_python import Configuration, KalshiClient
from kalshi_python.models.create_order_request import CreateOrderRequest
import json
from collections import defaultdict
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization
import requests
import datetime
import sys
import traceback
from dataclasses import dataclass, asdict, field
from enum import Enum

def to_tick(p: float) -> float:
    # Clamp to valid cents 0.01..0.99 and use round-half-up to 2 decimals
    d = Decimal(str(p))
    # quantize to 2 decimals with HALF_UP
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # clamp after rounding
    q = max(Decimal("0.01"), min(Decimal("0.99"), q))
    return float(q)

def to_cents(p: float) -> int:
    # Ensure consistency with to_tick rounding
    cents = Decimal(str(to_tick(p))) * Decimal(100)
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class AlertLevel(Enum):
    """Alert severity levels"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Structured alert message"""
    timestamp: float
    level: AlertLevel
    category: str
    message: str
    details: Dict = field(default_factory=dict)

    def to_json(self) -> str:
        data = {
            'timestamp': self.timestamp,
            'timestamp_iso': datetime.datetime.fromtimestamp(self.timestamp).isoformat(),
            'level': self.level.value,
            'category': self.category,
            'message': self.message,
            'details': self.details
        }
        return json.dumps(data)


class AlertManager:
    """Manages alerts and sends notifications"""
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.alerts: List[Alert] = []
        self.alert_file = "alerts.jsonl"
        
    def send_alert(self, level: AlertLevel, category: str, message: str, details: Dict = None):
        """Send an alert and log it"""
        alert = Alert(
            timestamp=time.time(),
            level=level,
            category=category,
            message=message,
            details=details or {}
        )
        self.alerts.append(alert)
        
        # Log to file
        try:
            with open(self.alert_file, 'a') as f:
                f.write(alert.to_json() + '\n')
        except Exception as e:
            self.logger.error(f"Failed to write alert to file: {e}")
        
        # Log based on severity
        log_msg = f"[{category.upper()}] {message}"
        if details:
            log_msg += f" | Details: {json.dumps(details)}"
            
        if level == AlertLevel.CRITICAL:
            self.logger.critical(log_msg)
        elif level == AlertLevel.WARNING:
            self.logger.warning(log_msg)
        else:
            self.logger.info(log_msg)


class CircuitBreaker:
    """Circuit breaker to stop trading on consecutive errors or PnL drops"""
    def __init__(
        self,
        max_consecutive_errors: int = 10,
        pnl_threshold: float = -100.0,
        max_inventory_imbalance: float = 0.8,
        logger: Optional[logging.Logger] = None,
        alert_manager: Optional[AlertManager] = None
    ):
        self.max_consecutive_errors = max_consecutive_errors
        self.pnl_threshold = pnl_threshold
        self.max_inventory_imbalance = max_inventory_imbalance
        self.logger = logger or logging.getLogger(__name__)
        self.alert_manager = alert_manager
        
        self.consecutive_errors = 0
        self.is_open = True  # True = allow trading
        self.trip_reason: Optional[str] = None
        self.trip_time: Optional[float] = None
        self.error_log: List[Dict] = []
        self.lock = threading.Lock()
        
    def record_success(self):
        """Record successful API call"""
        with self.lock:
            self.consecutive_errors = 0
            
    def record_error(self, error_type: str, error_msg: str):
        """Record API error and potentially trip circuit"""
        with self.lock:
            self.consecutive_errors += 1
            self.error_log.append({
                'timestamp': time.time(),
                'type': error_type,
                'message': error_msg,
                'consecutive_count': self.consecutive_errors
            })
            
            if self.consecutive_errors >= self.max_consecutive_errors and self.is_open:
                self._trip(f"Too many consecutive API errors ({self.consecutive_errors})")
                
    def check_pnl(self, current_pnl: float):
        """Check if PnL has dropped below threshold"""
        with self.lock:
            if current_pnl < self.pnl_threshold and self.is_open:
                self._trip(f"PnL below threshold: ${current_pnl:.2f} < ${self.pnl_threshold:.2f}")
                
    def check_inventory_imbalance(self, inventory: int, max_position: int):
        """Check if inventory is too imbalanced"""
        with self.lock:
            if max_position > 0:
                imbalance = abs(inventory) / max_position
                if imbalance > self.max_inventory_imbalance and self.is_open:
                    self._trip(f"Inventory imbalance too high: {imbalance:.1%} (inventory={inventory}, max={max_position})")
                    
    def _trip(self, reason: str):
        """Trip the circuit breaker (internal, assumes lock is held)"""
        self.is_open = False
        self.trip_reason = reason
        self.trip_time = time.time()
        
        self.logger.critical(f"CIRCUIT BREAKER TRIPPED: {reason}")
        if self.alert_manager:
            self.alert_manager.send_alert(
                AlertLevel.CRITICAL,
                "circuit_breaker",
                f"Circuit breaker tripped: {reason}",
                {
                    'trip_time': self.trip_time,
                    'consecutive_errors': self.consecutive_errors,
                    'recent_errors': self.error_log[-5:] if self.error_log else []
                }
            )
            
    def reset(self):
        """Manually reset the circuit breaker"""
        with self.lock:
            was_open = self.is_open
            self.is_open = True
            self.consecutive_errors = 0
            self.trip_reason = None
            self.trip_time = None
            
            if not was_open:
                self.logger.info("Circuit breaker manually reset")
                if self.alert_manager:
                    self.alert_manager.send_alert(
                        AlertLevel.INFO,
                        "circuit_breaker",
                        "Circuit breaker manually reset",
                        {}
                    )
                    
    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed"""
        with self.lock:
            return self.is_open
            
    def get_status(self) -> Dict:
        """Get current circuit breaker status"""
        with self.lock:
            return {
                'is_open': self.is_open,
                'consecutive_errors': self.consecutive_errors,
                'trip_reason': self.trip_reason,
                'trip_time': self.trip_time,
                'recent_errors': self.error_log[-10:] if self.error_log else []
            }


class MetricsTracker:
    def __init__(self, strategy_name: str, market_ticker: Optional[str] = None):
        self.strategy_name = strategy_name
        self.market_ticker = market_ticker
        self.start_time = time.time()
        self.loop_snapshots: List[Dict] = []
        self.action_log: List[Dict] = []
        self.latencies: List[Dict] = []
        
        # Enhanced metrics tracking
        self.api_errors: List[Dict] = []
        self.fills: List[Dict] = []
        self.inventory_changes: List[Dict] = []
        self.pnl_snapshots: List[Dict] = []
        
        # Aggregate counters
        self.orders_sent = 0
        self.orders_acknowledged = 0
        self.orders_rejected = 0
        self.api_error_count = 0
        
        # Structured JSON log file
        self.json_log_file = f"{strategy_name.replace(':', '_').replace(' ', '_')}_trading.jsonl"
        
    def log_structured(self, event_type: str, data: Dict):
        """Write structured JSON log entry"""
        entry = {
            'timestamp': time.time(),
            'timestamp_iso': datetime.datetime.now().isoformat(),
            'event_type': event_type,
            'strategy': self.strategy_name,
            'market': self.market_ticker,
            **data
        }
        try:
            with open(self.json_log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            # Don't let logging failures break the bot
            pass

    def record_loop(self, t_seconds: float, mid_price: float, inventory: int,
                    reservation_price: float, bid_price: float, ask_price: float,
                    buy_size: int, sell_size: int) -> None:
        self.loop_snapshots.append({
            "t_seconds": round(t_seconds, 3),
            "mid_price": round(mid_price, 4),
            "inventory": int(inventory),
            "reservation_price": round(reservation_price, 4),
            "bid_price": round(bid_price, 4),
            "ask_price": round(ask_price, 4),
            "buy_size": int(buy_size),
            "sell_size": int(sell_size),
        })

    def record_action(self, kind: str, details: Dict) -> None:
        entry = {"ts": time.time(), "kind": kind}
        entry.update(details or {})
        self.action_log.append(entry)

    def record_latency(self, name: str, seconds: float) -> None:
        self.latencies.append({
            "ts": time.time(),
            "name": name,
            "ms": round(seconds * 1000.0, 2)
        })
        
    def record_order_sent(self, ticker: str, side: str, action: str, price: float, size: int):
        """Record order sent to exchange"""
        self.orders_sent += 1
        self.log_structured('order_sent', {
            'ticker': ticker,
            'side': side,
            'action': action,
            'price': price,
            'size': size,
            'orders_sent_total': self.orders_sent
        })
        
    def record_order_acknowledged(self, order_id: str, ticker: str, side: str, action: str, price: float, size: int):
        """Record order acknowledged by exchange"""
        self.orders_acknowledged += 1
        self.log_structured('order_acknowledged', {
            'order_id': order_id,
            'ticker': ticker,
            'side': side,
            'action': action,
            'price': price,
            'size': size,
            'orders_acknowledged_total': self.orders_acknowledged
        })
        
    def record_order_rejected(self, ticker: str, side: str, action: str, price: float, size: int, reason: str):
        """Record order rejected by exchange"""
        self.orders_rejected += 1
        self.log_structured('order_rejected', {
            'ticker': ticker,
            'side': side,
            'action': action,
            'price': price,
            'size': size,
            'reason': reason,
            'orders_rejected_total': self.orders_rejected
        })
        
    def record_order_canceled(self, order_id: str, ticker: str, side: str, price: float, remaining_size: int):
        """Record order cancellation"""
        self.log_structured('order_canceled', {
            'order_id': order_id,
            'ticker': ticker,
            'side': side,
            'price': price,
            'remaining_size': remaining_size
        })
        
    def record_fill(self, order_id: str, ticker: str, side: str, action: str, price: float, size: int, fee: float = 0):
        """Record order fill"""
        fill_data = {
            'timestamp': time.time(),
            'order_id': order_id,
            'ticker': ticker,
            'side': side,
            'action': action,
            'price': price,
            'size': size,
            'fee': fee
        }
        self.fills.append(fill_data)
        self.log_structured('fill', fill_data)
        
    def record_inventory_change(self, ticker: str, old_inventory: int, new_inventory: int, reason: str):
        """Record inventory change"""
        change_data = {
            'timestamp': time.time(),
            'ticker': ticker,
            'old_inventory': old_inventory,
            'new_inventory': new_inventory,
            'change': new_inventory - old_inventory,
            'reason': reason
        }
        self.inventory_changes.append(change_data)
        self.log_structured('inventory_change', change_data)
        
    def record_pnl_snapshot(self, ticker: str, realized_pnl: float, unrealized_pnl: float, inventory: int, position_value: float):
        """Record PnL snapshot"""
        pnl_data = {
            'timestamp': time.time(),
            'ticker': ticker,
            'realized_pnl': round(realized_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'total_pnl': round(realized_pnl + unrealized_pnl, 2),
            'inventory': inventory,
            'position_value': round(position_value, 2)
        }
        self.pnl_snapshots.append(pnl_data)
        self.log_structured('pnl_snapshot', pnl_data)
        
    def record_api_error(self, error_type: str, error_msg: str, endpoint: str = ''):
        """Record API error"""
        self.api_error_count += 1
        error_data = {
            'timestamp': time.time(),
            'error_type': error_type,
            'error_message': error_msg,
            'endpoint': endpoint,
            'total_errors': self.api_error_count
        }
        self.api_errors.append(error_data)
        self.log_structured('api_error', error_data)
        
    def record_quote_latency(self, ticker: str, market_data_ts: float, quote_update_ts: float):
        """Record quote latency (time between market data and quote update)"""
        latency_ms = (quote_update_ts - market_data_ts) * 1000
        self.record_latency(f'quote_update_{ticker}', (quote_update_ts - market_data_ts))
        self.log_structured('quote_latency', {
            'ticker': ticker,
            'latency_ms': round(latency_ms, 2)
        })

    def summarize(self) -> Dict:
        runtime_s = time.time() - self.start_time
        orders_placed = sum(1 for a in self.action_log if a.get("kind") == "place_order")
        orders_canceled = sum(1 for a in self.action_log if a.get("kind") == "cancel_order")
        orders_kept = sum(1 for a in self.action_log if a.get("kind") == "keep_order")
        orders_skipped = sum(1 for a in self.action_log if a.get("kind") == "skip_place")
        last_inventory = self.loop_snapshots[-1]["inventory"] if self.loop_snapshots else 0
        
        # Calculate order success rate
        order_success_rate = (self.orders_acknowledged / self.orders_sent * 100) if self.orders_sent > 0 else 0
        
        # Calculate average quote latency
        quote_latencies = [l['ms'] for l in self.latencies if 'quote_update' in l.get('name', '')]
        avg_quote_latency = sum(quote_latencies) / len(quote_latencies) if quote_latencies else 0
        
        # Calculate total PnL
        total_realized_pnl = sum(s['realized_pnl'] for s in self.pnl_snapshots)
        latest_unrealized_pnl = self.pnl_snapshots[-1]['unrealized_pnl'] if self.pnl_snapshots else 0
        
        return {
            "strategy_name": self.strategy_name,
            "market_ticker": self.market_ticker,
            "runtime_seconds": round(runtime_s, 3),
            "num_iterations": len(self.loop_snapshots),
            "orders_placed": orders_placed,
            "orders_canceled": orders_canceled,
            "orders_kept": orders_kept,
            "orders_skipped": orders_skipped,
            "final_inventory": last_inventory,
            # Enhanced metrics
            "orders_sent": self.orders_sent,
            "orders_acknowledged": self.orders_acknowledged,
            "orders_rejected": self.orders_rejected,
            "order_success_rate_pct": round(order_success_rate, 2),
            "api_errors": self.api_error_count,
            "total_fills": len(self.fills),
            "avg_quote_latency_ms": round(avg_quote_latency, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "latest_unrealized_pnl": round(latest_unrealized_pnl, 2),
            "total_pnl": round(total_realized_pnl + latest_unrealized_pnl, 2),
        }

    def export_files(self, base_prefix: str) -> None:
        try:
            summary = self.summarize()
            payload = {
                "summary": summary,
                "loop_snapshots": self.loop_snapshots,
                "action_log": self.action_log,
                "latencies": self.latencies,
            }
            with open(f"{base_prefix}_metrics.json", "w") as f:
                json.dump(payload, f, indent=2)

            # Loops CSV
            try:
                with open(f"{base_prefix}_loops.csv", "w") as f:
                    f.write("t_seconds,mid_price,inventory,reservation_price,bid_price,ask_price,buy_size,sell_size\n")
                    for s in self.loop_snapshots:
                        f.write(
                            f"{s['t_seconds']},{s['mid_price']},{s['inventory']},{s['reservation_price']},{s['bid_price']},{s['ask_price']},{s['buy_size']},{s['sell_size']}\n"
                        )
            except Exception:
                pass

            # Actions CSV
            try:
                with open(f"{base_prefix}_actions.csv", "w") as f:
                    f.write("ts,kind,order_id,action,side,price,size,reason\n")
                    for a in self.action_log:
                        f.write(
                            f"{a.get('ts','')},{a.get('kind','')},{a.get('order_id','')},{a.get('action','')},{a.get('side','')},{a.get('price','')},{a.get('size','')},{a.get('reason','')}\n"
                        )
            except Exception:
                pass

            # Latencies CSV
            try:
                with open(f"{base_prefix}_latencies.csv", "w") as f:
                    f.write("ts,name,ms\n")
                    for l in self.latencies:
                        f.write(f"{l.get('ts','')},{l.get('name','')},{l.get('ms','')}\n")
            except Exception:
                pass
        except Exception:
            # Avoid raising during shutdown
            pass

class InsufficientBalanceError(Exception):
    """Raised when the exchange returns an insufficient balance error."""
    pass

class AbstractTradingAPI(abc.ABC):
    @abc.abstractmethod
    def get_price(self, ticker: str) -> Dict[str, float]:
        pass

    @abc.abstractmethod
    def get_touch(self, ticker: str) -> Dict[str, Tuple[float, float]]:
        pass

    @abc.abstractmethod
    def place_order(self, ticker: str, action: str, side: str, price: float, quantity: int, expiration_ts: int = None) -> str:
        pass

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass

    @abc.abstractmethod
    def get_position(self, ticker: str) -> int:
        pass

    @abc.abstractmethod
    def get_orders(self, ticker: str) -> List[Dict]:
        pass

class KalshiTradingAPI(AbstractTradingAPI):
    def __init__(
        self,
        email: str,
        password: str,
        base_url: str,
        logger: logging.Logger,
    ):
        self.email = email
        self.password = password
        self.token = None
        self.member_id = None
        self.logger = logger
        self.base_url = base_url
        self.login()

    def login(self):
        self.logger.info("Logging in...")
        config = Configuration(
            username=os.getenv("KALSHI_EMAIL"),
            password=os.getenv("KALSHI_PASSWORD"),
            access_token=os.getenv("KALSHI_API_KEY_ID"),
        )
        with open(os.getenv("KALSHI_PRIVATE_KEY_PATH"), "r") as f:
            private_key = f.read()
        config.api_key_id = os.getenv("KALSHI_API_KEY_ID")
        config.private_key_pem = private_key
        self.client = KalshiClient(config)
        balance = self.client.get_balance()
        self.logger.info(f"Balance: {balance}")
        self.logger.info(f"Client created: {self.client}")

    def logout(self):
        if self.client:
            self.client.logout()
            self.client = None
            self.logger.info("Successfully logged out")

    def get_headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def make_request(
        self, method: str, path: str, params: Dict = None, data: Dict = None
    ):
        url = f"{self.base_url}{path}"
        headers = self.get_headers()

        try:
            response = requests.request(
                method, url, headers=headers, params=params, json=data
            )
            self.logger.debug(f"Request URL: {response.url}")
            self.logger.debug(f"Request headers: {response.request.headers}")
            self.logger.debug(f"Request params: {params}")
            self.logger.debug(f"Request data: {data}")
            self.logger.debug(f"Response status code: {response.status_code}")
            self.logger.debug(f"Response content: {response.text}")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                self.logger.error(f"Response content: {e.response.text}")
            raise

    def get_position(self, ticker: str) -> int:
        try:
            api_key_id = os.getenv("KALSHI_API_KEY_ID")
            private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
            base_url = "https://api.elections.kalshi.com"

            if not api_key_id or not private_key_path:
                self.logger.error("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH")
                return 0

            # Load the private key from file (PEM)
            with open(private_key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)

            def create_signature(private_key_obj, timestamp, method, path):
                # Strip query parameters before signing
                path_without_query = path.split('?')[0]
                message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
                signature_bytes = private_key_obj.sign(
                    message,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                    hashes.SHA256()
                )
                return base64.b64encode(signature_bytes).decode('utf-8')

            timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
            method = "GET"
            path = "/trade-api/v2/portfolio/positions"

            signature = create_signature(private_key, timestamp, method, path)

            headers = {
                'KALSHI-ACCESS-KEY': api_key_id,
                'KALSHI-ACCESS-SIGNATURE': signature,
                'KALSHI-ACCESS-TIMESTAMP': timestamp
            }

            # Pass ticker as a query parameter; do NOT include it in the signature
            params = {"ticker": ticker} if ticker else None
            response = requests.get(base_url + path, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json() or {}

            # Normalize market_positions into a list of dict-like records
            market_positions = None
            if isinstance(data, dict):
                market_positions = data.get('market_positions')
            if market_positions is None:
                market_positions = getattr(data, 'market_positions', None)

            # Coerce to list
            if market_positions is None:
                market_positions_list = []
            elif isinstance(market_positions, list):
                market_positions_list = market_positions
            elif isinstance(market_positions, dict):
                market_positions_list = [market_positions]
            else:
                # Attempt model -> dict
                to_dict_fn = getattr(market_positions, 'to_dict', None)
                if callable(to_dict_fn):
                    try:
                        as_dict = to_dict_fn() or {}
                        inner = as_dict.get('market_positions')
                        if isinstance(inner, list):
                            market_positions_list = inner
                        elif isinstance(inner, dict):
                            market_positions_list = [inner]
                        else:
                            market_positions_list = []
                    except Exception:
                        market_positions_list = []
                else:
                    market_positions_list = []

            # Find the matching ticker and return its integer position
            for item in market_positions_list:
                if isinstance(item, dict):
                    tkr = item.get('ticker')
                    if tkr == ticker:
                        try:
                            return int(item.get('position', 0))
                        except Exception:
                            return 0
                else:
                    tkr = getattr(item, 'ticker', None)
                    if tkr == ticker:
                        try:
                            return int(getattr(item, 'position', 0))
                        except Exception:
                            return 0

            # If not found, default to 0
            return 0
                    
        except Exception as e:
            self.logger.error(f"Failed to get positions: {e}")
            try:
                self.logger.error(getattr(getattr(e, 'response', None), 'text', ''))
            except Exception:
                pass
            return 0

    def get_price(self, ticker: str) -> Dict[str, float]:
        self.logger.info("Retrieving market data for market ticker: " + ticker)
        api_response = self.client.get_market(ticker)
        market_obj = getattr(api_response, "market", None) or {}
        self.logger.info(f"Market object: {getattr(market_obj, 'close_time', None)}")
        yes_bid = float(market_obj.get("yes_bid") if isinstance(market_obj, dict) else getattr(market_obj, "yes_bid", 0)) / 100
        yes_ask = float(market_obj.get("yes_ask") if isinstance(market_obj, dict) else getattr(market_obj, "yes_ask", 0)) / 100
        no_bid = float(market_obj.get("no_bid") if isinstance(market_obj, dict) else getattr(market_obj, "no_bid", 0)) / 100
        no_ask = float(market_obj.get("no_ask") if isinstance(market_obj, dict) else getattr(market_obj, "no_ask", 0)) / 100
        
        yes_mid_price = round((yes_bid + yes_ask) / 2, 2)
        no_mid_price = round((no_bid + no_ask) / 2, 2)

        self.logger.info(f"Current yes mid-market price: ${yes_mid_price:.2f}")
        self.logger.info(f"Current no mid-market price: ${no_mid_price:.2f}")
        return {"yes": yes_mid_price, "no": no_mid_price}

    def get_balance(self):
        try:
            balance = self.client.get_balance()
            if hasattr(balance, "balance"):
                return float(balance.balance)
            elif isinstance(balance, dict):
                return float(balance.get("balance", 0))
            else:
                return 0.0
        except Exception as e:
            self.logger.warning(f"get_balance fallback: {e}")
            return 0.00

    def get_touch(self, ticker: str):
        m = self.client.get_market(ticker).market
        def g(obj, k, default=0):
            return (obj[k] if isinstance(obj, dict) else getattr(obj, k, default)) / 100.0
        yes_bid, yes_ask = g(m, "yes_bid"), g(m, "yes_ask")
        no_bid,  no_ask  = g(m, "no_bid"),  g(m, "no_ask")
        return {"yes": (to_tick(yes_bid) if yes_bid else 0.0,
                        to_tick(yes_ask) if yes_ask else 0.0),
                "no":  (to_tick(no_bid)  if no_bid  else 0.0,
                        to_tick(no_ask)  if no_ask  else 0.0)}

    def get_orderbook(self, market_ticker: str) -> Dict:
        # Normalize to dict: {"var_true": [(price, count), ...], "var_false": [(price, count), ...]}
        result: Dict = {"var_true": [], "var_false": []}

        # Helper to convert a side list to [(price, count)] supporting multiple shapes
        def normalize_side(side_val):
            out = []
            for level in (side_val or []):
                price = None
                count = None
                # Shape A: pair-like [price_cents, count]
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    try:
                        price_cents = float(level[0])
                        count = int(level[1])
                        price = price_cents / 100.0
                    except Exception:
                        price, count = None, None
                # Shape B: dict with price/count (price may be cents or dollars)
                elif isinstance(level, dict):
                    price = level.get("price")
                    count = level.get("count")
                    try:
                        # If price looks like integer cents (1..99), convert; if already dollars, keep
                        if price is not None:
                            price_f = float(price)
                            price = price_f / 100.0 if price_f > 1.0 else price_f
                        if count is not None:
                            count = int(count)
                    except Exception:
                        price, count = None, None
                # Shape C: SDK model with attributes
                else:
                    price = getattr(level, "price", None)
                    count = getattr(level, "count", None)
                    try:
                        if price is not None:
                            price_f = float(price)
                            price = price_f / 100.0 if price_f > 1.0 else price_f
                        if count is not None:
                            count = int(count)
                    except Exception:
                        price, count = None, None

                if price is None or count is None:
                    continue
                try:
                    out.append((round(float(price), 2), int(count)))
                except Exception:
                    continue
            return out

        # Prefer direct REST call (public endpoint); fall back to SDK on failure
        try:
            base = self.base_url or "https://api.elections.kalshi.com/trade-api/v2"
            url = f"{base.rstrip('/')}/markets/{market_ticker}/orderbook"
            resp = requests.get(url, params={"depth": 100}, timeout=5)
            self.logger.debug(f"GET {resp.url} -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json() or {}
            ob = data.get("orderbook") or {}
            # Known public shape uses 'yes' and 'no'
            yes_side = ob.get("yes") or ob.get("var_true") or ob.get("true")
            no_side = ob.get("no") or ob.get("var_false") or ob.get("false")
            result["var_true"] = normalize_side(yes_side)
            result["var_false"] = normalize_side(no_side)
            return result
        except Exception as http_err:
            self.logger.warning(f"REST orderbook fetch failed, falling back to SDK: {http_err}")

        # Fallback: SDK method
        try:
            api_response = self.client.get_market_orderbook(market_ticker)
            ob = getattr(api_response, "orderbook", None)
            if isinstance(ob, dict):
                result["var_true"] = normalize_side(ob.get("var_true") or ob.get("true") or ob.get("yes"))
                result["var_false"] = normalize_side(ob.get("var_false") or ob.get("false") or ob.get("no"))
            else:
                true_side = getattr(ob, "var_true", None)
                false_side = getattr(ob, "var_false", None)
                if true_side is None and false_side is None:
                    to_dict_fn = getattr(ob, "to_dict", None)
                    if callable(to_dict_fn):
                        try:
                            as_dict = to_dict_fn()
                            true_side = as_dict.get("true") or as_dict.get("yes")
                            false_side = as_dict.get("false") or as_dict.get("no")
                        except Exception:
                            pass
                result["var_true"] = normalize_side(true_side)
                result["var_false"] = normalize_side(false_side)
            return result
        except Exception as sdk_err:
            self.logger.error(f"Failed to retrieve orderbook via SDK: {sdk_err}")
            return result

    def get_markets(self) -> List[Dict]:
        self.logger.info("Retrieving markets...")
        try:
            cursor = None
            markets: List[Dict] = []
            while True:
                api_response = self.client.get_markets(status='open', cursor=cursor)
                if not api_response:
                    break

                current_markets = getattr(api_response, "markets", None)

                if current_markets:
                    if isinstance(current_markets, list):
                        markets.extend(current_markets)
                    else:
                        markets.append(current_markets)

                cursor = getattr(api_response, "cursor", None)
                if not cursor:
                    break
            # Persist markets to a JSON file for offline inspection
            try:
                with open("markets.json", "w") as f:
                    json.dump(markets, f, indent=2, default=str)
                self.logger.info(f"Wrote {len(markets)} markets to markets.json")
            except Exception as write_error:
                self.logger.error(f"Failed to write markets.json: {write_error}")
            return markets
        except Exception as e:
            self.logger.error(f"Failed to retrieve markets: {e}")
            return []

    def get_markets_by_event(self, event_ticker: str, status: str = 'open') -> List[Dict]:
        self.logger.info(f"Retrieving markets for event {event_ticker}...")
        markets: List[Dict] = []
        try:
            cursor = None
            while True:
                try:
                    api_response = self.client.get_markets(event_ticker=event_ticker, status=status, cursor=cursor)
                except TypeError:
                    # SDK may not support event_ticker filter; fall back to fetching and filtering locally
                    self.logger.warning("SDK get_markets does not accept event_ticker; falling back to local filter")
                    all_markets = self.get_markets()
                    filtered: List[Dict] = []
                    for m in all_markets:
                        et = m.get('event_ticker') if isinstance(m, dict) else getattr(m, 'event_ticker', None)
                        if et == event_ticker:
                            filtered.append(m)
                    self.logger.info(f"Filtered {len(filtered)} markets for event {event_ticker}")
                    return filtered

                if not api_response:
                    break

                current_markets = getattr(api_response, 'markets', None)
                if current_markets:
                    if isinstance(current_markets, list):
                        markets.extend(current_markets)
                    else:
                        markets.append(current_markets)

                cursor = getattr(api_response, 'cursor', None)
                if not cursor:
                    break

            # Normalize to list of dicts
            normalized: List[Dict] = []
            for item in markets:
                if isinstance(item, dict):
                    normalized.append(item)
                    continue
                to_dict_fn = getattr(item, 'to_dict', None)
                if callable(to_dict_fn):
                    try:
                        normalized.append(to_dict_fn())
                        continue
                    except Exception:
                        pass
                model_dump_fn = getattr(item, 'model_dump', None)
                if callable(model_dump_fn):
                    try:
                        normalized.append(model_dump_fn(by_alias=True, exclude_none=True))
                        continue
                    except Exception:
                        pass
                # Fallback minimal fields
                candidate: Dict = {}
                for field in [
                    'ticker', 'event_ticker', 'series_ticker', 'yes_bid', 'yes_ask', 'no_bid', 'no_ask', 'status',
                ]:
                    if hasattr(item, field):
                        candidate[field] = getattr(item, field)
                normalized.append(candidate)

            self.logger.info(f"Retrieved {len(normalized)} markets for event {event_ticker}")
            try:
                with open("markets.json", "w") as f:
                    json.dump(normalized, f, indent=2, default=str)
            except Exception:
                pass
            return normalized
        except Exception as e:
            self.logger.error(f"Failed to retrieve markets for event {event_ticker}: {e}")
            return []

    def get_series(self) -> List[Dict]:
        self.logger.info("Retrieving series...")
        try:
            if not hasattr(self.client, "get_series"):
                self.logger.error("KalshiClient has no method get_series")
                return []

            api_response = self.client.get_series(status='open')
            current_series = getattr(api_response, "series", None)
            if not current_series:
                current_series = []

            for item in current_series:
                self.logger.info(f"Series: {item}")

            try:
                with open("series.json", "w") as f:
                    json.dump(current_series, f, indent=2, default=str)
                self.logger.info(f"Wrote {len(current_series)} series to series.json")
            except Exception as write_error:
                self.logger.error(f"Failed to write series.json: {write_error}")

            self.logger.info(f"Retrieved {len(current_series)} total series")
            return current_series
        except Exception as e:
            self.logger.error(f"Failed to retrieve series: {e}")
            return []

    def place_order(self, ticker: str, action: str, side: str, price: float, quantity: int, expiration_ts: int = None) -> str:
        self.logger.info(f"Placing {action} order for {side} side at price ${price:.2f} with quantity {quantity}...")
        path = "/portfolio/orders"
        data = {
            "ticker": ticker,
            "action": action.lower(),  # 'buy' or 'sell'
            "type": "limit",
            "side": side,  # 'yes' or 'no'
            "count": quantity,
            "client_order_id": str(uuid.uuid4()),
        }

        price_to_send = max(1, min(99, int(to_cents(price)))) # Convert dollars to cents

        if side == "yes":
            data["yes_price"] = price_to_send
        else:
            data["no_price"] = price_to_send

        if expiration_ts is not None:
            data["expiration_ts"] = expiration_ts

        self.logger.info(f"Data: {data}")
        try:
            # SDK constructs CreateOrderRequest(**kwargs) internally
            response = self.client.create_order(**data)
            # Handle response as model or dict
            order = getattr(response, "order", None)
            if order is None and isinstance(response, dict):
                order = response.get("order")
            order_id = getattr(order, "order_id", None) if order is not None else None
            if order_id is None and isinstance(order, dict):
                order_id = order.get("order_id")
            self.logger.info(f"Placed {action} order for {side} side at price ${price:.2f} with quantity {quantity}, order ID: {order_id}")
            return str(order_id)
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            # Try to detect insufficient balance to allow caller-side handling
            try:
                resp_text = getattr(getattr(e, 'response', None), 'text', '') or ''
            except Exception:
                resp_text = ''
            err_blob = f"{e} {resp_text}".lower()
            if 'insufficient_balance' in err_blob or 'insufficient balance' in err_blob:
                self.logger.error("Detected insufficient balance from exchange response")
                self.logger.error(f"Request data: {data}")
                raise InsufficientBalanceError("insufficient_balance")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response content: {e.response.text}")
            self.logger.error(f"Request data: {data}")
            raise

    def cancel_order(self, order_id: int) -> bool:
        self.logger.info(f"Canceling order with ID {order_id}...")
        try:
            self.client.cancel_order(order_id)
            self.logger.info(f"Canceled order with ID {order_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def get_liq_markets(self) -> List[Dict]:
        self.logger.info("Retrieving liquid markets with pagination...")
        url = "https://api.elections.kalshi.com/trade-api/v2/incentive_programs"
        cursor: Optional[str] = None
        all_items: List[Dict] = []

        while True:
            params = {}
            params["type"] = "liquidity"
            params["status"] = "active"
            params["limit"] = 100000
            if cursor:
                params["cursor"] = cursor
       

            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json() or {}

            page_items = data.get("incentive_programs")
            if isinstance(page_items, list):
                all_items.extend(page_items)
                self.logger.info(f"Accumulated {len(all_items)} liquid markets so far")

            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
            cursor = next_cursor
        self.logger.info(f"Retrieved {len(all_items)} liquid markets in total")
        return all_items

    def get_orders(self, ticker: str) -> List[Dict]:
        self.logger.info("Retrieving orders...")
        api_response = self.client.get_orders(ticker=ticker, status="resting")
        raw_orders = getattr(api_response, "orders", None)
        if raw_orders is None:
            raw_orders = api_response.get("orders", []) if isinstance(api_response, dict) else []

        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        def _price_to_float(val) -> Optional[float]:
            if val is None:
                return None
            try:
                f = float(val)
                # if value looks like cents (>= 1), convert to dollars 0.01..0.99
                return round(f / 100.0, 2) if f > 1.0 else round(f, 2)
            except Exception:
                return None

        def _to_int(val, default: int = 0) -> int:
            try:
                if val is None:
                    return default
                return int(val)
            except Exception:
                return default

        normalized_orders: List[Dict] = []
        for item in (raw_orders or []):
            # Normalize source to a dict first
            src: Dict = {}
            if isinstance(item, dict):
                src = item
            else:
                to_dict_fn = getattr(item, "to_dict", None)
                if callable(to_dict_fn):
                    try:
                        src = to_dict_fn() or {}
                    except Exception:
                        src = {}
                if not src:
                    model_dump_fn = getattr(item, "model_dump", None)
                    if callable(model_dump_fn):
                        try:
                            src = model_dump_fn(by_alias=True, exclude_none=True) or {}
                        except Exception:
                            src = {}

            # Build normalized order matching required schema
            yes_px_float = _price_to_float(_get(src, "yes_price"))
            no_px_float = _price_to_float(_get(src, "no_price"))
            count_val = _to_int(_get(src, "count"), 0)
            remaining_val = _to_int(_get(src, "remaining_count"), 0)
            initial_val = _get(src, "initial_count")
            initial_val = _to_int(initial_val if initial_val is not None else count_val, 0)
            fill_count_val = _get(src, "fill_count")
            if fill_count_val is None:
                fill_count_val = max(0, initial_val - remaining_val)
            else:
                fill_count_val = _to_int(fill_count_val, max(0, initial_val - remaining_val))

            taker_fees_val = _to_int(_get(src, "taker_fees"), 0)
            maker_fees_val = _to_int(_get(src, "maker_fees"), 0)

            normalized = {
                "order_id": _get(src, "order_id"),
                "client_order_id": _get(src, "client_order_id"),
                "ticker": _get(src, "ticker", ticker),
                "side": _get(src, "side"),
                "action": _get(src, "action"),
                "type": _get(src, "type"),
                "status": _get(src, "status"),
                "yes_price": yes_px_float,
                "no_price": no_px_float,
                "count": count_val,
                "fill_count": fill_count_val,
                "remaining_count": remaining_val,
                "initial_count": initial_val,
                "taker_fees": taker_fees_val,
                "maker_fees": maker_fees_val,
                "expiration_time": _get(src, "expiration_time"),
                "created_time": _get(src, "created_time"),
                "updated_time": _get(src, "updated_time"),
            }

            normalized_orders.append(normalized)

        self.logger.info(f"Retrieved {len(normalized_orders)} orders")
        return normalized_orders

    def get_all_orders(self) -> List[Dict]:
        """Retrieve all resting orders across all tickers and normalize shape."""
        self.logger.info("Retrieving ALL resting orders...")
        try:
            api_response = self.client.get_orders(status="resting")
        except TypeError:
            # SDK may require explicit None for ticker
            api_response = self.client.get_orders(ticker=None, status="resting")

        raw_orders = getattr(api_response, "orders", None)
        if raw_orders is None:
            raw_orders = api_response.get("orders", []) if isinstance(api_response, dict) else []

        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        def _price_to_float(val) -> Optional[float]:
            if val is None:
                return None
            try:
                f = float(val)
                return round(f / 100.0, 2) if f > 1.0 else round(f, 2)
            except Exception:
                return None

        def _to_int(val, default: int = 0) -> int:
            try:
                if val is None:
                    return default
                return int(val)
            except Exception:
                return default

        normalized_orders: List[Dict] = []
        for item in (raw_orders or []):
            src: Dict = {}
            if isinstance(item, dict):
                src = item
            else:
                to_dict_fn = getattr(item, "to_dict", None)
                if callable(to_dict_fn):
                    try:
                        src = to_dict_fn() or {}
                    except Exception:
                        src = {}
                if not src:
                    model_dump_fn = getattr(item, "model_dump", None)
                    if callable(model_dump_fn):
                        try:
                            src = model_dump_fn(by_alias=True, exclude_none=True) or {}
                        except Exception:
                            src = {}

            yes_px_float = _price_to_float(_get(src, "yes_price"))
            no_px_float = _price_to_float(_get(src, "no_price"))
            count_val = _to_int(_get(src, "count"), 0)
            remaining_val = _to_int(_get(src, "remaining_count"), 0)
            initial_val = _get(src, "initial_count")
            initial_val = _to_int(initial_val if initial_val is not None else count_val, 0)
            fill_count_val = _get(src, "fill_count")
            if fill_count_val is None:
                fill_count_val = max(0, initial_val - remaining_val)
            else:
                fill_count_val = _to_int(fill_count_val, max(0, initial_val - remaining_val))

            taker_fees_val = _to_int(_get(src, "taker_fees"), 0)
            maker_fees_val = _to_int(_get(src, "maker_fees"), 0)

            normalized = {
                "order_id": _get(src, "order_id"),
                "client_order_id": _get(src, "client_order_id"),
                "ticker": _get(src, "ticker"),
                "side": _get(src, "side"),
                "action": _get(src, "action"),
                "type": _get(src, "type"),
                "status": _get(src, "status"),
                "yes_price": yes_px_float,
                "no_price": no_px_float,
                "count": count_val,
                "fill_count": fill_count_val,
                "remaining_count": remaining_val,
                "initial_count": initial_val,
                "taker_fees": taker_fees_val,
                "maker_fees": maker_fees_val,
                "expiration_time": _get(src, "expiration_time"),
                "created_time": _get(src, "created_time"),
                "updated_time": _get(src, "updated_time"),
            }

            normalized_orders.append(normalized)

        self.logger.info(f"Retrieved {len(normalized_orders)} total resting orders")
        return normalized_orders

    def get_valid_markets(self) -> List[Dict]:
        liq_markets = self.get_liq_markets()
        valid_markets: List[Dict] = []

        def analyze_side(side: List[Tuple[float, int]], target: int, book_side: str):
            """
            side: list of (price, size) levels for the side you're *hitting*
            target: desired quantity to take
            book_side: "bid" if this side is bids (so sort high→low), else "ask" (low→high)

            Returns:
            best_price
            amount_needed   (positive qty needed to hit target)
            best_size       (size of the best price level)
            coverage        (min(best_size, target) / target)
            """
            if not side or target <= 0:
                return (None, 0, 0, 0.0)

            # Aggregate sizes per price
            levels = defaultdict(int)
            for price, size in side:
                if size > 0:
                    levels[price] += size

            if not levels:
                return (None, 0, 0, 0.0)

            # Sort: bids high→low, asks low→high
            reverse = (book_side == "bid")
            ordered = sorted(levels.items(), key=lambda x: x[0], reverse=reverse)

            best_price, best_size = ordered[0]

            amount_needed = max(0, target - best_size)
            coverage = min(best_size, target) / target if target > 0 else 0.0

            return best_price, amount_needed, best_size, coverage

        mkts_checked: int = 0
        for market in liq_markets:
            mkts_checked += 1
            ticker = market.get("market_ticker")
            if not ticker:
                self.logger.info(f"No ticker for market: {market}")
                continue
            orderbook = self.get_orderbook(ticker)
            var_true = orderbook.get("var_true", [])
            var_false = orderbook.get("var_false", [])

            if not var_true and not var_false:
                self.logger.info(f"{ticker}: empty orderbook (both sides empty); skipping")
                continue

            target_size = int(market.get("target_size", 300))
            # Compute spread if both sides exist
            spread_val: Optional[float] = None
            if var_true and var_false:
                try:
                    # Best ask for YES is the minimum price on var_true; best bid for NO is the maximum on var_false
                    best_yes_tmp = min(p for p, _ in var_true)
                    best_no_tmp = max(p for p, _ in var_false)
                    spread_val = round(best_no_tmp - best_yes_tmp, 4)
                except Exception:
                    spread_val = None

            # YES side entry
            if var_true:
                (best_yes, amount_needed_yes, best_size_yes, cov_yes) = analyze_side(var_true, target_size, "ask")
                if amount_needed_yes > 0:
                    entry_yes: Dict = {
                        "ticker": ticker,
                        "side": "yes",
                        "target_size": target_size,
                        "best_price": best_yes,
                        "amount_needed": amount_needed_yes,
                        "best_size": best_size_yes,
                        "coverage": round(cov_yes, 3),
                        "spread": spread_val if spread_val is not None else 0.0,
                        "valid_for_entry": amount_needed_yes > 0,
                        "discount_factor_bps": market.get("discount_factor_bps", 0),
                        "period_reward": market.get("period_reward", 0),
                        "start_date": market.get("start_date", 0),
                        "end_date": market.get("end_date", 0),
                    }
                    entry_yes["score"] = score_side("yes", entry_yes)
                    valid_markets.append(entry_yes)

            # NO side entry
            if var_false:
                (best_no, amount_needed_no, best_size_no, cov_no) = analyze_side(var_false, target_size, "bid")
                if amount_needed_no > 0:
                    entry_no: Dict = {
                        "ticker": ticker,
                        "side": "no",
                        "target_size": target_size,
                        "best_price": best_no,
                        "amount_needed": amount_needed_no,
                        "best_size": best_size_no,
                        "coverage": round(cov_no, 3),
                        "spread": spread_val if spread_val is not None else 0.0,
                        "valid_for_entry": amount_needed_no > 0,
                        "discount_factor_bps": market.get("discount_factor_bps", 0),
                        "period_reward": market.get("period_reward", 0),
                        "start_date": market.get("start_date", 0),
                        "end_date": market.get("end_date", 0),
                    }
                    entry_no["score"] = score_side("no", entry_no)
                    valid_markets.append(entry_no)

        self.logger.info(f"Markets checked: {mkts_checked}")
        self.logger.info(f"Valid markets: {len(valid_markets)}")
        return valid_markets
            

def _gauss(x: float, mu: float, sigma: float) -> float:
    # 0..1 bump centered at mu
    if sigma <= 0:
        return 0.0
    return math.exp(-0.5 * ((x - mu) / sigma) ** 2)

def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))

def score_side(side: str, entry: Dict) -> int:
    """
    Score a single side of a market as its own entity.
    Expects keys on entry: coverage, spread, target_size, best_size,
    discount_factor_bps or discount_factor, and period_reward or reward_pool.
    Also favors programs that end later via an exponential time-to-end factor.
    Returns an integer score (0..1000).
    """

    coverage = float(entry.get("coverage", 0.0))
    spread = float(entry.get("spread", 0.0) or 0.0)
    target = max(1, int(entry.get("target_size", 300)))

    # Discount factor normalization (0..1)
    if "discount_factor" in entry:
        df = float(entry.get("discount_factor", 0.5))
    else:
        df_bps = float(entry.get("discount_factor_bps", 0))
        df = _clip01(df_bps / 10000.0) if df_bps else 0.5

    reward_pool = float(entry.get("period_reward", entry.get("reward_pool", 50.0)))
    best_cap = float(entry.get("best_size", 0.0))

    # Time-to-end factor (higher when end_date is later)
    end_raw = entry.get("end_date", None)
    time_score = 1.0
    try:
        end_ts = float(end_raw) if end_raw is not None else None
    except Exception:
        end_ts = None
    if end_ts and end_ts > 0:
        if end_ts > 1e12:
            end_ts = end_ts / 1000.0
        days_remaining = max(0.0, (end_ts - time.time()) / 86400.0)
        tau_days = 30.0
        # Increase with more days remaining; near 0 for soon, approaches 1 for far
        time_score = _clip01(1.0 - math.exp(-days_remaining / tau_days))

    # Monotonic component scores in 0..1
    cov_score = _clip01(coverage)  # higher coverage is better
    df_score = _clip01(df) ** 1.15  # prefer higher discount factor
    spread_score = _clip01(spread / 0.20)  # reward wider spreads up to ~20c
    cap_norm = _clip01(best_cap / target)  # more liquidity at best is better
    rew_score = _clip01(1.0 - math.exp(-reward_pool / 120.0))  # larger reward pools are better

    # Weighted multiplicative blend (keeps 0..1 and monotonic)
    # Give time-to-end a bit more weight than others
    w_cov, w_df, w_spread, w_cap, w_rew, w_time = 1.0, 1.0, 0.9, 1.2, 1.0, 1.5
    comp = (
        (cov_score ** w_cov)
        * (df_score ** w_df)
        * (spread_score ** w_spread)
        * (cap_norm ** w_cap)
        * (rew_score ** w_rew)
        * (time_score ** w_time)
    )

    return int(round(1000 * _clip01(comp)))


class LIPBot:
    def __init__(
        self,
        logger: logging.Logger,
        api: AbstractTradingAPI,
        max_position: int,
        position_limit_buffer: float = 0.1,
        inventory_skew_factor: float = 0.01,
        improve_once_per_touch: bool = True,
        improve_cooldown_seconds: int = 0,
        min_quote_width_cents: int = 0,
        stop_event: Optional[threading.Event] = None,
        max_consecutive_errors: int = 10,
        pnl_threshold: float = -100.0,
        max_inventory_imbalance: float = 0.8,
    ):
        self.api = api
        self.logger = logger
        self.max_position = max_position
        self.position_limit_buffer = position_limit_buffer
        self.inventory_skew_factor = inventory_skew_factor
        self.improve_once_per_touch = bool(improve_once_per_touch)
        self.improve_cooldown_seconds = int(improve_cooldown_seconds)
        self.min_quote_width = max(0.0, float(min_quote_width_cents or 0) / 100.0)
        self.stop_event = stop_event
        
        # Monitoring and safety systems
        self.alert_manager = AlertManager(logger)
        self.circuit_breaker = CircuitBreaker(
            max_consecutive_errors=max_consecutive_errors,
            pnl_threshold=pnl_threshold,
            max_inventory_imbalance=max_inventory_imbalance,
            logger=logger,
            alert_manager=self.alert_manager
        )
        
        # metrics
        self.metrics: Optional[MetricsTracker] = None
        
        # Track positions for PnL calculation
        self.position_tracker: Dict[str, Dict] = {}  # ticker -> {inventory, cost_basis, realized_pnl}
        
        # improvement gating state
        self._last_external_touch: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}
        self._improved_on_touch: Dict[Tuple[str, str], bool] = {}
        self._last_improve_ts: Dict[Tuple[str, str], float] = {}
        
        # Send startup alert
        self.alert_manager.send_alert(AlertLevel.INFO, "bot_lifecycle", "Market maker bot starting up", {})

    def run(self, dt: float):
        start_time = time.time()
        print(f"Starting LIPBot")
        if self.metrics is None:
            strategy_name = getattr(self.logger, 'name', 'Strategy')
            self.metrics = MetricsTracker(strategy_name=strategy_name, market_ticker=None)
            
            # Track markets we have activity in
            tracked_markets: Dict[str, Dict[str, bool]] = {}
            last_discovery_ts: float = 0.0
            last_pnl_check_ts: float = 0.0

            while not self._should_stop():
                loop_start = time.time()
                
                # Check circuit breaker before proceeding
                if not self.circuit_breaker.is_trading_allowed():
                    self.logger.critical("Circuit breaker is tripped. Trading halted.")
                    self.alert_manager.send_alert(
                        AlertLevel.CRITICAL,
                        "circuit_breaker",
                        "Trading halted due to circuit breaker",
                        self.circuit_breaker.get_status()
                    )
                    break

                # 1) Always fetch ALL open orders and monitor them
                try:
                    open_orders = []
                    if hasattr(self.api, 'get_all_orders'):
                        open_orders = self.api.get_all_orders() or []
                    self.circuit_breaker.record_success()  # Successful API call
                except Exception as e:
                    self.logger.warning(f"Failed to fetch all orders: {e}")
                    self.metrics.record_api_error("get_all_orders", str(e), "get_all_orders")
                    self.circuit_breaker.record_error("get_all_orders", str(e))
                    open_orders = []

                # Group open orders by ticker and sides
                orders_by_ticker: Dict[str, List[Dict]] = defaultdict(list)
                for o in open_orders:
                    tkr = o.get('ticker')
                    if tkr:
                        orders_by_ticker[tkr].append(o)

                # Ensure tracked_markets includes any tickers with open orders
                for tkr in orders_by_ticker.keys():
                    tracked_markets.setdefault(tkr, {})
                    for s in {o.get('side') for o in orders_by_ticker[tkr] if o.get('side')}:
                        tracked_markets[tkr][s] = True

                # For each tracked ticker/side, compute quotes and manage orders
                for ticker in list(tracked_markets.keys()):
                    try:
                        touch = self.api.get_touch(ticker)
                    except Exception as e:
                        self.logger.warning(f"Failed to get touch for {ticker}: {e}")
                        continue

                    try:
                        inventory = self.api.get_position(ticker)
                    except Exception as e:
                        self.logger.warning(f"Failed to get position for {ticker}: {e}")
                        inventory = 0

                    # manage per side we are tracking (or have orders on)
                    for side in list(tracked_markets.get(ticker, {}).keys()):
                        side_touch = touch.get(side)
                        if not side_touch:
                            continue
                        mkt_bid, mkt_ask = side_touch
                        spread = max(0.0, (mkt_ask - mkt_bid))
                        # determine if best touch is ours (exclude our quotes for external-change detection)
                        side_orders = [o for o in orders_by_ticker.get(ticker, []) if o.get('side') == side]
                        def _px(o):
                            raw = o.get("yes_price") if side == "yes" else o.get("no_price")
                            f = float(raw)
                            return to_tick(f/100.0 if f > 1.0 else f)
                        our_best_buy = None
                        our_best_sell = None
                        for o in side_orders:
                            act = o.get('action')
                            try:
                                p = _px(o)
                            except Exception:
                                continue
                            if act == 'buy':
                                our_best_buy = max(our_best_buy, p) if our_best_buy is not None else p
                            elif act == 'sell':
                                our_best_sell = min(our_best_sell, p) if our_best_sell is not None else p

                        ext_bid = None if (our_best_buy is not None and to_tick(mkt_bid) == our_best_buy) else to_tick(mkt_bid)
                        ext_ask = None if (our_best_sell is not None and to_tick(mkt_ask) == our_best_sell) else to_tick(mkt_ask)
                        key = (ticker, side)
                        last_ext = self._last_external_touch.get(key)
                        external_changed = (last_ext != (ext_bid, ext_ask))
                        if external_changed:
                            self._improved_on_touch[key] = False
                        now_ts = time.time()
                        cooldown_ok = (self.improve_cooldown_seconds <= 0) or (now_ts - self._last_improve_ts.get(key, 0.0) >= self.improve_cooldown_seconds)
                        allow_improvement = True
                        if self.improve_once_per_touch:
                            allow_improvement = (not self._improved_on_touch.get(key, False)) and cooldown_ok

                        bid, ask = self.compute_quotes(mkt_bid, mkt_ask, inventory, allow_improvement=allow_improvement, min_width=self.min_quote_width)
                        self.manage_orders(bid, ask, spread, ticker, inventory, side)
                        # update gating state
                        self._last_external_touch[key] = (ext_bid, ext_ask)
                        if allow_improvement and inventory == 0 and spread >= 0.02:
                            self._improved_on_touch[key] = True
                            self._last_improve_ts[key] = now_ts

                # 2) Periodically discover new markets to enter
                if (time.time() - last_discovery_ts) >= max(1.0, dt):
                    try:
                        valid_markets = self.api.get_valid_markets() or []
                        valid_markets.sort(key=lambda x: x.get('score', 0), reverse=True)
                    except Exception as e:
                        self.logger.warning(f"Market discovery failed: {e}")
                        valid_markets = []

                    # Try the top few candidates not currently tracked
                    for entry in valid_markets[:5]:
                        tkr = entry.get('ticker')
                        side = entry.get('side')
                        if not tkr or not side:
                            continue
                        if tkr in orders_by_ticker:
                            continue  # already have orders
                        # attempt to start managing this market/side
                        try:
                            touch = self.api.get_touch(tkr)
                            if side not in touch:
                                continue
                            mkt_bid, mkt_ask = touch[side]
                            inventory = self.api.get_position(tkr)
                            spread = max(0.0, (mkt_ask - mkt_bid))
                            # no orders yet; treat external touch as the live touch
                            key = (tkr, side)
                            self._last_external_touch.setdefault(key, (to_tick(mkt_bid), to_tick(mkt_ask)))
                            self._improved_on_touch.setdefault(key, False)
                            now_ts = time.time()
                            cooldown_ok = (self.improve_cooldown_seconds <= 0) or (now_ts - self._last_improve_ts.get(key, 0.0) >= self.improve_cooldown_seconds)
                            allow_improvement = True
                            if self.improve_once_per_touch:
                                allow_improvement = (not self._improved_on_touch.get(key, False)) and cooldown_ok
                            bid, ask = self.compute_quotes(mkt_bid, mkt_ask, inventory, allow_improvement=allow_improvement, min_width=self.min_quote_width)
                            self.manage_orders(bid, ask, spread, tkr, inventory, side)
                            if allow_improvement and inventory == 0 and spread >= 0.02:
                                self._improved_on_touch[key] = True
                                self._last_improve_ts[key] = now_ts
                            tracked_markets.setdefault(tkr, {})[side] = True
                            self.logger.info(f"Started tracking market {tkr} [{side.upper()}]")
                        except Exception as e:
                            self.logger.warning(f"Failed to initialize market {tkr}: {e}")

                    last_discovery_ts = time.time()

                # 3) Periodically check PnL and inventory imbalance
                if (time.time() - last_pnl_check_ts) >= 60.0:  # Check every minute
                    try:
                        total_pnl = self._calculate_total_pnl(tracked_markets)
                        self.circuit_breaker.check_pnl(total_pnl)
                        
                        # Check inventory imbalance across all tracked markets
                        for ticker in tracked_markets.keys():
                            try:
                                inventory = self.api.get_position(ticker)
                                self.circuit_breaker.check_inventory_imbalance(inventory, self.max_position)
                                
                                # Alert on high inventory imbalance (but don't trip circuit yet)
                                if self.max_position > 0 and abs(inventory) / self.max_position > 0.5:
                                    self.alert_manager.send_alert(
                                        AlertLevel.WARNING,
                                        "inventory_imbalance",
                                        f"High inventory imbalance in {ticker}",
                                        {
                                            'ticker': ticker,
                                            'inventory': inventory,
                                            'max_position': self.max_position,
                                            'imbalance_pct': abs(inventory) / self.max_position * 100
                                        }
                                    )
                            except Exception as e:
                                self.logger.warning(f"Failed to check inventory for {ticker}: {e}")
                                
                        last_pnl_check_ts = time.time()
                    except Exception as e:
                        self.logger.warning(f"Failed to check PnL: {e}")

                # pacing
                elapsed = time.time() - loop_start
                sleep_for = max(0.0, dt - elapsed)
                if sleep_for > 0:
                    time.sleep(sleep_for)

        self.logger.info("LIPBot finished running")
        self.alert_manager.send_alert(AlertLevel.INFO, "bot_lifecycle", "Market maker bot shutting down", {})

    def _record_lip_loop(self, t_seconds: float, touch_bid: float, touch_ask: float,
                          inventory: int, bid: float, ask: float) -> None:
        if self.metrics is None:
            return
        self.metrics.record_action("lip_loop", {
            "t_seconds": round(t_seconds, 3),
            "touch_bid": round(touch_bid, 2),
            "touch_ask": round(touch_ask, 2),
            "inventory": int(inventory),
            "bid": round(bid, 2),
            "ask": round(ask, 2),
        })

    def _should_stop(self) -> bool:
        return bool(self.stop_event.is_set()) if self.stop_event is not None else False
        
    def _calculate_total_pnl(self, tracked_markets: Dict[str, Dict[str, bool]]) -> float:
        """Calculate total PnL across all tracked markets"""
        total_pnl = 0.0
        
        for ticker in tracked_markets.keys():
            try:
                # Get current position
                inventory = self.api.get_position(ticker)
                
                # Initialize position tracker if needed
                if ticker not in self.position_tracker:
                    self.position_tracker[ticker] = {
                        'inventory': inventory,
                        'cost_basis': 0.0,
                        'realized_pnl': 0.0
                    }
                
                # Get current market price
                try:
                    prices = self.api.get_price(ticker)
                    # Use yes mid price as market value
                    current_price = prices.get('yes', 0.5)
                except Exception:
                    current_price = 0.5
                
                # Calculate unrealized PnL (simplified)
                position_value = inventory * current_price
                unrealized_pnl = position_value - self.position_tracker[ticker]['cost_basis']
                realized_pnl = self.position_tracker[ticker]['realized_pnl']
                
                # Record PnL snapshot
                if self.metrics:
                    self.metrics.record_pnl_snapshot(
                        ticker=ticker,
                        realized_pnl=realized_pnl,
                        unrealized_pnl=unrealized_pnl,
                        inventory=inventory,
                        position_value=position_value
                    )
                
                total_pnl += (realized_pnl + unrealized_pnl)
                
            except Exception as e:
                self.logger.warning(f"Failed to calculate PnL for {ticker}: {e}")
                
        return total_pnl

    def export_metrics(self) -> None:
        if self.metrics is None:
            return
        # Safe file prefix based on logger name
        strategy_name = getattr(self.logger, 'name', 'Strategy')
        safe_name = strategy_name.replace(':', '_').replace(' ', '_')
        base_prefix = f"{safe_name}"
        self.metrics.export_files(base_prefix)

    def compute_quotes(self, touch_bid, touch_ask, inventory, theta=0.005, allow_improvement: bool = True, min_width: float = 0.0):
        tick = 0.01
        spread = touch_ask - touch_bid

        # always anchor to top of book
        bid = touch_bid
        ask = touch_ask

        # lean away from your inventory
        skew = inventory * theta * spread

        bid = to_tick(max(0.01, bid - skew))
        ask = to_tick(min(0.99, ask + skew))

        # optional: nudge 1 tick better if spread >= 0.02, only when flat inventory
        if allow_improvement and inventory == 0 and spread >= 2 * tick:
            bid = min(to_tick(bid + tick), to_tick(ask - tick))
            ask = max(to_tick(ask - tick), to_tick(bid + tick))

        # enforce optional minimum width
        if min_width and (ask - bid) < min_width:
            deficit = min_width - (ask - bid)
            half = deficit / 2.0
            bid = to_tick(max(0.01, bid - half))
            ask = to_tick(min(0.99, ask + half))
            if ask <= bid:
                ask = to_tick(min(0.99, bid + tick))

        return bid, ask

    def compute_desired_size(self, side: str, action: str, price: float, spread: float, inventory: int, min_order_size: int = 1) -> int:
        """
        Compute desired order size using three components:
        - Capacity scaling: shrink size as inventory approaches max_position.
        - Spread scaling: grow size as market spread widens (linear vs 2¢ anchor).
        - Base size: anchor sizing to a fraction of max_position.

        Enforces the exchange minimum size and caps by remaining capacity.
        """
        remaining_capacity = max(0, self.max_position - abs(inventory))
        inv_factor = (remaining_capacity / self.max_position) if self.max_position else 0.0
        spread_factor = 1 + (spread / 0.02)  # +100% size if spread ≥ 2¢
        base_size = int(self.max_position * 0.2 * inv_factor * spread_factor)

        balance_cap = self.max_affordable_size(side, action, price,
                                           balance_reserve_frac=float(os.getenv("LIP_RESERVE_FRAC", "0.9")),
                                           per_market_budget_frac=float(os.getenv("LIP_MARKET_FRAC", "0.25")),
                                           fee_per_contract=float(os.getenv("LIP_FEE_PER_CONTRACT", "0.00")))

        desired = min(remaining_capacity, balance_cap, max(min_order_size, base_size))
        return desired

    def max_affordable_size(self, side: str, action: str, price: float,
                        balance_reserve_frac: float = 0.9,
                        per_market_budget_frac: float = 0.25,
                        fee_per_contract: float = 0.00) -> int:
        avail = self.get_available_cash()
        # keep a cash buffer
        spendable = max(0.0, avail * (1.0 - balance_reserve_frac))
        # also cap by per-market slice of total spendable
        market_cap = spendable * per_market_budget_frac

        # unit capital per contract
        unit = self.order_capital_required(side, action, price, 1, fee_per_contract)
        if unit <= 0:
            return 0
        return int(market_cap // unit)


    def get_available_cash(self) -> float:
        """Return available cash for sizing decisions; falls back to 0.0 on error."""
        try:
            get_bal = getattr(self.api, "get_balance", None)
            if callable(get_bal):
                val = get_bal()
                return float(val) if val is not None else 0.0
        except Exception as e:
            self.logger.warning(f"get_available_cash fallback: {e}")
        return 0.0

    def order_capital_required(self, side: str, action: str, price: float, size: int,
        fee_per_contract: float = 0.00) -> float:
        # price is 0.01..0.99 dollars
        if action == "buy":
            unit = price if side == "yes" else (1.0 - price)
        else:  # sell
            unit = (1.0 - price) if side == "yes" else price
        return unit * size + fee_per_contract * size


    def manage_orders(self, bid: float, ask: float, spread: float, ticker: str, inventory: int, side: str):
        # does it ever exit markets?
        current_orders = self.api.get_orders(ticker) or []

        buy_size = self.compute_desired_size(side, "buy", bid, spread, inventory)
        sell_size = inventory

        # Partition by action for THIS side only
        buy_orders  = [o for o in current_orders if o.get("side")==side and o.get("action")=="buy"]
        sell_orders = [o for o in current_orders if o.get("side")==side and o.get("action")=="sell"]

        def _px(o):
            raw = o.get("yes_price") if side=="yes" else o.get("no_price")
            f = float(raw)
            return to_tick(f/100.0 if f>1.0 else f)

        # If we have inventory, cancel ALL buy orders
        # Otherwise, keep only 1 best buy at bid; cancel others
        keep_buy = False
        if inventory > 0:
            # Cancel all buy orders when we have inventory
            for o in buy_orders:
                try:
                    self.api.cancel_order(o["order_id"])
                    self.logger.info(f"Canceling buy order for {ticker} [{side}] at {_px(o)} for {o.get('remaining_count', 0)} units (inventory={inventory})")
                    if self.metrics:
                        self.metrics.record_order_canceled(o["order_id"], ticker, side, _px(o), o.get('remaining_count', 0))
                        self.metrics.record_action("cancel_order", {"action":"buy","side":side,"price":_px(o),"size":o.get("remaining_count",0)})
                    self.circuit_breaker.record_success()
                except Exception as e:
                    self.logger.error(f"Failed to cancel buy order: {e}")
                    if self.metrics:
                        self.metrics.record_api_error("cancel_order", str(e), "cancel_order")
                    self.circuit_breaker.record_error("cancel_order", str(e))
        else:
            # Normal case: keep only 1 best buy at bid; cancel others
            for o in buy_orders:
                if _px(o) == bid and not keep_buy:
                    keep_buy = True
                else:
                    try:
                        self.api.cancel_order(o["order_id"])
                        self.logger.info(f"Canceling buy order for {ticker} [{side}] at {_px(o)} for {o.get('remaining_count', 0)} units")
                        if self.metrics:
                            self.metrics.record_order_canceled(o["order_id"], ticker, side, _px(o), o.get('remaining_count', 0))
                            self.metrics.record_action("cancel_order", {"action":"buy","side":side,"price":_px(o),"size":o.get("remaining_count",0)})
                        self.circuit_breaker.record_success()
                    except Exception as e:
                        self.logger.error(f"Failed to cancel buy order: {e}")
                        if self.metrics:
                            self.metrics.record_api_error("cancel_order", str(e), "cancel_order")
                        self.circuit_breaker.record_error("cancel_order", str(e))

        # Keep only 1 best sell at ask; cancel others
        keep_sell = False
        for o in sell_orders:
            if _px(o) == ask and not keep_sell:
                keep_sell = True
            else:
                try:
                    self.api.cancel_order(o["order_id"])
                    self.logger.info(f"Canceling sell order for {ticker} [{side}] at {_px(o)} for {o.get('remaining_count', 0)} units")
                    if self.metrics:
                        self.metrics.record_order_canceled(o["order_id"], ticker, side, _px(o), o.get('remaining_count', 0))
                        self.metrics.record_action("cancel_order", {"action":"sell","side":side,"price":_px(o),"size":o.get("remaining_count",0)})
                    self.circuit_breaker.record_success()
                except Exception as e:
                    self.logger.error(f"Failed to cancel sell order: {e}")
                    if self.metrics:
                        self.metrics.record_api_error("cancel_order", str(e), "cancel_order")
                    self.circuit_breaker.record_error("cancel_order", str(e))

        # Place buy if we don't already have one and have capacity
        if not keep_buy and buy_size > 0:
            try:
                if self.metrics:
                    self.metrics.record_order_sent(ticker, side, "buy", bid, buy_size)
                    
                oid = self.api.place_order(ticker, "buy", side, bid, buy_size, None)
                self.logger.info(f"Placing buy order for {ticker} [{side}] at {bid} for {buy_size} units")
                
                if self.metrics:
                    self.metrics.record_order_acknowledged(oid, ticker, side, "buy", bid, buy_size)
                    self.metrics.record_action("place_order", {"action":"buy","side":side,"price":bid,"size":buy_size})
                    
                self.circuit_breaker.record_success()
            except Exception as e:
                self.logger.error(f"Failed to place buy order: {e}")
                if self.metrics:
                    self.metrics.record_order_rejected(ticker, side, "buy", bid, buy_size, str(e))
                    self.metrics.record_api_error("place_order", str(e), "place_order")
                self.circuit_breaker.record_error("place_order", str(e))

        # Place sell only if you want to unload inventory (optional for LIP)
        self.logger.info(f"Inventory: {inventory}, keep_sell: {keep_sell}, sell_size: {sell_size}")
        if inventory > 0 and not keep_sell:
            try:
                if self.metrics:
                    self.metrics.record_order_sent(ticker, side, "sell", ask, sell_size)
                    
                self.logger.info(f"Placing sell order for {ticker} [{side}] at {ask} for {sell_size} units")
                oid = self.api.place_order(ticker, "sell", side, ask, sell_size, None)
                
                if self.metrics:
                    self.metrics.record_order_acknowledged(oid, ticker, side, "sell", ask, sell_size)
                    self.metrics.record_action("place_order", {"action":"sell","side":side,"price":ask,"size":sell_size})
                    
                self.circuit_breaker.record_success()
            except Exception as e:
                self.logger.error(f"Failed to place sell order: {e}")
                if self.metrics:
                    self.metrics.record_order_rejected(ticker, side, "sell", ask, sell_size, str(e))
                    self.metrics.record_api_error("place_order", str(e), "place_order")
                self.circuit_breaker.record_error("place_order", str(e))
