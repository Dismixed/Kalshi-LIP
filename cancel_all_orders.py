#!/usr/bin/env python3
"""
Cancel All Orders Script

This script cancels all resting orders on Kalshi using the Kalshi Python SDK.
It uses the same authentication method as mm.py.

Usage:
    python cancel_all_orders.py

Environment Variables Required:
    - KALSHI_EMAIL: Your Kalshi account email
    - KALSHI_PASSWORD: Your Kalshi account password
    - KALSHI_API_KEY_ID: Your API key ID
    - KALSHI_PRIVATE_KEY_PATH: Path to your private key PEM file
"""

import os
import sys
import logging
from typing import List, Dict
from kalshi_python import Configuration, KalshiClient
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def initialize_client() -> KalshiClient:
    """Initialize and authenticate the Kalshi client"""
    logger.info("Initializing Kalshi client...")
    
    # Check required environment variables
    required_vars = [
        "KALSHI_EMAIL",
        "KALSHI_PASSWORD", 
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY_PATH"
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        sys.exit(1)
    
    # Load private key
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    try:
        with open(private_key_path, "r") as f:
            private_key = f.read()
    except FileNotFoundError:
        logger.error(f"Private key file not found at: {private_key_path}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to read private key file: {e}")
        sys.exit(1)
    
    # Configure the client
    config = Configuration(
        username=os.getenv("KALSHI_EMAIL"),
        password=os.getenv("KALSHI_PASSWORD"),
        access_token=os.getenv("KALSHI_API_KEY_ID"),
    )
    config.api_key_id = os.getenv("KALSHI_API_KEY_ID")
    config.private_key_pem = private_key
    
    # Create client
    client = KalshiClient(config)
    
    # Verify connection by getting balance
    try:
        balance = client.get_balance()
        logger.info(f"Successfully authenticated. Balance: {balance}")
    except Exception as e:
        logger.error(f"Failed to authenticate: {e}")
        sys.exit(1)
    
    return client


def get_all_resting_orders(client: KalshiClient) -> List[Dict]:
    """Get all resting orders from the portfolio"""
    logger.info("Fetching all resting orders...")
    
    try:
        # Try without ticker parameter first (gets all orders)
        api_response = client.get_orders(status="resting")
    except TypeError:
        # SDK may require explicit None for ticker
        try:
            api_response = client.get_orders(ticker=None, status="resting")
        except Exception as e:
            logger.error(f"Failed to fetch orders: {e}")
            return []
    except Exception as e:
        logger.error(f"Failed to fetch orders: {e}")
        return []
    
    # Extract orders from response
    raw_orders = getattr(api_response, "orders", None)
    if raw_orders is None:
        raw_orders = api_response.get("orders", []) if isinstance(api_response, dict) else []
    
    # Convert to list of dicts for easier processing
    orders = []
    for order in raw_orders:
        if isinstance(order, dict):
            orders.append(order)
        else:
            # Convert object to dict
            order_dict = {}
            for attr in ["order_id", "ticker", "side", "action", "yes_price", "no_price", "remaining_count"]:
                order_dict[attr] = getattr(order, attr, None)
            orders.append(order_dict)
    
    logger.info(f"Found {len(orders)} resting orders")
    return orders


def cancel_all_orders(client: KalshiClient, orders: List[Dict]) -> Dict[str, int]:
    """Cancel all orders and return success/failure counts"""
    if not orders:
        logger.info("No orders to cancel")
        return {"success": 0, "failed": 0}
    
    logger.info(f"Canceling {len(orders)} orders...")
    
    success_count = 0
    failed_count = 0
    
    for i, order in enumerate(orders, 1):
        order_id = order.get("order_id")
        ticker = order.get("ticker", "UNKNOWN")
        side = order.get("side", "UNKNOWN")
        action = order.get("action", "UNKNOWN")
        remaining = order.get("remaining_count", 0)
        
        logger.info(f"[{i}/{len(orders)}] Canceling order {order_id} - {ticker} {side} {action} (remaining: {remaining})")
        
        try:
            client.cancel_order(order_id)
            logger.info(f"  ✅ Successfully canceled order {order_id}")
            success_count += 1
        except Exception as e:
            logger.error(f"  ❌ Failed to cancel order {order_id}: {e}")
            failed_count += 1
    
    return {"success": success_count, "failed": failed_count}


def main():
    """Main function to cancel all orders"""
    # Load environment variables from .env file
    load_dotenv()
    
    logger.info("=" * 60)
    logger.info("Kalshi - Cancel All Orders Script")
    logger.info("=" * 60)
    
    # Initialize client
    client = initialize_client()
    
    # Get all resting orders
    orders = get_all_resting_orders(client)
    
    if not orders:
        logger.info("No orders found to cancel. Exiting.")
        return
    
    # Display orders summary
    logger.info("\n" + "=" * 60)
    logger.info("Orders Summary:")
    logger.info("=" * 60)
    
    ticker_summary = {}
    for order in orders:
        ticker = order.get("ticker", "UNKNOWN")
        ticker_summary[ticker] = ticker_summary.get(ticker, 0) + 1
    
    for ticker, count in sorted(ticker_summary.items()):
        logger.info(f"  {ticker}: {count} order(s)")
    
    logger.info("=" * 60)
    
    # Ask for confirmation
    try:
        response = input(f"\nAre you sure you want to cancel all {len(orders)} orders? (yes/no): ")
        if response.lower() not in ["yes", "y"]:
            logger.info("Cancellation aborted by user.")
            return
    except KeyboardInterrupt:
        logger.info("\nCancellation aborted by user.")
        return
    
    # Cancel all orders
    logger.info("")
    results = cancel_all_orders(client, orders)
    
    # Display results
    logger.info("\n" + "=" * 60)
    logger.info("Results:")
    logger.info("=" * 60)
    logger.info(f"  Successfully canceled: {results['success']}")
    logger.info(f"  Failed to cancel: {results['failed']}")
    logger.info(f"  Total: {results['success'] + results['failed']}")
    logger.info("=" * 60)
    
    if results['failed'] > 0:
        logger.warning("Some orders failed to cancel. Check the logs above for details.")
        sys.exit(1)
    else:
        logger.info("✅ All orders successfully canceled!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\nScript interrupted by user. Exiting.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)

