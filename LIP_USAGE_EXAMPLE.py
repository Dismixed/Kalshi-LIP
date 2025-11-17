"""
LIP Risk-Based Quoting - Usage Example

This file demonstrates how to use the new LIP risk-based quoting functionality
in your market making strategy.
"""

import os
import logging
from mm import LIPBot, KalshiTradingAPI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LIP_Example")


def setup_lip_environment():
    """Configure environment variables for LIP risk-based quoting"""
    # Enable LIP risk-based quoting
    os.environ["LIP_RISK_ENABLED"] = "1"
    
    # LIP parameters
    os.environ["LIP_DISCOUNT_FACTOR"] = "0.95"  # Multiplier decay per tick
    os.environ["LIP_RISK_THRESHOLD"] = "3.0"    # Max risk score before skipping
    os.environ["LIP_RISK_ALPHA"] = "1.0"        # Quote distance scaling
    
    # Risk computation parameters
    os.environ["LIP_TIME_RISK_K"] = "0.15"      # Time decay constant
    os.environ["LIP_VOL_GAMMA"] = "2.0"         # Volatility scaling factor


def example_basic_usage(bot, ticker, orderbook, target_size, inventory):
    """
    Example 1: Basic usage of LIP-adjusted quotes
    """
    logger.info("\n=== Example 1: Basic LIP Quote Computation ===")
    
    # Compute LIP-adjusted quotes
    result = bot.compute_lip_adjusted_quotes(
        ticker=ticker,
        orderbook=orderbook,
        target_size=target_size,
        inventory=inventory,
        discount_factor=0.95,
        risk_threshold=3.0,
        alpha=1.0
    )
    
    # Check if market should be skipped
    if result['skip_reason']:
        logger.info(f"❌ Skipping {ticker}: {result['skip_reason']}")
        return None
    
    # Log results
    logger.info(f"✅ {ticker} - Risk Score: {result['risk_score']:.2f}")
    logger.info(f"   Bid: ${result['bid_price']:.2f} x {result['bid_size']}")
    logger.info(f"   Ask: ${result['ask_price']:.2f} x {result['ask_size']}")
    logger.info(f"   LIP Intensity (bid): {result['lip_intensity_bid']:.2f}")
    logger.info(f"   LIP Intensity (ask): {result['lip_intensity_ask']:.2f}")
    
    return result


def example_qualifying_bands(bot, orderbook, target_size):
    """
    Example 2: Understanding qualifying bands
    """
    logger.info("\n=== Example 2: Qualifying Band Construction ===")
    
    # Extract and sort orderbook levels
    yes_bids = [(0.45, 100), (0.44, 150), (0.43, 200)]
    yes_asks = [(0.55, 80), (0.56, 120), (0.57, 180)]
    
    # Build qualifying bands
    bid_band = bot.build_qualifying_band(
        orderbook_levels=yes_bids,
        target_size=target_size,
        is_bid_side=True,
        discount_factor=0.95
    )
    
    ask_band = bot.build_qualifying_band(
        orderbook_levels=yes_asks,
        target_size=target_size,
        is_bid_side=False,
        discount_factor=0.95
    )
    
    # Display bid band
    if bid_band:
        logger.info(f"Bid qualifying band (target: {target_size}):")
        for level in bid_band:
            logger.info(
                f"  ${level['price']:.2f} x {level['size']:3d} "
                f"(ticks={level['ticks_from_best']}, mult={level['multiplier']:.3f})"
            )
        
        # Compute intensity
        intensity = bot.compute_lip_intensity(bid_band, target_size)
        logger.info(f"  → LIP Intensity: {intensity:.2f}")
        
        if intensity < 0.3:
            logger.info("     📈 Sparse - Good opportunity to quote at top")
        elif intensity <= 3.0:
            logger.info("     ⚖️  Moderate - Normal competitive environment")
        else:
            logger.info("     📊 Crowded - Consider backing off")
    
    return bid_band, ask_band


def example_risk_scoring(bot, ticker):
    """
    Example 3: Understanding risk components
    """
    logger.info("\n=== Example 3: Risk Score Computation ===")
    
    # Compute individual risk components
    time_risk = bot.compute_time_risk(ticker, k=0.15)
    vol_risk = bot.compute_volatility_risk(ticker, lookback_hours=48, ewma_alpha=0.3)
    
    logger.info(f"{ticker} risk components:")
    logger.info(f"  Time Risk: {time_risk:.3f} (higher = closer to expiry)")
    logger.info(f"  Volatility: {vol_risk:.3f} (EWMA of logit returns)")
    
    # Compute combined risk score
    risk_score = bot.compute_risk_score(ticker, vol_percentiles=None, gamma=2.0)
    logger.info(f"  Combined Risk Score: {risk_score:.3f}")
    
    # Interpret risk score
    if risk_score < 1.0:
        risk_level = "LOW 🟢"
        action = "Can quote aggressively at top"
    elif risk_score < 2.0:
        risk_level = "MODERATE 🟡"
        action = "Normal quoting, maybe 1 tick back"
    elif risk_score < 3.0:
        risk_level = "HIGH 🟠"
        action = "Be cautious, sit 2+ ticks back"
    else:
        risk_level = "VERY HIGH 🔴"
        action = "Consider skipping this market"
    
    logger.info(f"  Risk Level: {risk_level}")
    logger.info(f"  → {action}")
    
    return risk_score


def example_quote_level_selection(bot, bid_band, risk_score, inventory):
    """
    Example 4: Quote level determination
    """
    logger.info("\n=== Example 4: Quote Level Determination ===")
    
    if not bid_band:
        logger.warning("No qualifying band available")
        return None
    
    # Determine where to quote based on risk
    chosen_level = bot.determine_quote_level(
        qualifying_band=bid_band,
        risk_score=risk_score,
        alpha=1.0,
        inventory=inventory,
        max_position=100,
        is_bid=True
    )
    
    if chosen_level:
        logger.info(f"Chosen quote level:")
        logger.info(f"  Price: ${chosen_level['price']:.2f}")
        logger.info(f"  Size: {chosen_level['size']}")
        logger.info(f"  Ticks from best: {chosen_level['ticks_from_best']}")
        logger.info(f"  LIP Multiplier: {chosen_level['multiplier']:.3f}")
        
        # Explain why this level was chosen
        max_ticks = int(1.0 * risk_score)  # alpha * risk_score
        logger.info(f"\nReasoning:")
        logger.info(f"  Risk score: {risk_score:.2f}")
        logger.info(f"  Max allowed ticks: {max_ticks}")
        logger.info(f"  Inventory: {inventory} (long positions back off from bids)")
        logger.info(f"  → Chose closest qualifying level within constraints")
    
    return chosen_level


def example_market_selection(bot, candidate_markets):
    """
    Example 5: Market selection and ranking
    """
    logger.info("\n=== Example 5: Market Selection Strategy ===")
    
    market_scores = []
    
    for ticker, orderbook, target_size in candidate_markets:
        try:
            # Extract orderbook levels
            yes_bids = [(0.45, 100), (0.44, 150)]  # Simplified for example
            
            # Build qualifying band
            bid_band = bot.build_qualifying_band(
                orderbook_levels=yes_bids,
                target_size=target_size,
                is_bid_side=True,
                discount_factor=0.95
            )
            
            if not bid_band:
                continue
            
            # Compute metrics
            intensity = bot.compute_lip_intensity(bid_band, target_size)
            risk_score = bot.compute_risk_score(ticker)
            
            # Score: prefer moderate intensity, lower risk
            if 0.3 <= intensity <= 3.0:
                intensity_score = 1.0
            elif intensity < 0.3:
                intensity_score = intensity / 0.3
            else:
                intensity_score = 3.0 / intensity
            
            risk_score_norm = max(0.0, 1.0 - risk_score / 3.0)
            score = intensity_score * 0.5 + risk_score_norm * 0.5
            
            market_scores.append((ticker, score, intensity, risk_score))
            
        except Exception as e:
            logger.warning(f"Failed to score {ticker}: {e}")
    
    # Sort by score
    market_scores.sort(key=lambda x: x[1], reverse=True)
    
    logger.info("Market Rankings:")
    for i, (ticker, score, intensity, risk) in enumerate(market_scores[:5], 1):
        logger.info(
            f"  {i}. {ticker}: score={score:.3f} "
            f"(intensity={intensity:.2f}, risk={risk:.2f})"
        )
    
    return market_scores


def example_integration_pattern(bot, ticker, orderbook, target_size, inventory):
    """
    Example 6: Integration into trading loop
    """
    logger.info("\n=== Example 6: Integration Pattern ===")
    
    # Check if LIP mode is enabled
    if bot.lip_enabled and target_size > 0:
        logger.info("Using LIP risk-adjusted quoting...")
        
        # Compute LIP-adjusted quotes
        lip_result = bot.compute_lip_adjusted_quotes(
            ticker=ticker,
            orderbook=orderbook,
            target_size=target_size,
            inventory=inventory,
            discount_factor=bot.lip_discount_factor,
            risk_threshold=bot.lip_risk_threshold,
            alpha=bot.lip_risk_alpha
        )
        
        if lip_result['skip_reason']:
            logger.info(f"❌ Skipping: {lip_result['skip_reason']}")
            return None
        
        # Extract quote parameters
        bid_price = lip_result['bid_price']
        ask_price = lip_result['ask_price']
        bid_size = lip_result['bid_size']
        ask_size = lip_result['ask_size']
        
        logger.info(f"✅ LIP quotes: bid ${bid_price:.2f} x {bid_size}, ask ${ask_price:.2f} x {ask_size}")
        
        # Place orders (pseudo-code)
        # if bid_price and bid_size > 0:
        #     bot.api.place_order(ticker, "buy", "yes", bid_price, bid_size)
        # if ask_price and ask_size > 0:
        #     bot.api.place_order(ticker, "sell", "yes", ask_price, ask_size)
        
    else:
        logger.info("LIP disabled or no target size, using standard quoting...")
        # Fallback to standard compute_quotes method
        # bid, ask = bot.compute_quotes(mkt_bid, mkt_ask, inventory)
    
    return True


def main():
    """
    Main example runner
    """
    logger.info("=" * 60)
    logger.info("LIP Risk-Based Quoting - Usage Examples")
    logger.info("=" * 60)
    
    # Setup environment
    setup_lip_environment()
    
    # Note: You would initialize these with real API credentials
    # api = KalshiTradingAPI(email="...", password="...", base_url="...", logger=logger)
    # bot = LIPBot(logger=logger, api=api, max_position=100)
    
    logger.info("\n⚠️  These are conceptual examples showing the API usage.")
    logger.info("⚠️  Replace with real API initialization for production use.")
    
    # Example parameters
    ticker = "EXAMPLE-TICKER"
    target_size = 300
    inventory = 50
    
    # Example orderbook (simplified)
    orderbook = {
        "var_true": [(0.45, 100), (0.44, 150), (0.43, 200)],
        "var_false": [(0.55, 80), (0.56, 120), (0.57, 180)]
    }
    
    logger.info("\nFor full integration, see:")
    logger.info("  1. LIP_RISK_FRAMEWORK.md - Comprehensive documentation")
    logger.info("  2. mm.py - Implementation in LIPBot class")
    logger.info("  3. Key methods:")
    logger.info("     - build_qualifying_band()")
    logger.info("     - compute_lip_intensity()")
    logger.info("     - compute_time_risk()")
    logger.info("     - compute_volatility_risk()")
    logger.info("     - compute_risk_score()")
    logger.info("     - determine_quote_level()")
    logger.info("     - compute_lip_adjusted_quotes() [main method]")


if __name__ == "__main__":
    main()

