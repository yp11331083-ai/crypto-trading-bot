from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from config import StrategyAConfig
from modules.data_fetcher import DataFetcher
from modules.exchange import ExchangeClient
from modules.indicators import compute_all_indicators

logger = logging.getLogger(__name__)


class BreakoutStrategy:
    """Strategy A: High-Momentum Breakout.

    1. Hourly scan: find hot symbols (volume spike >= 200%, amplitude >= 8%)
    2. On 15m klines: price > 24h high, volume > 2.5x avg, RSI > 65
    3. Long entry with 5x isolated leverage
    4. Hard stop-loss: -1.5% from entry
    5. Trailing stop: activates at +2% unrealized, trails by 1x ATR or 1%
    """

    def __init__(self, exchange: ExchangeClient, data_fetcher: DataFetcher, config: StrategyAConfig):
        self.exchange = exchange
        self.data_fetcher = data_fetcher
        self.config = config
        self.active_positions: dict[str, dict] = {}  # symbol -> position info
        self.scanned_hot_symbols: list[str] = []

    def scan_hot_symbols(self) -> list[str]:
        """Step 1: Scan all USDT-M perpetuals for hot targets."""
        all_symbols = self.exchange.get_usdt_perpetual_symbols()
        self.scanned_hot_symbols = self.data_fetcher.scan_hot_symbols(
            symbols=all_symbols,
            volume_spike_pct=self.config.volume_spike_pct,
            amplitude_pct=self.config.amplitude_pct,
        )
        return self.scanned_hot_symbols

    def check_entry_signal(self, symbol: str) -> Optional[dict]:
        """Check if a symbol has a valid breakout entry signal on 15m klines."""
        try:
            # Need at least lookback_klines + volume_ma_period for indicators
            required = self.config.lookback_klines + self.config.volume_ma_period + 5
            df = self.data_fetcher.get_ohlcv_df(symbol, "15m", limit=required)

            if df is None or len(df) < required:
                return None

            # Compute indicators
            ind_config = {
                "rsi_period": self.config.rsi_period,
                "atr_period": self.config.trailing_atr_period,
                "volume_ma_period": self.config.volume_ma_period,
            }
            df = compute_all_indicators(df, ind_config)

            # Drop rows with NaN indicators
            df = df.dropna(subset=["RSI", "ATR_14", "VOL_MA"])
            if len(df) < self.config.lookback_klines:
                return None

            # Current candle
            current = df.iloc[-1]
            prev_window = df.iloc[-(self.config.lookback_klines + 1):-1]

            # Condition 1: Price breakout - close > 24h highest high
            highest_high = prev_window["high"].max()
            if current["close"] <= highest_high:
                return None

            # Condition 2: Volume explosion - current vol > 2.5x 20-period avg
            if current["volume"] <= self.config.volume_multiplier * current["VOL_MA"]:
                return None

            # Condition 3: RSI confirmation > 65
            if current["RSI"] <= self.config.rsi_threshold:
                return None

            # All conditions met
            signal = {
                "symbol": symbol,
                "side": "long",
                "entry_price": current["close"],
                "highest_high": highest_high,
                "volume_ratio": current["volume"] / current["VOL_MA"],
                "rsi": current["RSI"],
                "atr": current["ATR_14"],
                "timestamp": datetime.utcnow().isoformat(),
            }
            logger.info(
                f"BREAKOUT SIGNAL: {symbol} @ {current['close']:.4f} | "
                f"RSI={current['RSI']:.1f} | VolRatio={signal['volume_ratio']:.2f}x"
            )
            return signal

        except Exception as e:
            logger.error(f"Error checking entry signal for {symbol}: {e}")
            return None

    def execute_entry(self, signal: dict, allocated_capital: float) -> Optional[dict]:
        """Execute a breakout trade entry."""
        symbol = signal["symbol"]

        # Check max positions
        if len(self.active_positions) >= self.config.max_open_positions:
            logger.warning(f"Max positions reached ({self.config.max_open_positions}), skip {symbol}")
            return None

        if symbol in self.active_positions:
            logger.warning(f"Already have position in {symbol}, skip")
            return None

        try:
            # Calculate position size
            position_value = allocated_capital * self.config.position_size_pct
            leverage = self.config.leverage
            entry_price = signal["entry_price"]

            # Get contract size from exchange
            markets = self.exchange.exchange.markets
            if symbol not in markets:
                logger.error(f"Symbol {symbol} not found in markets")
                return None

            contract_size = markets[symbol].get("contractSize", 1)
            # Position size in contracts: (position_value * leverage) / (entry_price * contract_size)
            notional = position_value * leverage
            amount = notional / (entry_price * contract_size)

            # Set leverage and margin mode
            self.exchange.set_leverage(symbol, leverage)
            self.exchange.set_margin_mode(symbol, self.config.margin_mode)

            # Create market buy order
            order = self.exchange.create_market_order(symbol, "buy", amount)

            # Calculate stop-loss price
            stop_loss_price = entry_price * (1 - self.config.stop_loss_pct)

            # Record position
            position_info = {
                "symbol": symbol,
                "side": "long",
                "entry_price": entry_price,
                "amount": amount,
                "leverage": leverage,
                "stop_loss_price": stop_loss_price,
                "trailing_activated": False,
                "highest_price": entry_price,
                "order_id": order.get("id"),
                "signal": signal,
                "entered_at": datetime.utcnow().isoformat(),
            }
            self.active_positions[symbol] = position_info

            logger.info(
                f"ENTRY EXECUTED: {symbol} | Amount={amount:.4f} | "
                f"Entry={entry_price:.4f} | SL={stop_loss_price:.4f} | Leverage={leverage}x"
            )

            return {
                "action": "ENTRY",
                "symbol": symbol,
                "side": "long",
                "price": entry_price,
                "amount": amount,
                "leverage": leverage,
                "stop_loss": stop_loss_price,
            }

        except Exception as e:
            logger.error(f"Failed to execute entry for {symbol}: {e}")
            return None

    def check_exit_signals(self, allocated_capital: float) -> list[dict]:
        """Check all active positions for exit conditions.

        Returns list of exit actions to execute.
        """
        exits = []
        symbols_to_remove = []

        for symbol, pos in self.active_positions.items():
            try:
                # Fetch current price
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker["last"]

                # Update highest price
                if current_price > pos["highest_price"]:
                    pos["highest_price"] = current_price

                # Calculate unrealized PnL percentage
                entry_price = pos["entry_price"]
                leverage = pos["leverage"]
                price_change_pct = (current_price - entry_price) / entry_price
                leveraged_pnl_pct = price_change_pct * leverage

                # ── Hard Stop-Loss Check ──
                if current_price <= pos["stop_loss_price"]:
                    exits.append({
                        "action": "STOP_LOSS",
                        "symbol": symbol,
                        "reason": f"Hard SL hit: {current_price:.4f} <= {pos['stop_loss_price']:.4f}",
                        "pnl_pct": leveraged_pnl_pct,
                    })
                    symbols_to_remove.append(symbol)
                    continue

                # ── Trailing Stop Check ──
                if leveraged_pnl_pct >= self.config.trailing_activate_pct * leverage:
                    pos["trailing_activated"] = True

                if pos["trailing_activated"]:
                    atr = pos["signal"]["atr"]
                    # Trail by 1x ATR or 1% from highest price
                    trail_atr = pos["highest_price"] - (self.config.trailing_atr_multiplier * atr)
                    trail_pct = pos["highest_price"] * (1 - self.config.trailing_fallback_pct)
                    trail_price = max(trail_atr, trail_pct)

                    if current_price <= trail_price:
                        exits.append({
                            "action": "TRAILING_STOP",
                            "symbol": symbol,
                            "reason": f"Trailing stop: {current_price:.4f} <= trail {trail_price:.4f}",
                            "pnl_pct": leveraged_pnl_pct,
                        })
                        symbols_to_remove.append(symbol)
                        continue

            except Exception as e:
                logger.error(f"Error checking exit for {symbol}: {e}")
                continue

        return exits, symbols_to_remove

    def execute_exit(self, exit_signal: dict) -> Optional[dict]:
        """Execute a position exit (market close)."""
        symbol = exit_signal["symbol"]
        try:
            pos = self.active_positions.get(symbol)
            if not pos:
                logger.warning(f"No active position found for {symbol}")
                return None

            result = self.exchange.close_position(symbol)

            logger.info(
                f"EXIT EXECUTED: {symbol} | {exit_signal['action']} | "
                f"Reason: {exit_signal['reason']} | PnL: {exit_signal['pnl_pct']:.2f}%"
            )

            return {
                "action": exit_signal["action"],
                "symbol": symbol,
                "pnl_pct": exit_signal["pnl_pct"],
            }

        except Exception as e:
            logger.error(f"Failed to execute exit for {symbol}: {e}")
            return None

    def close_all_positions(self) -> list[dict]:
        """Emergency close all active positions."""
        results = []
        for symbol in list(self.active_positions.keys()):
            try:
                self.exchange.close_position(symbol)
                self.exchange.cancel_all_orders(symbol)
                results.append({"symbol": symbol, "status": "closed"})
                logger.info(f"Emergency close: {symbol}")
            except Exception as e:
                results.append({"symbol": symbol, "status": "error", "error": str(e)})
                logger.error(f"Emergency close failed for {symbol}: {e}")
        self.active_positions.clear()
        return results
