#!/usr/bin/env python3
"""
Quantitative Trading Discord Bot - Main Entry Point
=============================================
Strategy A: High-Momentum Breakout (5x isolated long)
Strategy B: Dynamic Grid Trading (EMA_20 + ATR_14 bands)

Usage:
  python bot.py

Environment variables required (.env file):
  DISCORD_BOT_TOKEN       - Your Discord bot token
  BYBIT_API_KEY           - Bybit API key
  BYBIT_API_SECRET        - Bybit API secret
  TESTNET                 - "true" for testnet, "false" for mainnet
  ALLOCATED_CAPITAL       - Trading capital in USDT (default: 1000)
  DISCORD_LOG_CHANNEL_ID  - (optional) Channel for bot logs
  DISCORD_ALERT_CHANNEL_ID - (optional) Channel for trade alerts
"""
import logging
import os
import sys

from dotenv import load_dotenv

from config import load_config, validate_config
from discord_bot import TradingBot


# ── Logging Setup ───────────────────────────────────────────
def setup_logging(level: str = "INFO"):
    """Configure structured logging."""
    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("trading_bot.log", encoding="utf-8"),
        ],
    )


# ── Main ────────────────────────────────────────────────────
def main():
    # Load .env file
    load_dotenv()

    # Load configuration
    config = load_config()

    # Setup logging
    setup_logging(config.log_level)
    logger = logging.getLogger("main")

    # Validate configuration
    errors = validate_config(config)
    if errors:
        logger.critical("Configuration errors:")
        for err in errors:
            logger.critical(f"  ❌ {err}")
        logger.critical("Please set the required environment variables. See .env.example")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  Quantitative Trading Discord Bot")
    logger.info("=" * 60)
    logger.info(f"  Exchange: {config.exchange.exchange_id} ({'Testnet' if config.exchange.testnet else 'Mainnet'})")
    logger.info(f"  Capital: {config.allocated_capital:.2f} USDT")
    logger.info(f"  Strategy A (Breakout): {'ON' if config.strategy_a.enabled else 'OFF'}")
    logger.info(f"  Strategy B (Grid): {'ON' if config.strategy_b.enabled else 'OFF'}")
    logger.info(f"  Daily Drawdown Limit: -{config.risk.daily_drawdown_limit*100:.1f}%")
    logger.info("=" * 60)

    # Create and run bot
    bot = TradingBot(config)

    try:
        logger.info("Starting Discord bot...")
        bot.run(config.discord.token)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Cleanup on shutdown
        logger.info("Shutting down...")
        if bot._ready:
            logger.info("Closing all positions and orders...")
            bot.risk_manager.emergency_stop_all(bot.breakout, bot.grid)


if __name__ == "__main__":
    main()
