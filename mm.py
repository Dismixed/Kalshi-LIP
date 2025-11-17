import abc
from re import L, M
import time
from datetime import datetime
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
# import datetime  # Removed duplicate import - datetime class already imported above
import sys
import traceback
from dataclasses import dataclass, asdict, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import websockets

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
            'timestamp_iso': datetime.fromtimestamp(self.timestamp).isoformat(),
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
                self.logger.info(f"Inventory imbalance: {imbalance:.1%}, inventory={inventory}, max={max_position}")
                if imbalance > self.max_inventory_imbalance and self.is_open:
                    # todo not sure if it should just trip here
                    # self._trip(f"Inventory imbalance too high: {imbalance:.1%} (inventory={inventory}, max={max_position})")
                    pass
                    
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
            'timestamp_iso': datetime.now().isoformat(),
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


class WebSocketFillTracker:
    """
    WebSocket connection to track order fills in real-time.
    Based on Kalshi's WebSocket API: https://docs.kalshi.com/websockets/user-fills
    """
    def __init__(
        self,
        logger: logging.Logger,
        bot: Optional['LIPBot'] = None,
        metrics_tracker: Optional['MetricsTracker'] = None,
        stop_event: Optional[threading.Event] = None
    ):
        self.logger = logger
        self.bot = bot  # Reference to the bot for markout checks
        self.metrics_tracker = metrics_tracker
        self.stop_event = stop_event or threading.Event()
        self.ws = None
        self.ws_thread = None
        self.message_id = 1
        self.is_running = False
        self.reconnect_delay = 1.0  # Start with 1 second
        self.max_reconnect_delay = 60.0  # Max 60 seconds

    def _parse_date_to_timestamp(self, date_str: str) -> Optional[float]:
        """Parse ISO date string to Unix timestamp (seconds since epoch)"""
        if not date_str:
            return None
        try:
            # Handle 'Z' timezone by converting to '+00:00' for compatibility
            date_clean = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(date_clean)
            return dt.timestamp()
        except (ValueError, AttributeError):
            return None

    def _create_auth_headers(self) -> Dict[str, str]:
        """Create authentication headers for WebSocket connection"""
        api_key_id = os.getenv("KALSHI_API_KEY_ID")
        private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        
        if not api_key_id or not private_key_path:
            self.logger.error("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH for WebSocket")
            return {}
        
        try:
            # Load the private key
            with open(private_key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)
            
            # Create signature
            timestamp = str(int(time.time() * 1000))
            method = "GET"
            path = "/trade-api/ws/v2"
            message = f"{timestamp}{method}{path}".encode('utf-8')
            
            signature_bytes = private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
            signature = base64.b64encode(signature_bytes).decode('utf-8')
            
            return {
                'KALSHI-ACCESS-KEY': api_key_id,
                'KALSHI-ACCESS-SIGNATURE': signature,
                'KALSHI-ACCESS-TIMESTAMP': timestamp
            }
        except Exception as e:
            self.logger.error(f"Failed to create WebSocket auth headers: {e}")
            return {}
    
    async def _connect_and_listen(self):
        """Main WebSocket connection loop with automatic reconnection"""
        ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
        
        while not self.stop_event.is_set():
            try:
                # Create auth headers
                auth_headers = self._create_auth_headers()
                if not auth_headers:
                    self.logger.error("Failed to create auth headers, waiting before retry...")
                    await asyncio.sleep(self.reconnect_delay)
                    continue
                
                # Connect to WebSocket
                self.logger.info(f"Connecting to WebSocket: {ws_url}")
                async with websockets.connect(
                    ws_url,
                    additional_headers=auth_headers,
                    ping_interval=20,  # Send ping every 20 seconds
                    ping_timeout=10    # Timeout after 10 seconds
                ) as websocket:
                    self.ws = websocket
                    self.logger.info("WebSocket connected successfully")
                    
                    # Reset reconnect delay on successful connection
                    self.reconnect_delay = 1.0
                    
                    # Subscribe to fill channel
                    await self._subscribe_to_fills(websocket)
                    
                    # Listen for messages
                    async for message in websocket:
                        if self.stop_event.is_set():
                            break
                        await self._process_message(message)
                        
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"WebSocket connection closed: {e}")
                if not self.stop_event.is_set():
                    await self._handle_reconnect()
                    
            except Exception as e:
                self.logger.error(f"WebSocket error: {e}")
                if not self.stop_event.is_set():
                    await self._handle_reconnect()
        
        self.logger.info("WebSocket fill tracker stopped")
    
    async def _subscribe_to_fills(self, websocket):
        """Subscribe to the fill channel"""
        subscription = {
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["fill"]
            }
        }
        await websocket.send(json.dumps(subscription))
        self.message_id += 1
        self.logger.info("Subscribed to fill updates")
    
    async def _process_message(self, message: str):
        """Process incoming WebSocket messages"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "subscribed":
                self.logger.info(f"Subscription confirmed: {data}")
                
            elif msg_type == "fill":
                # Process fill notification
                fill_data = data.get("msg", {})
                self._handle_fill(fill_data)
                
            elif msg_type == "error":
                error_code = data.get("msg", {}).get("code")
                error_msg = data.get("msg", {}).get("msg")
                self.logger.error(f"WebSocket error {error_code}: {error_msg}")
                
            else:
                self.logger.debug(f"Received message type: {msg_type}")
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse WebSocket message: {e}")
        except Exception as e:
            self.logger.error(f"Error processing WebSocket message: {e}")
    
    def _handle_fill(self, fill_data: Dict):
        """Handle a fill notification"""
        try:
            trade_id = fill_data.get("trade_id")
            order_id = fill_data.get("order_id")
            market_ticker = fill_data.get("market_ticker")
            is_taker = fill_data.get("is_taker")
            side = fill_data.get("side")
            yes_price = fill_data.get("yes_price")
            yes_price_dollars = fill_data.get("yes_price_dollars")
            count = fill_data.get("count")
            action = fill_data.get("action")
            timestamp = fill_data.get("ts")
            post_position = fill_data.get("post_position")
            
            self.logger.info(
                f"FILL: {market_ticker} | {action.upper()} {count} @ ${yes_price_dollars} "
                f"(price={yes_price}) | Side: {side} | Taker: {is_taker} | "
                f"Post-position: {post_position} | Order: {order_id} | Trade: {trade_id}"
            )
            
            # Record fill in metrics if available
            if self.metrics_tracker:
                self.metrics_tracker.record_fill(
                    order_id=order_id,
                    ticker=market_ticker,
                    side=side,
                    action=action,
                    price=yes_price_dollars,
                    size=count,
                    fee=0.0,
                )
                if self.bot and hasattr(self.bot, "on_fill"):
                    try:
                        self.bot.on_fill({
                            'ts': timestamp,
                            'trade_id': trade_id,
                            'order_id': order_id,
                            'market_ticker': market_ticker,
                            'action': action,
                            'side': side,
                            'price': yes_price,
                            'price_dollars': yes_price_dollars,
                            'count': count,
                            'is_taker': is_taker,
                            'post_position': post_position
                        })
                    except Exception as e:
                        self.logger.error(f"Error in bot.on_fill: {e}")


            # Enqueue markout checks if bot is available
            if self.bot:
                current_time = time.time()
                
                # Update fill history for throttle tracking
                if hasattr(self.bot, '_fills_hist'):
                    self.bot._fills_hist.append(current_time)
                    # Cap list size to prevent unbounded growth
                    self.bot._fills_hist = self.bot._fills_hist[-2000:]
                
                # Enqueue markout checks for short and long term
                if hasattr(self.bot, '_markout_checks'):
                    self.bot._markout_checks.append({
                        "ticker": market_ticker,
                        "side": side,            # 'yes' or 'no'
                        "action": action,        # 'buy' or 'sell'
                        "price": float(yes_price_dollars),   # dollars
                        "count": count,          # number of contracts
                        "t_entry": current_time,
                        "t_check": [current_time + self.bot.mo_short, current_time + self.bot.mo_long],
                        "checked": [False, False],
                    })
                    # Cap list size to prevent unbounded growth
                    self.bot._markout_checks = self.bot._markout_checks[-2000:]
                    
                    self.logger.debug(
                        f"Enqueued markout checks for {market_ticker} {action} {side} @ ${yes_price_dollars:.4f} "
                        f"(short={self.bot.mo_short}s, long={self.bot.mo_long}s)"
                    )
                
        except Exception as e:
            self.logger.error(f"Error handling fill: {e}")
    
    async def _handle_reconnect(self):
        """Handle reconnection with exponential backoff"""
        self.logger.info(f"Reconnecting in {self.reconnect_delay:.1f} seconds...")
        await asyncio.sleep(self.reconnect_delay)
        
        # Exponential backoff
        self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
    
    def start(self):
        """Start the WebSocket connection in a separate thread"""
        if self.is_running:
            self.logger.warning("WebSocket fill tracker already running")
            return
        
        self.is_running = True
        self.stop_event.clear()
        
        def run_async_loop():
            """Run the asyncio event loop in a separate thread"""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._connect_and_listen())
            finally:
                loop.close()
        
        self.ws_thread = threading.Thread(target=run_async_loop, daemon=True)
        self.ws_thread.start()
        self.logger.info("WebSocket fill tracker started")
    
    def stop(self):
        """Stop the WebSocket connection"""
        if not self.is_running:
            return
        
        self.logger.info("Stopping WebSocket fill tracker...")
        self.stop_event.set()
        self.is_running = False
        
        # Wait for thread to finish (with timeout)
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=5.0)
        
        self.logger.info("WebSocket fill tracker stopped")


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

    def _parse_date_to_timestamp(self, date_str: str) -> Optional[float]:
        """Parse ISO date string to Unix timestamp (seconds since epoch)"""
        if not date_str:
            return None
        try:
            # Handle 'Z' timezone by converting to '+00:00' for compatibility
            date_clean = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(date_clean)
            return dt.timestamp()
        except (ValueError, AttributeError):
            return None

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

            timestamp = str(int(datetime.now().timestamp() * 1000))
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

    def get_all_positions(self) -> Dict[str, int]:
        """Get all positions across all tickers. Returns a dict mapping ticker -> position."""
        try:
            api_key_id = os.getenv("KALSHI_API_KEY_ID")
            private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
            base_url = "https://api.elections.kalshi.com"

            if not api_key_id or not private_key_path:
                self.logger.error("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH")
                return {}

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

            timestamp = str(int(datetime.now().timestamp() * 1000))
            method = "GET"
            path = "/trade-api/v2/portfolio/positions"

            signature = create_signature(private_key, timestamp, method, path)

            headers = {
                'KALSHI-ACCESS-KEY': api_key_id,
                'KALSHI-ACCESS-SIGNATURE': signature,
                'KALSHI-ACCESS-TIMESTAMP': timestamp
            }

            # No ticker parameter - get all positions
            response = requests.get(base_url + path, headers=headers, timeout=10)
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

            # Build dict mapping ticker -> position
            positions_dict = {}
            for item in market_positions_list:
                if isinstance(item, dict):
                    tkr = item.get('ticker')
                    pos = item.get('position', 0)
                else:
                    tkr = getattr(item, 'ticker', None)
                    pos = getattr(item, 'position', 0)
                
                if tkr:
                    try:
                        positions_dict[tkr] = int(pos)
                    except Exception:
                        positions_dict[tkr] = 0

            return positions_dict
                    
        except Exception as e:
            self.logger.error(f"Failed to get all positions: {e}")
            try:
                self.logger.error(getattr(getattr(e, 'response', None), 'text', ''))
            except Exception:
                pass
            return {}

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

        # Initialize drop counters
        drop_no_ticker = 0
        drop_ends_too_soon = 0
        drop_too_short_duration = 0
        drop_empty_orderbook = 0
        drop_missing_side = 0
        drop_resolved = 0

        def analyze_side(side: List[Tuple[float, int]], target: int, book_side: str, ticker: str = ""):
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

            self.logger.info(f"Ordered: {ordered} for {book_side} on {ticker}")

            best_price, best_size = ordered[0]

            self.logger.info(f"Best price: {best_price}, Best size: {best_size} for {ticker}")

            if best_size >= target:
                amount_needed = 0
                coverage = 1.0
            else:
                amount_needed = max(0, target - best_size)
                coverage = min(best_size, target) / target if target > 0 else 0.0

            return best_price, amount_needed, best_size, coverage

        mkts_checked: int = 0
        for market in liq_markets:
            mkts_checked += 1
            ticker = market.get("market_ticker")
            if not ticker:
                self.logger.info(f"No ticker for market: {market}")
                drop_no_ticker += 1
                continue

            # Filter out markets that end before 24 hours from now
            end_date = market.get("end_date", "")
            self.logger.info(f"End date: {end_date}")
            if end_date:
                try:
                    # Parse ISO date string and convert to timestamp
                    # Handle 'Z' timezone by converting to '+00:00' for compatibility
                    end_date_clean = end_date.replace('Z', '+00:00')
                    end_dt = datetime.fromisoformat(end_date_clean)
                    end_ts = end_dt.timestamp()
                    # Check if market ends before 24 hours from now
                    if end_ts < time.time() + 3 * 86400:  # 86400 seconds = 24 hours
                        self.logger.info(f"{ticker}: market ends before 24 hours; skipping")
                        drop_ends_too_soon += 1
                        continue
                except (ValueError, AttributeError) as e:
                    self.logger.warning(f"Failed to parse end_date '{end_date}' for {ticker}: {e}")
                    continue

            # Filter out markets where time between start and end is less than 28 hours
            start_date = market.get("start_date", "")
            self.logger.info(f"Start date: {start_date}")
            if start_date and end_date:
                try:
                    # Parse ISO date strings and convert to timestamps
                    start_date_clean = start_date.replace('Z', '+00:00')
                    end_date_clean = end_date.replace('Z', '+00:00')
                    start_dt = datetime.fromisoformat(start_date_clean)
                    end_dt = datetime.fromisoformat(end_date_clean)
                    start_ts = start_dt.timestamp()
                    end_ts_check = end_dt.timestamp()
                    # Check if duration is less than 28 hours
                    duration_hours = (end_ts_check - start_ts) / 3600.0
                    if duration_hours < 28:
                        self.logger.info(f"{ticker}: market duration {duration_hours:.1f} hours < 28 hours; skipping")
                        drop_too_short_duration += 1
                        continue
                except (ValueError, AttributeError) as e:
                    self.logger.warning(f"Failed to parse dates for {ticker}: start='{start_date}', end='{end_date}': {e}")
                    continue
            orderbook = self.get_orderbook(ticker)
            var_true = orderbook.get("var_true", [])
            var_false = orderbook.get("var_false", [])

            if not var_true and not var_false:
                self.logger.info(f"{ticker}: empty orderbook (both sides empty); skipping")
                drop_empty_orderbook += 1
                continue

            # Skip if missing orders on either side
            if not var_true or not var_false:
                missing_side = "YES" if not var_true else "NO"
                self.logger.info(f"{ticker}: missing orders on {missing_side} side; skipping")
                drop_missing_side += 1
                continue

            # Skip if top of orderbook is at 99c or 1c on either side
            skip_market = False
            price = self.get_price(ticker)
            yes_mid = price.get("yes")
            no_mid = price.get("no")
            if yes_mid >= 0.90 or yes_mid <= 0.10:
                self.logger.info(f"{ticker}: market is resolved; skipping")
                skip_market = True
            
            if no_mid >= 0.90 or no_mid <= 0.10:
                self.logger.info(f"{ticker}: market is resolved; skipping")
                skip_market = True
                
            if skip_market:
                drop_resolved += 1
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
                (best_yes, amount_needed_yes, best_size_yes, cov_yes) = analyze_side(var_true, target_size, "bid", ticker)
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
                        "end_date": self._parse_date_to_timestamp(market.get("end_date", "")),
                    }
                    entry_yes["score"] = score_side("yes", entry_yes)
                    valid_markets.append(entry_yes)

            # NO side entry
            if var_false:
                (best_no, amount_needed_no, best_size_no, cov_no) = analyze_side(var_false, target_size, "ask", ticker)
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
                        "end_date": self._parse_date_to_timestamp(market.get("end_date", "")),
                    }
                    entry_no["score"] = score_side("no", entry_no)
                    valid_markets.append(entry_no)

        self.logger.info(f"Markets checked: {mkts_checked}")
        self.logger.info(f"Market filtering summary:")
        self.logger.info(f"  Dropped - No ticker: {drop_no_ticker}")
        self.logger.info(f"  Dropped - Ends too soon: {drop_ends_too_soon}")
        self.logger.info(f"  Dropped - Too short duration: {drop_too_short_duration}")
        self.logger.info(f"  Dropped - Empty orderbook: {drop_empty_orderbook}")
        self.logger.info(f"  Dropped - Missing side: {drop_missing_side}")
        self.logger.info(f"  Dropped - Resolved: {drop_resolved}")
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

def yes_equiv_from(side: str, action: str, price: float) -> Tuple[str, float]:
    """
    Map (side, action, price) to the YES-equivalent (action_y, price_y).
    - buy NO @ p  -> sell YES @ (1-p)
    - sell NO @ p -> buy  YES @ (1-p)
    - YES stays the same
    """
    price = to_tick(price)
    if side == "yes":
        return action, price
    # side == "no"
    y_price = to_tick(1.0 - price)
    y_action = "sell" if action == "buy" else "buy"
    return y_action, y_price

def no_from_yes(action_y: str, price_y: float) -> Tuple[str, float]:
    """If you ever need to send to NO side explicitly."""
    return ("sell" if action_y == "buy" else "buy", to_tick(1.0 - price_y))

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
        my_positions: Optional[List[str]] = None,
        inventory_buy_threshold: float = 0.4,
        max_workers: int = 5,
        _market_end_ts: Optional[Dict[str, float]] = None,
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
        self.my_positions = set(my_positions) if my_positions else set()  # Set of tickers that are personal positions
        self.inventory_buy_threshold = float(inventory_buy_threshold)  # Stop buying when inventory > threshold * max_position
        self.max_workers = max(1, int(max_workers))  # Number of parallel threads for order management
        self._market_end_ts = _market_end_ts or {}

        self.toxicity_cooldown_secs = float(os.getenv("LIP_TOXICITY_COOLDOWN", "1800"))  # 30 min
        self._toxic_until: Dict[str, float] = {}  # ticker -> epoch when we can try again

        
        # Monitoring and safety systems
        self.alert_manager = AlertManager(logger)
        self.circuit_breaker = CircuitBreaker(
            max_consecutive_errors=max_consecutive_errors,
            pnl_threshold=pnl_threshold,
            max_inventory_imbalance=max_inventory_imbalance,
            logger=logger,
            alert_manager=self.alert_manager
        )

        # --- Stale-quote / velocity state ---
        self._last_touch = {}     # (ticker -> (best_bid, best_ask))
        self._cooldown_until = {} # (ticker -> epoch seconds)
        self.fast_move_ticks = 1  # 1 tick = 0.01
        self.cooldown_secs = 15  # sit out a few seconds after sweep/thin
        
        # metrics
        self.metrics: Optional[MetricsTracker] = None
        
        # Track positions for PnL calculation
        self.position_tracker: Dict[str, Dict] = {}  # ticker -> {inventory, cost_basis, realized_pnl}
        self.position_tracker: Dict[str, Dict] = {}  # ticker -> {inventory, avg_price, realized_pnl}
        self._position_lock = threading.Lock()

        
        # improvement gating state (thread-safe with locks)
        self._last_external_touch: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}
        self._improved_on_touch: Dict[Tuple[str, str], bool] = {}
        self._last_improve_ts: Dict[Tuple[str, str], float] = {}
        self._state_lock = threading.Lock()  # Lock for thread-safe access to shared state
        
        # Send startup alert
        self.alert_manager.send_alert(AlertLevel.INFO, "bot_lifecycle", "Market maker bot starting up", {})

        # --- Markout adaptation ---
        self._markout_ema = {}          # ticker -> EMA markout in dollars (yes-mid vs entry)
        self._edge_bonus = {}           # ticker -> extra edge requirement (dollars)
        self._width_bonus = {}          # ticker -> extra min-width (dollars)

        # parameters
        self.mo_short = 5.0    # seconds
        self.mo_long  = 30.0   # seconds
        self.mo_alpha = 0.4    # EMA smoothing
        self.mo_bad_threshold = -0.003  # -0.3¢ average = toxic
        self.edge_bump = 0.002          # add 0.2¢ edge when toxic
        self.width_bump = 0.01          # add 1¢ width when toxic

        # queue of delayed checks from fills
        self._markout_checks = []  # list of dicts
        self._fills_hist = []  # list of fill timestamps for throttle data

        self._target_sizes: Dict[str, int] = {}
        self._last_target_refresh_ts = 0.0
        self._target_refresh_interval = 60.0  # seconds
        
        # WebSocket fill tracker (will be started when run() is called)
        self.ws_fill_tracker: Optional[WebSocketFillTracker] = None

        # How many *new* markets to start per discovery cycle
        self.discovery_max_new = int(os.getenv("LIP_DISCOVERY_MAX_NEW", "8"))

        # How many candidates to scan per cycle before giving up (safety cap)
        self.discovery_scan_cap = int(os.getenv("LIP_DISCOVERY_SCAN_CAP", "100"))

        def on_fill(self, fill: Dict) -> None:
            """
            Update per-ticker inventory, avg_price, and realized_pnl from a fill.
            We track everything in YES-equivalent space.
            """
            try:
                ticker = fill["market_ticker"]
                side = fill["side"]      # 'yes' or 'no'
                action = fill["action"]  # 'buy' or 'sell'
                count = int(fill["count"])
                price_y = float(fill["price_dollars"])  # already dollars

                if count <= 0:
                    return

                # Map (side, action, price) -> YES-equivalent action & price
                act_y, px_y = yes_equiv_from(side, action, price_y)
                # Trade sign: +count for buy YES, -count for sell YES
                trade_qty = count if act_y == "buy" else -count

                with self._position_lock:
                    pos = self.position_tracker.get(ticker, {
                        "inventory": 0,
                        "avg_price": 0.0,
                        "realized_pnl": 0.0,
                    })
                    q_old = int(pos["inventory"])
                    p_old = float(pos["avg_price"])
                    rpnl = float(pos["realized_pnl"])

                    # --- No existing position ---
                    if q_old == 0:
                        q_new = trade_qty
                        p_new = px_y
                        # rpnl unchanged

                    else:
                        # Existing position q_old (can be + or -)
                        # Trade q_trade = trade_qty (can be + or -)
                        same_dir = (q_old > 0 and trade_qty > 0) or (q_old < 0 and trade_qty < 0)

                        if same_dir:
                            # Increasing exposure in same direction -> weighted avg price
                            q_new = q_old + trade_qty
                            if q_new != 0:
                                p_new = (
                                    p_old * abs(q_old) + px_y * abs(trade_qty)
                                ) / abs(q_new)
                            else:
                                p_new = 0.0

                        else:
                            # Opposite direction → first close some/all of old position
                            close_qty = min(abs(q_old), abs(trade_qty))
                            # Realized PnL from closing part:
                            if q_old > 0:
                                # Closing a long: sold at px_y, bought at p_old
                                rpnl += (px_y - p_old) * close_qty
                            else:
                                # Closing a short: originally sold at p_old, now buying at px_y
                                rpnl += (p_old - px_y) * close_qty

                            # Remaining open quantity after closing
                            remaining_old = abs(q_old) - close_qty
                            remaining_new = abs(trade_qty) - close_qty

                            if remaining_old > 0 and remaining_new == 0:
                                # Still same direction as q_old, just smaller
                                q_new = q_old + trade_qty
                                p_new = p_old  # avg price of remaining chunk unchanged

                            elif remaining_new > 0 and remaining_old == 0:
                                # Flipped and opened a new position in direction of trade
                                q_new = q_old + trade_qty
                                p_new = px_y  # new position entirely at this fill price

                            else:
                                # Exactly flat
                                q_new = 0
                                p_new = 0.0

                    pos["inventory"] = q_new
                    pos["avg_price"] = p_new
                    pos["realized_pnl"] = rpnl
                    self.position_tracker[ticker] = pos

                    if self.metrics:
                        self.metrics.log_structured("position_update", {
                            "ticker": ticker,
                            "inventory": q_new,
                            "avg_price": round(p_new, 4),
                            "realized_pnl": round(rpnl, 2),
                        })

            except Exception as e:
                self.logger.error(f"on_fill error: {e}")

    def _current_yes_mid(self, ticker: str) -> Optional[float]:
        """Return current YES mid (dollars) for ticker, or None."""
        try:
            touch = self.api.get_touch(ticker) or {}
            y = touch.get("yes")
            if not y:
                return None
            bid, ask = y
            if not bid or not ask:
                return None
            return to_tick((bid + ask) / 2.0)
        except Exception:
            return None

    def _refresh_target_sizes(self):
        """Refresh per-market LIP target sizes from active incentive programs."""
        try:
            items = self.api.get_liq_markets() or []
            for it in items:
                tkr = it.get("market_ticker")
                tsz = it.get("target_size") or 0
                self._target_sizes[tkr] = int(tsz)

                end_raw = it.get("end_date", None)
                if end_raw is not None:
                    end_ts = float(end_raw)
                    if end_ts > 1e12:
                        end_ts = end_ts / 1000.0
                    self._market_end_ts[tkr] = end_ts
        except Exception as e:
            self.logger.warning(f"Target-size refresh failed: {e}")


    def _update_markout_ema(self, ticker: str, realized_markout: float):
        """EMA update and bump state."""
        prev = self._markout_ema.get(ticker, 0.0)
        ema = self.mo_alpha * realized_markout + (1.0 - self.mo_alpha) * prev
        self._markout_ema[ticker] = ema

        # Decide bumps
        if ema <= self.mo_bad_threshold:  # toxic flow → require more edge & width
            self._edge_bonus[ticker] = max(self._edge_bonus.get(ticker, 0.0), self.edge_bump)
            self._width_bonus[ticker] = max(self._width_bonus.get(ticker, 0.0), self.width_bump)
            self.logger.info(f"{ticker}: markout EMA {ema:.4f} ≤ {self.mo_bad_threshold:.4f} → bump edge+width")
        else:
            # gentle decay back to zero
            self._edge_bonus[ticker] = max(0.0, self._edge_bonus.get(ticker, 0.0) * 0.5)
            self._width_bonus[ticker] = max(0.0, self._width_bonus.get(ticker, 0.0) * 0.5)

    def _hours_to_expiry(self, ticker: str) -> Optional[float]:
        end_ts = self._market_end_ts.get(ticker)
        if not end_ts:
            return None
        now = time.time()
        return max(0.0, (end_ts - now) / 3600.0)

    def _drain_markout_checks(self):
        """Run due markout checks enqueued by fills (short + long horizons)."""
        now = time.time()
        if not self._markout_checks:
            return

        # process in-place; keep pending checks
        remaining = []
        for item in self._markout_checks:
            tkr = item["ticker"]
            side = item["side"]        # 'yes'/'no'
            action = item["action"]    # 'buy'/'sell'
            px_dollars = float(item["price"])
            t_check_list = item["t_check"]
            checked = item["checked"]

            # yes-equivalent mapping to align markout sign
            act_y, px_y = yes_equiv_from(side, action, px_dollars)

            # evaluate any due checkpoints
            for idx, t_due in enumerate(t_check_list):
                if checked[idx] or now < t_due:
                    continue

                mid_y = self._current_yes_mid(tkr)
                if mid_y is None:
                    # if we can't get a mid now, retry later
                    continue

                # markout: positive if trade would be profitable in YES-terms
                # buy YES → profit if mid - entry; sell YES → profit if entry - mid
                sign = +1.0 if act_y == "buy" else -1.0
                realized = sign * (mid_y - px_y)

                # update EMA + bumps
                self._update_markout_ema(tkr, realized)

                # metrics (optional)
                if self.metrics:
                    self.metrics.log_structured("markout_check", {
                        "ticker": tkr,
                        "horizon": "short" if idx == 0 else "long",
                        "act_y": act_y,
                        "entry_y": round(px_y, 2),
                        "mid_y": round(mid_y, 2),
                        "markout": round(realized, 4),
                        "ema": round(self._markout_ema.get(tkr, 0.0), 4)
                    })

                checked[idx] = True

            # keep the item if there are still pending checkpoints
            if not all(checked):
                remaining.append(item)

        self._markout_checks = remaining

    def _process_single_market(self, ticker: str, orders_by_ticker: Dict[str, List[Dict]]) -> None:
        """
        Process order management for a single market.
        This method is designed to be thread-safe and can be called in parallel.
        """
        try:
            toxic_until = self._toxic_until.get(ticker)
            if toxic_until and time.time() < toxic_until:
                self.logger.info(f"{ticker}: in toxicity cooldown until {time.ctime(toxic_until)}, skipping.")
                return {"ticker": ticker, "untrack": False}

            # Get touch data for the market
            try:
                touch = self.api.get_touch(ticker)
            except Exception as e:
                self.logger.warning(f"Failed to get touch for {ticker}: {e}")
                return

            # Get current position
            try:
                inventory = self.api.get_position(ticker)
            except Exception as e:
                self.logger.warning(f"Failed to get position for {ticker}: {e}")
                inventory = 0

            hrs = self._hours_to_expiry(ticker)
            soft = 48
            hard = 6

            expiry_mode = "normal"
            if hrs is not None:
                if hrs <= hard:
                    expiry_mode = "hard"
                elif hrs <= soft:
                    expiry_mode = "soft"
            self.logger.debug(f"{ticker}: hours_to_expiry={hrs}, expiry_mode={expiry_mode}")

            # Only manage YES side - cancel any NO side orders first
            side = "yes"

            
            # Cancel all NO side orders (they're redundant with YES orders)
            no_orders = [o for o in orders_by_ticker.get(ticker, []) if o.get('side') == 'no']
            for o in no_orders:
                try:
                    self.api.cancel_order(o["order_id"])
                    self.logger.info(f"Canceling redundant NO order for {ticker} (we only manage YES side)")
                    if self.metrics:
                        self.metrics.record_order_canceled(o["order_id"], ticker, "no", 0, o.get('remaining_count', 0))
                except Exception as e:
                    self.logger.error(f"Failed to cancel NO order: {e}")
            
            side_touch = touch.get(side)
            if not side_touch:
                return
            mkt_bid, mkt_ask = side_touch
            spread = max(0.0, (mkt_ask - mkt_bid))

            now = time.time()

            if hrs is not None and hrs <= 1.0 and inventory != 0:
                # Cross the spread to get out instead of waiting to be lifted
                cashout_action = "sell" if inventory > 0 else "buy"
                cashout_price = mkt_bid if inventory > 0 else mkt_ask
                size = abs(inventory)
                self.logger.info(f"{ticker}: {hrs:.1f}h to expiry, force-flatten {size} @ {cashout_price:.2f}")
                try:
                    self.api.place_order(ticker, cashout_action, side, cashout_price, size, None)
                except Exception as e:
                    self.logger.error(f"{ticker}: force-flatten failed near expiry: {e}")
                # Skip normal order management this cycle
                return

            if self._cooldown_until.get(ticker, 0) > now:
                # In cooldown: cancel all orders for this ticker and skip
                try:
                    for o in self.api.get_orders(ticker) or []:
                        self.api.cancel_order(o["order_id"])
                except Exception:
                    pass
                self.logger.info(f"{ticker}: in cooldown, skipping quoting")
                return
                
            orderbook = {}
            try:
                orderbook = self.api.get_orderbook(ticker)
            except Exception as e:
                self.logger.warning(f"Failed to get orderbook for {ticker}: {e}")
                orderbook = {}
            
            if orderbook and self.thin_book(orderbook, min_lvl_size=200, levels=2):
                self.logger.info(f"{ticker}: thin book detected → shrink size / widen or skip")
                return

            
            prev_touch = self._last_touch.get(ticker)
            is_fast = self.fast_move(prev_touch, (mkt_bid, mkt_ask))
            
            target = self._target_sizes.get(ticker, 0)
            self.logger.info(f"Target size for {ticker}: {target}")
            block_bid_for_lip = False

            if target and target > 0:
                # In your normalization: YES bids = var_true, YES asks = 1 - var_false
                yes_bids = [(to_tick(p), sz) for (p, sz) in (orderbook.get("var_true") or [])]
                yes_asks = [(to_tick(1.0 - p), sz) for (p, sz) in (orderbook.get("var_false") or [])]

                best_bid_size = self._best_level_size(yes_bids, bid_side=True)
                best_ask_size = self._best_level_size(yes_asks, bid_side=False)

                if best_bid_size >= target:
                    block_bid_for_lip = True
                    for o in self.api.get_orders(ticker) or []:
                        if o.get("side") == "yes" and o.get("action") == "buy":
                            self.api.cancel_order(o["order_id"])
                    if inventory == 0:
                        return {"ticker": ticker, "untrack": True}
                    else:
                        return {"ticker": ticker, "untrack": False}

            if is_fast:
                # pull quotes and set cooldown
                try:
                    for o in self.api.get_orders(ticker) or []:
                        self.api.cancel_order(o["order_id"])
                except Exception:
                    pass
                self._cooldown_until[ticker] = now + self.cooldown_secs
                self.logger.info(f"{ticker}: fast={is_fast} → cooldown {self.cooldown_secs}s")
                # update trackers and skip this cycle
                self._last_touch[ticker] = (mkt_bid, mkt_ask)
                return
            
            self._last_touch[ticker] = (mkt_bid, mkt_ask)

            # Check if market is resolved and cash out if needed
            if self.check_and_cashout_resolved_market(ticker, side, mkt_bid, mkt_ask, inventory):
                # Market is resolved and we attempted to cash out - skip normal order management
                self.logger.info(f"Skipping normal order management for resolved market {ticker}")
                return
            
            # Determine if best touch is ours (exclude our quotes for external-change detection)
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
            
            # Thread-safe access to shared state
            with self._state_lock:
                last_ext = self._last_external_touch.get(key)
                external_changed = (last_ext != (ext_bid, ext_ask))
                if external_changed:
                    self._improved_on_touch[key] = False
                now_ts = time.time()
                cooldown_ok = (self.improve_cooldown_seconds <= 0) or (now_ts - self._last_improve_ts.get(key, 0.0) >= self.improve_cooldown_seconds)
                allow_improvement = True
                if self.improve_once_per_touch:
                    allow_improvement = (not self._improved_on_touch.get(key, False)) and cooldown_ok

            if target and target > 0:
                # In your normalization: YES bids = var_true, YES asks = 1 - var_false
                yes_bids = [(to_tick(p), sz) for (p, sz) in (orderbook.get("var_true") or [])]
                yes_asks = [(to_tick(1.0 - p), sz) for (p, sz) in (orderbook.get("var_false") or [])]

                best_bid_size = self._best_level_size(yes_bids, bid_side=True)
                best_ask_size = self._best_level_size(yes_asks, bid_side=False)

                if best_bid_size >= target:
                    self.logger.info(f"{ticker}: LIP target met in _process_single_market (best_bid_size={best_bid_size} >= target={target})")
                    block_bid_for_lip = True
                    # Cancel all buy orders since target is met
                    for o in self.api.get_orders(ticker) or []:
                        if o.get("side") == "yes" and o.get("action") == "buy":
                            try:
                                self.api.cancel_order(o["order_id"])
                                self.logger.info(f"{ticker}: Canceled buy order {o['order_id']} due to LIP target met")
                            except Exception as e:
                                self.logger.error(f"{ticker}: Failed to cancel buy order {o['order_id']}: {e}")
                    
                    # If we have no inventory, we should exit this market entirely
                    if inventory == 0:
                        self.logger.info(f"{ticker}: LIP target met and flat position → untracking market")
                        return {"ticker": ticker, "untrack": True}
                    else:
                        # We have inventory, so we still need to manage exit orders but no new bids
                        self.logger.info(f"{ticker}: LIP target met but have inventory={inventory} → will only place exit orders")
                        # Continue to allow_ask management but ensure no bids are placed

            fair = self.compute_fair(orderbook)
            if fair is None:
                self.logger.warning(f"Failed to compute fair price for {ticker}")
                # If we have inventory, still try to place sell orders to exit position
                if inventory > 0:
                    self.logger.info(f"Attempting to exit {inventory} units of inventory for {ticker} despite no fair price")
                    # Use market prices as fallback for selling inventory
                    bid, ask = mkt_bid, mkt_ask
                    allow_bid = False  # Don't place new buy orders without fair price
                    allow_ask = True   # Allow sell orders to exit inventory
                    self.manage_orders(bid, ask, spread, ticker, inventory, side, allow_bid=allow_bid, allow_ask=allow_ask)
                return

            # Apply adaptive bumps based on markout EMA
            edge_min = 0.01 + float(self._edge_bonus.get(ticker, 0.0) or 0.0)
            min_width_local = max(self.min_quote_width, float(self._width_bonus.get(ticker, 0.0) or 0.0))

            bid, ask = self.compute_quotes(mkt_bid, mkt_ask, inventory, allow_improvement=allow_improvement, min_width=min_width_local, block_bid_for_lip=block_bid_for_lip)

            # add edge calculation with adaptive edge_min
            allow_bid = (fair - bid) >= edge_min
            allow_ask = (inventory > 0)

            if expiry_mode == "soft":
                # scale down size later (we’ll handle in manage_orders) and
                # be more conservative about opening new risk
                allow_bid = allow_bid and (abs(inventory) < self.max_position * 0.5)

            if expiry_mode == "hard":
                # DO NOT open new risk; only exit
                allow_bid = False
                # only allow asks if we actually have inventory to dump
                allow_ask = (inventory > 0)

            if expiry_mode == "hard" and inventory == 0:
                # fully flat near expiry: no reason to be in this name
                try:
                    for o in self.api.get_orders(ticker) or []:
                        self.api.cancel_order(o["order_id"])
                except Exception:
                    pass
                self.logger.info(f"{ticker}: hard-expiry window & flat → untracking.")
                return {"ticker": ticker, "untrack": True}


            if block_bid_for_lip:
                allow_bid = False

            # If target met on ask and we're flat, don't place asks (no scoring benefit).
            # If we have inventory, keep allow_ask True so we can exit risk.
            if inventory == 0 and (not allow_bid) and (not allow_ask):
                try:
                    for o in self.api.get_orders(ticker) or []:
                        self.api.cancel_order(o["order_id"])
                        if self.metrics:
                            self.metrics.record_order_canceled(
                                o["order_id"], ticker, o.get('side', 'yes'), 0, o.get('remaining_count', 0)
                            )
                except Exception as e:
                    self.logger.warning(f"{ticker}: cancel-before-untrack failed: {e}")
                self.logger.info(f"{ticker}: LIP target met, flat; untracking market.")
                return {"ticker": ticker, "untrack": True}


            # Surface toxicity state (optional)
            if self.metrics:
                self.metrics.log_structured("toxicity_state", {
                    "ticker": ticker,
                    "ema": round(self._markout_ema.get(ticker, 0.0), 4),
                    "edge_bonus": round(self._edge_bonus.get(ticker, 0.0), 4),
                    "width_bonus": round(self._width_bonus.get(ticker, 0.0), 4)
                })

            self.manage_orders(bid, ask, spread, ticker, inventory, side, allow_bid=allow_bid, allow_ask=allow_ask)
            
            # Update gating state (thread-safe)
            with self._state_lock:
                self._last_external_touch[key] = (ext_bid, ext_ask)
                if allow_improvement and inventory == 0 and spread >= 0.02:
                    self._improved_on_touch[key] = True
                    self._last_improve_ts[key] = now_ts
            
            ema = self._markout_ema.get(ticker, 0.0)
            very_bad = self.mo_bad_threshold * 5.0

            if ema <= very_bad:
                self.logger.warning(
                    f"{ticker}: markout EMA {ema:.4f} extremely bad ≤ {very_bad:.4f} → toxicity cooldown"
                )
                # cancel orders
                try:
                    for o in self.api.get_orders(ticker) or []:
                        self.api.cancel_order(o["order_id"])
                except Exception as e:
                    self.logger.warning(f"{ticker}: failed to cancel orders on toxicity stop: {e}")

                # set cooldown
                self._toxic_until[ticker] = time.time() + self.toxicity_cooldown_secs
                return {"ticker": ticker, "untrack": True}

            
            return {"ticker": ticker, "untrack": False}
                    
        except Exception as e:
            self.logger.error(f"Error processing market {ticker}: {e}")
            self.logger.error(f"Traceback: {traceback.format_exc()}")
    def _best_level_size(self, levels: List[Tuple[float, int]], bid_side: bool) -> int:
        """
        levels: [(price, count), ...]
        bid_side=True → choose max price; else min price.
        Returns the total size queued exactly at the best price.
        """
        if not levels:
            return 0
        try:
            best_px = max(p for p, c in levels) if bid_side else min(p for p, c in levels)
            return sum(int(c) for p, c in levels if p == best_px)
        except Exception:
            return 0

    def run(self, dt: float):
        start_time = time.time()
        print(f"Starting LIPBot")
        if self.metrics is None:
            strategy_name = getattr(self.logger, 'name', 'Strategy')
            self.metrics = MetricsTracker(strategy_name=strategy_name, market_ticker=None)
            
            # Start WebSocket fill tracker
            if self.ws_fill_tracker is None:
                self.ws_fill_tracker = WebSocketFillTracker(
                    logger=self.logger,
                    bot=self,  # Pass bot instance for markout checks
                    metrics_tracker=self.metrics,
                    stop_event=self.stop_event
                )
                self.ws_fill_tracker.start()
                self.logger.info("WebSocket fill tracker initialized and started")
            
            # Track markets we have activity in
            tracked_markets: Dict[str, Dict[str, bool]] = {}
            last_discovery_ts: float = 0.0
            last_pnl_check_ts: float = 0.0

            while not self._should_stop():
                loop_start = time.time()
                now_ts = time.time()
                if (now_ts - self._last_target_refresh_ts) >= self._target_refresh_interval:
                    self._refresh_target_sizes()
                    self._last_target_refresh_ts = now_ts

                
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

                # Drain markout checks
                self._drain_markout_checks()

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
                # Only track YES side (NO side orders will be cancelled)
                for tkr in orders_by_ticker.keys():
                    tracked_markets.setdefault(tkr, {})
                    tracked_markets[tkr]['yes'] = True

                # Also include tickers with positions (excluding my_positions)
                # Note: Positive positions represent "yes" positions, negative positions represent "no" positions
                try:
                    if hasattr(self.api, 'get_all_positions'):
                        all_positions = self.api.get_all_positions() or {}
                        for ticker, position in all_positions.items():
                            self.logger.info(f"Ticker: {ticker}, Position: {position}")
                            # Only add if we have a non-zero position (positive = yes, negative = no) and it's not in my_positions
                            if position != 0 and ticker not in self.my_positions:
                                # Only track YES side to avoid duplicate positions
                                # (buying YES = selling NO, so we only need to manage one side)
                                tracked_markets.setdefault(ticker, {})
                                tracked_markets[ticker]['yes'] = True
                except Exception as e:
                    self.logger.warning(f"Failed to fetch all positions for managed tickers: {e}")
                # For each tracked ticker, compute quotes and manage orders (in parallel)
                # IMPORTANT: Only manage YES side to avoid duplicate positions
                # (buying YES = selling NO, so managing both creates duplicate orders)
                tickers_to_process = list(tracked_markets.keys())
                
                if tickers_to_process:
                    # Use ThreadPoolExecutor to process markets in parallel
                    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                        # Submit all market processing tasks
                        futures = {
                            executor.submit(self._process_single_market, ticker, orders_by_ticker): ticker
                            for ticker in tickers_to_process
                        }
                        
                        # Wait for all tasks to complete
                        for future in as_completed(futures):
                            ticker = futures[future]
                            try:
                                status = future.result()
                                if isinstance(status, dict) and status.get("untrack"):
                                    tracked_markets.pop(ticker, None)
                                    self.logger.info(f"Stopped tracking {ticker} (LIP-gated and flat).")
                            except Exception as e:
                                self.logger.error(f"Error in parallel processing for {ticker}: {e}")

                    
                    self.logger.info(f"Processed {len(tickers_to_process)} markets in parallel with {self.max_workers} workers")

                # 2) Periodically discover new markets to enter
                if (time.time() - last_discovery_ts) >= max(1.0, dt):
                    self.logger.info(f"Discovering new markets")
                    try:
                        valid_markets = self.api.get_valid_markets() or []
                        valid_markets.sort(key=lambda x: x.get('score', 0), reverse=True)
                        self.logger.info(f"Valid markets: {valid_markets}")
                    except Exception as e:
                        self.logger.warning(f"Market discovery failed: {e}")
                        valid_markets = []

                    # Try the top few candidates not currently tracked
                    # Only consider YES side entries (to avoid duplicate positions)
                    added = 0
                    scanned = 0

                    for entry in valid_markets:
                        if scanned >= self.discovery_scan_cap or added >= self.discovery_max_new:
                            break
                        scanned += 1


                        tkr = entry.get('ticker')
                        entry_side = entry.get('side')
                        end_raw = entry.get('end_date', None)
                        ema = self._markout_ema.get(tkr)
                        if ema is not None and ema <= (self.mo_bad_threshold * 3.0):
                            self.logger.info(f"[DISCOVERY] Skipping {tkr}: historically toxic (EMA={ema:.4f})")
                            continue
                        if end_raw is not None:
                            end_ts = float(end_raw)
                            if end_ts > 1e12:
                                end_ts = end_ts / 1000.0
                            self._market_end_ts[tkr] = end_ts
                        if not tkr:
                            self.logger.info(f"No ticker for market: {entry}")
                            continue
                        # Skip NO side entries - we only manage YES side
                        if entry_side == 'no':
                            self.logger.info(f"No side for market: {entry}")
                            entry = dict(entry)
                            entry['side'] = 'yes'
                            if entry.get('best_price') is not None:
                                entry['best_price'] = to_tick(1.0 - float(entry['best_price']))

                        if tkr in orders_by_ticker:
                            self.logger.info(f"Already have orders for market: {entry}")
                            continue  # already have orders
                        # attempt to start managing this market (YES side only)
                        side = "yes"
                        try:
                            self.logger.info(f"Getting touch for market: {tkr}")
                            touch = self.api.get_touch(tkr)
                            if side not in touch:
                                self.logger.info(f"No touch for market: {entry}")
                                continue
                            mkt_bid, mkt_ask = touch[side]
                            self.logger.info(f"Mkt bid: {mkt_bid}, Mkt ask: {mkt_ask}")
                            inventory = self.api.get_position(tkr)
                            spread = max(0.0, (mkt_ask - mkt_bid))
                            # no orders yet; treat external touch as the live touch
                            self.logger.info(f"Spread: {spread}")
                            key = (tkr, side)
                            self._last_external_touch.setdefault(key, (to_tick(mkt_bid), to_tick(mkt_ask)))
                            self._improved_on_touch.setdefault(key, False)
                            now_ts = time.time()
                            cooldown_ok = (self.improve_cooldown_seconds <= 0) or (now_ts - self._last_improve_ts.get(key, 0.0) >= self.improve_cooldown_seconds)
                            allow_improvement = True
                            if self.improve_once_per_touch:
                                allow_improvement = (not self._improved_on_touch.get(key, False)) and cooldown_ok
                            orderbook = {}
                            try:
                                orderbook = self.api.get_orderbook(tkr)
                            except Exception as e:
                                self.logger.warning(f"Failed to get orderbook for {tkr}: {e}")
                                orderbook = {}
                            
                            target = self._target_sizes.get(tkr)
                            self.logger.info(f"Target size for {tkr}: {target}")
                            block_bid_for_lip = False

                            if target and target > 0:
                                # In your normalization: YES bids = var_true, YES asks = 1 - var_false
                                yes_bids = [(to_tick(p), sz) for (p, sz) in (orderbook.get("var_true") or [])]
                                yes_asks = [(to_tick(1.0 - p), sz) for (p, sz) in (orderbook.get("var_false") or [])]

                                best_bid_size = self._best_level_size(yes_bids, bid_side=True)
                                best_ask_size = self._best_level_size(yes_asks, bid_side=False)

                                if best_bid_size >= target:
                                    self.logger.info(f"Best bid size {best_bid_size} >= target {target} for {tkr}")
                                    block_bid_for_lip = True
                                    for o in self.api.get_orders(tkr) or []:
                                        if o.get("side") == "yes" and o.get("action") == "buy":
                                            self.api.cancel_order(o["order_id"])
                                    self.logger.info(f"[DISCOVERY] Skipping {tkr}: LIP target met at best")
                                    continue 

                            #todo change to bias towards markets ending later
                            fair = self.compute_fair(orderbook)
                            if fair is None:
                                self.logger.warning(f"Failed to compute fair price for {tkr}")
                                continue
                            EDGE_MIN = 0.01
                            bid, ask = self.compute_quotes(mkt_bid, mkt_ask, inventory, allow_improvement=allow_improvement, min_width=self.min_quote_width, block_bid_for_lip=block_bid_for_lip)
                            allow_bid = (fair - bid) >= EDGE_MIN
                            allow_ask = (inventory > 0)

                            if block_bid_for_lip:
                                allow_bid = False
                            
                            if inventory == 0 and (not allow_bid) and (not allow_ask):
                                self.logger.info(f"[DISCOVERY] Skipping {tkr}: LIP target met at best; no scoring opportunity.")
                                continue

                            self.manage_orders(bid, ask, spread, tkr, inventory, side, allow_bid=allow_bid, allow_ask=allow_ask)

                            if allow_improvement and inventory == 0 and spread >= 0.02:
                                self._improved_on_touch[key] = True
                                self._last_improve_ts[key] = now_ts
                            tracked_markets.setdefault(tkr, {})[side] = True
                            added += 1
                            self.logger.info(f"Started tracking market {tkr} [YES only - avoiding duplicate positions]")
                        except Exception as e:
                            self.logger.warning(f"Failed to initialize market {tkr}: {e}")


                    if added == 0:
                        self.logger.info("[DISCOVERY] No eligible markets found this cycle "
                     f"(scanned={scanned}, cap={self.discovery_scan_cap}).")

                    last_discovery_ts = time.time()

                # 3) Periodically check PnL and inventory imbalance
                if (time.time() - last_pnl_check_ts) >= 60.0:  # Check every minute
                    try:
                        total_pnl = self._calculate_total_pnl(tracked_markets)
                        self.circuit_breaker.check_pnl(total_pnl)
                        
                        # Check inventory imbalance across all tracked markets (excluding resolved markets)
                        for ticker in tracked_markets.keys():
                            try:
                                inventory = self.api.get_position(ticker)
                                
                                # Check if market is resolved - skip inventory check if it is
                                try:
                                    is_resolved, _ = self.resolved_from_bids(ticker)
                                    
                                    if is_resolved:
                                        self.logger.debug(f"Skipping inventory check for resolved market {ticker}")
                                        continue
                                except Exception as e:
                                    self.logger.debug(f"Could not check if {ticker} is resolved, will check inventory anyway: {e}")
                                
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

        # Stop WebSocket fill tracker before shutting down
        if self.ws_fill_tracker:
            self.ws_fill_tracker.stop()
            
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
        """Calculate total PnL across all tracked markets using position_tracker."""
        total_pnl = 0.0
        tickers = list(tracked_markets.keys())

        with self._position_lock:
            # Only consider tickers we actually have tracking for
            for ticker in tickers:
                pos = self.position_tracker.get(ticker)
                if not pos:
                    continue

                inv = int(pos["inventory"])
                avg_price = float(pos["avg_price"])
                realized_pnl = float(pos["realized_pnl"])

                # Current yes mid (fallback = avg_price if something fails)
                try:
                    prices = self.api.get_price(ticker)
                    current_price = float(prices.get("yes", avg_price or 0.5))
                except Exception:
                    current_price = avg_price or 0.5

                # Unrealized PnL in YES-equivalent space
                unrealized_pnl = (current_price - avg_price) * inv if inv != 0 else 0.0

                if self.metrics:
                    self.metrics.record_pnl_snapshot(
                        ticker=ticker,
                        realized_pnl=realized_pnl,
                        unrealized_pnl=unrealized_pnl,
                        inventory=inv,
                        position_value=current_price * inv,
                    )

                total_pnl += realized_pnl + unrealized_pnl

        return total_pnl


    def export_metrics(self) -> None:
        if self.metrics is None:
            return
        # Safe file prefix based on logger name
        strategy_name = getattr(self.logger, 'name', 'Strategy')
        safe_name = strategy_name.replace(':', '_').replace(' ', '_')
        base_prefix = f"{safe_name}"
        self.metrics.export_files(base_prefix)

    def compute_fair(self, orderbook: Dict):
        yes_bids = [(to_tick(p), sz) for (p, sz) in (orderbook.get("var_true") or [])]
        yes_asks = [(to_tick(1.0 - p), sz) for (p, sz) in (orderbook.get("var_false") or [])]

        if not yes_bids or not yes_asks:
            return None
        
        try:
            y_best_bid = max(p for p, _ in yes_bids)
            y_best_ask = min(p for p, _ in yes_asks)
        except Exception as e:
            self.logger.warning(f"Failed to compute fair price for {orderbook}: {e}")
            return None

        y_bid_sz = sum(int(sz) for p, sz in yes_bids if p == y_best_bid)
        y_ask_sz = sum(int(sz) for p, sz in yes_asks if p == y_best_ask)

        yes_mid = to_tick((y_best_bid + y_best_ask) / 2.0)
        if (y_bid_sz + y_ask_sz) > 0:
            micro_yes = to_tick((y_best_ask * y_bid_sz + y_best_bid * y_ask_sz) / (y_bid_sz + y_ask_sz))
        else:
            micro_yes = yes_mid
        
        return yes_mid * 0.35 + micro_yes * 0.65


    def compute_quotes(self, touch_bid, touch_ask, inventory, theta=0.005, allow_improvement: bool = True, min_width: float = 0.0, block_bid_for_lip: bool = False):
        bid = touch_bid
        ask = touch_ask
        skew = theta * inventory * max(0.01, touch_ask - touch_bid)
        spread = max(0.0, touch_ask - touch_bid)

        # When LIP target is met, don't modify the bid at all - just use touch
        if block_bid_for_lip:
            bid = touch_bid
            # Still allow ask modifications for inventory exit
            if inventory > 0:
                ask = touch_ask  # Use touch for faster exit
            else:
                # No inventory and target met - use touch as-is
                ask = touch_ask
            return bid, ask

        # inventory skew - but don't skew asks when we have inventory (we want to exit at touch)
        bid = to_tick(max(0.02, bid - skew))
        # Only skew ask upward when we DON'T have inventory (normal market making)
        # When we have inventory, keep ask at the touch for faster exits
        if inventory <= 0:
            ask = to_tick(min(0.98, ask + skew))

        # ensure minimum width
        want_width = max(min_width, 0.0)
        cur_width = max(0.0, ask - bid)
        if cur_width < want_width:
            # widen around the mid of current quotes to meet width
            mid = (bid + ask) / 2.0
            half = want_width / 2.0
            bid = to_tick(max(0.02, mid - half))
            ask = to_tick(min(0.98, mid + half))
            cur_width = ask - bid

        # gentle improvement logic
        # When we have inventory, don't widen the ask - keep it at touch for faster exits
        if spread < 0.03 and not block_bid_for_lip:
            bid = to_tick(max(0.02, bid - 0.01))
            # Only widen ask if we don't have inventory to exit
            if inventory <= 0:
                ask = to_tick(min(0.98, ask + 0.01))
        else:
            # Allow improvement logic for both flat and inventory positions
            if allow_improvement and spread >= 0.04:
                bid = min(to_tick(bid + 0.01), to_tick(ask - 0.01))
                # When we have inventory, tighten the ask to the touch
                if inventory == 0:
                    ask = max(to_tick(ask - 0.01), to_tick(bid + 0.01))
                else:
                    # Keep ask at touch when we have inventory
                    ask = to_tick(touch_ask)

        # re-enforce min width after tweaks
        if (ask - bid) < want_width:
            mid = (bid + ask) / 2.0
            half = want_width / 2.0
            bid = to_tick(max(0.02, mid - half))
            ask = to_tick(min(0.98, mid + half))

        return bid, ask

    def compute_desired_size(self, ticker: str, side: str, action: str, price: float, spread: float, inventory: int, min_order_size: int = 1) -> int:
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
        hrs = self._hours_to_expiry(ticker)
        if hrs is None:
            time_factor = 1.0
        else:
            # Full size when far from expiry, fades to 0 as you approach 6h
            cutoff = 6.0  # hours
            time_factor = max(0.0, min(1.0, hrs / cutoff))

        base_size = int(self.max_position * 0.2 * inv_factor * spread_factor * time_factor)

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


    def _best_bid(self, levels):
        """Extract best bid from orderbook levels [(price, count), ...]"""
        if not levels:
            return None
        prices = [float(p) for p, cnt in levels if p is not None and (cnt or 0) > 0]
        return max(prices) if prices else None

    def resolved_from_bids(self, ticker: str):
        """
        Determine if market is resolved based on yes mid price.
        Returns (resolved: bool, side: 'yes'|'no'|None).
        """
        EDGE_HIGH = 0.95  # treat ≥95c as "yes"
        EDGE_LOW = 0.05   # treat ≤5c as "no"
        
        try:
            prices = self.api.get_price(ticker)
            yes_mid = prices.get("yes")
            
            self.logger.info(f"Yes mid price: {yes_mid}")
            
            if yes_mid is not None:
                if yes_mid >= EDGE_HIGH:
                    return True, "yes"
                elif yes_mid <= EDGE_LOW:
                    return True, "no"
        except Exception as e:
            self.logger.error(f"Error getting price for {ticker}: {e}")
        
        return False, None
    
    def fast_move(self, prev, now):
        if not prev or not now:
            return False
        pb, pa = prev; nb, na = now
        return (abs(nb - pb) > 0.01) or (abs(na - pa) > 0.01)
    
    def thin_book(self, orderbook: Dict, min_lvl_size=200, levels=2) -> bool:
        yt = sorted(orderbook.get("var_true", []))  # asks low→high
        nf = sorted(orderbook.get("var_false", []), reverse=True)  # bids high→low
        def has_depth(side):
            return sum(sz for _,sz in side[:levels]) >= min_lvl_size
        return (not has_depth(yt)) or (not has_depth(nf))
        
    def check_and_cashout_resolved_market(self, ticker: str, side: str, mkt_bid: float, mkt_ask: float, inventory: int) -> bool:
        """
        Check if market is resolved based on yes mid price and cash out position if needed.
        Returns True if we cashed out (or attempted to), False otherwise.
        """
        is_resolved, resolved_side = self.resolved_from_bids(ticker)
        cashout_action = None
        cashout_price = None

        self.logger.info(f"Checking if market is resolved: {ticker}, {side}, {mkt_bid}, {mkt_ask}, {inventory}")
        self.logger.info(f"Resolved check result: is_resolved={is_resolved}, resolved_side={resolved_side}")
        
        if is_resolved and resolved_side:
            # Market is resolved to a specific side
            if resolved_side == "yes":
                # Market resolved to YES
                if inventory > 0:
                    # We have YES position - sell at market (best bid)
                    cashout_action = "sell"
                    cashout_price = mkt_bid
                elif inventory < 0:
                    # We have NO position - buy it back at market to close (loss scenario)
                    cashout_action = "buy"
                    cashout_price = mkt_ask
            elif resolved_side == "no":
                # Market resolved to NO
                if inventory < 0:
                    # We have NO position - this is a win, buy back at market
                    cashout_action = "buy"
                    cashout_price = mkt_ask
                elif inventory > 0:
                    # We have YES position - sell at market (loss scenario)
                    cashout_action = "sell"
                    cashout_price = mkt_bid

        self.logger.info(f"Is resolved: {is_resolved}, Resolved side: {resolved_side}, Cashout action: {cashout_action}, Cashout price: {cashout_price}")
        
        # If resolved with conflicting signals, skip trading but don't cashout
        if is_resolved and resolved_side is None:
            self.logger.info(f"⚠️  CONFLICTING SIGNALS for {ticker} - treating as resolved, skipping trading")
            return True
        
        if is_resolved and inventory != 0 and cashout_action and cashout_price:
            resolved_label = f"to {resolved_side.upper()}" if resolved_side else "conflicting signals"
            self.logger.info(f"🎯 RESOLVED MARKET DETECTED: {ticker} {resolved_label}")
            self.logger.info(f"   Inventory: {inventory}, Bid: {mkt_bid:.2f}, Ask: {mkt_ask:.2f}")
            self.logger.info(f"   Cashing out: {cashout_action} at {cashout_price:.2f}")
            
            # First, cancel all existing orders for this market
            try:
                current_orders = self.api.get_orders(ticker) or []
                for o in current_orders:
                    try:
                        self.api.cancel_order(o["order_id"])
                        self.logger.info(f"   Canceled order {o['order_id']} before cashout")
                        if self.metrics:
                            self.metrics.record_order_canceled(o["order_id"], ticker, o.get('side', side), 0, o.get('remaining_count', 0))
                    except Exception as e:
                        self.logger.warning(f"   Failed to cancel order before cashout: {e}")
            except Exception as e:
                self.logger.warning(f"   Failed to fetch orders before cashout: {e}")
            
            # Now place the cashout order
            try:
                cashout_size = abs(inventory)
                
                if self.metrics:
                    self.metrics.record_order_sent(ticker, side, cashout_action, cashout_price, cashout_size)
                
                oid = self.api.place_order(ticker, cashout_action, side, cashout_price, cashout_size, None)
                self.logger.info(f"   ✅ Cashout order placed: {cashout_action} {cashout_size} @ {cashout_price:.2f}, order_id: {oid}")
                
                if self.metrics:
                    self.metrics.record_order_acknowledged(oid, ticker, side, cashout_action, cashout_price, cashout_size)
                    self.metrics.record_action("cashout_resolved", {
                        "action": cashout_action,
                        "side": side,
                        "price": cashout_price,
                        "size": cashout_size,
                        "inventory": inventory,
                        "market_bid": mkt_bid,
                        "market_ask": mkt_ask
                    })
                
                self.circuit_breaker.record_success()
                return True
                
            except Exception as e:
                self.logger.error(f"   ❌ Failed to place cashout order: {e}")
                if self.metrics:
                    self.metrics.record_order_rejected(ticker, side, cashout_action, cashout_price, cashout_size, str(e))
                    self.metrics.record_api_error("cashout_order", str(e), "place_order")
                self.circuit_breaker.record_error("cashout_order", str(e))
                return True  # Still return True to skip normal order management
        
        return False  # Not a resolved market or no position to cash out

    def manage_orders(self, bid: float, ask: float, spread: float, ticker: str, inventory: int, side: str, allow_bid: bool = True, allow_ask: bool = True):
        # does it ever exit markets?
        current_orders = self.api.get_orders(ticker) or []

        buy_size = self.compute_desired_size(ticker, side, "buy", bid, spread, inventory)
        sell_size = inventory

        ema = self._markout_ema.get(ticker, 0.0)
        first_phase = (ema is None)  # no markout seen yet

        if first_phase:
            # Hard-cap buy size to tiny for first trade(s)
            buy_size = min(buy_size, max(1, int(0.01 * self.max_position)))
            self.logger.info(f"{ticker}: first phase, hard-capping buy size to {buy_size}")

        very_bad = self.mo_bad_threshold * 3.0  # e.g. -0.009 if threshold = -0.003

        if ema is not None and ema <= self.mo_bad_threshold:
            # mildly / moderately toxic → shrink size
            scale = 0.25 if ema > very_bad else 0.0  # 25% size, or 0 for really awful
            old_buy = buy_size
            buy_size = int(buy_size * scale)
            self.logger.info(
                f"{ticker}: toxic flow ema={ema:.4f} ≤ {self.mo_bad_threshold:.4f} → "
                f"scaling buy_size {old_buy} → {buy_size}"
            )

            if ema <= very_bad:
                # also kill new bids entirely, only allow exit via asks
                allow_bid = False

        # if we have zero buy size after scaling, don't bother placing bids
        if buy_size <= 0:
            allow_bid = False

        # Stop buying when inventory exceeds threshold to prioritize exiting position
        # This balances liquidity provision (earning rewards) with risk management
        inventory_threshold = int(self.max_position * self.inventory_buy_threshold)
        if inventory > inventory_threshold:
            buy_size = 0
            self.logger.debug(f"Inventory {inventory} exceeds threshold {inventory_threshold} ({self.inventory_buy_threshold*100:.0f}% of {self.max_position}), stopping buy orders")

        # Partition by action for THIS side only
        buy_orders  = [o for o in current_orders if o.get("side")==side and o.get("action")=="buy"]
        sell_orders = [o for o in current_orders if o.get("side")==side and o.get("action")=="sell"]

        def _px(o):
            raw = o.get("yes_price") if side=="yes" else o.get("no_price")
            f = float(raw)
            return to_tick(f/100.0 if f>1.0 else f)

        # If we have inventory, cancel ALL buy orders
        # Otherwise, keep only 1 best buy at bid; cancel others

        if not allow_bid:
            self.logger.debug(f"{ticker}: bid blocked by edge (bid={bid:.2f})")
            for o in buy_orders:
                try:
                    self.api.cancel_order(o["order_id"])
                    self.logger.info(f"{ticker}: cancel buy {_px(o)} blocked by edge")
                    if self.metrics:
                        self.metrics.record_order_canceled(o["order_id"], ticker, side, _px(o), o.get('remaining_count', 0))
                except Exception as e:
                    self.logger.error(f"Cancel buy failed: {e}")
            buy_size = 0  # prevent new buy placement


        # If flat and no ask edge, we won’t place a new ask; we still allow asks if inventory>0 to exit
        if inventory == 0 and not allow_ask:
            self.logger.debug(f"{ticker}: ask blocked by edge while flat (ask={ask:.2f})")
            for o in sell_orders:
                try:
                    self.api.cancel_order(o["order_id"])
                    self.logger.info(f"{ticker}: cancel sell {_px(o)} blocked by edge (flat)")
                    if self.metrics:
                        self.metrics.record_order_canceled(o["order_id"], ticker, side, _px(o), o.get('remaining_count', 0))
                except Exception as e:
                    self.logger.error(f"Cancel sell failed: {e}")
            sell_size = 0  # prevent new sell placement when flat


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
            # Prevent placing new buy orders when we have inventory
            buy_size = 0
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
        self.logger.info(f"Ticker: {ticker}, Side: {side}, Inventory: {inventory}, keep_sell: {keep_sell}, sell_size: {sell_size}")
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
