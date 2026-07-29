from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from config import StrategyBConfig
from modules.data_fetcher import DataFetcher
from modules.exchange import ExchangeClient
from modules.indicators import compute_all_indicators, compute_rsi

logger = logging.getLogger(__name__)


class GridStrategy:
    """Strategy B: Dynamic Grid Trading.

    1. Calculate EMA_20 + ATR_14 on 1h klines
    2. Upper band = EMA_20 + 2.5*ATR, Lower band = EMA_20 - 2.5*ATR
    3. Place 12 grid limit orders (buy below, sell above)
    4. On buy fill, place sell order one grid level up
    5. Every 1h close, recalculate bands and shift grid
    6. Trend protection: pause if RSI > 75 or < 25
    7. Global stop: if price < EMA_20 - 3.5*ATR, close all
    """

    def __init__(self, exchange: ExchangeClient, data_fetcher: DataFetcher, config: StrategyBConfig):
        self.exchange = exchange
        self.data_fetcher = data_fetcher
        self.config = config

        # Grid state per symbol
        self.grid_states: dict[str, dict] = {}
        # Format: { symbol: { upper, lower, center, grid_spacing, orders: [], paused, ... } }

    def _compute_bands(self, symbol: str) -> Optional[dict]:
        """Compute grid bands from 1h klines."""
        try:
            required = max(self.config.ema_period, self.config.atr_period) + 10
            df = self.data_fetcher.get_ohlcv_df(symbol, "1h", limit=required)

            if df is None or len(df) < required:
                return None

            ind_config = {
                "rsi_period": self.config.rsi_period,
                "ema_period": self.config.ema_period,
                "atr_period": self.config.atr_period,
            }
            df = compute_all_indicators(df, ind_config)
            df = df.dropna(subset=["EMA_20", "ATR_14", "RSI"])

            if len(df) == 0:
                return None

            latest = df.iloc[-1]
            ema = latest["EMA_20"]
            atr = latest["ATR_14"]
            rsi = latest["RSI"]
            current_price = latest["close"]

            upper = ema + (self.config.band_multiplier * atr)
            lower = ema - (self.config.band_multiplier * atr)
            grid_spacing = (upper - lower) / self.config.grid_count

            # Global stop level
            global_stop = ema - (self.config.stop_multiplier * atr)

            return {
                "ema": ema,
                "atr": atr,
                "rsi": rsi,
                "current_price": current_price,
                "upper": upper,
                "lower": lower,
                "grid_spacing": grid_spacing,
                "global_stop": global_stop,
            }
        except Exception as e:
            logger.error(f"Error computing bands for {symbol}: {e}")
            return None

    def check_trend_protection(self, symbol: str) -> bool:
        """Check if trend protection should pause grid trading."""
        bands = self._compute_bands(symbol)
        if bands is None:
            return True  # Pause if can't compute

        rsi = bands["rsi"]
        if rsi >= self.config.rsi_overbought or rsi <= self.config.rsi_oversold:
            logger.info(
                f"TREND PROTECTION: {symbol} paused | RSI={rsi:.1f} "
                f"(overbought={self.config.rsi_overbought}, oversold={self.config.rsi_oversold})"
            )
            return True

        return False

    def check_global_stop(self, symbol: str) -> bool:
        """Check if price has hit global stop-loss level."""
        bands = self._compute_bands(symbol)
        if bands is None:
            return False

        if bands["current_price"] <= bands["global_stop"]:
            logger.warning(
                f"GLOBAL STOP: {symbol} | Price {bands['current_price']:.4f} "
                f"<= Stop {bands['global_stop']:.4f}"
            )
            return True

        return False

    def setup_grid(self, symbol: str, allocated_capital: float) -> Optional[dict]:
        """Set up a complete grid for a symbol."""
        try:
            # Check trend protection first
            if self.check_trend_protection(symbol):
                self.grid_states[symbol] = {
                    "paused": True,
                    "reason": "trend_protection",
                }
                return {"status": "paused", "reason": "trend_protection"}

            bands = self._compute_bands(symbol)
            if bands is None:
                return None

            # Check max grids
            active_grids = {s for s, st in self.grid_states.items() if not st.get("paused")}
            if len(active_grids) >= self.config.max_open_positions and symbol not in active_grids:
                logger.warning(f"Max grid positions reached, skip {symbol}")
                return None

            # Set leverage
            self.exchange.set_leverage(symbol, self.config.leverage)
            self.exchange.set_margin_mode(symbol, self.config.margin_mode)

            # Cancel any existing orders for this symbol
            self.exchange.cancel_all_orders(symbol)

            # Calculate grid levels
            grid_prices = []
            for i in range(self.config.grid_count + 1):
                price = bands["lower"] + (i * bands["grid_spacing"])
                grid_prices.append(round(price, 8))

            # Capital per grid level
            position_value = allocated_capital * self.config.position_size_pct
            leverage = self.config.leverage
            notional = position_value * leverage

            # Get contract size
            markets = self.exchange.exchange.markets
            contract_size = markets.get(symbol, {}).get("contractSize", 1)

            placed_orders = []

            # Place limit buy orders below current price
            for i, price in enumerate(grid_prices):
                if price >= bands["current_price"]:
                    continue  # Only buy below current price

                amount = notional / (price * contract_size)
                try:
                    order = self.exchange.create_limit_order(symbol, "buy", amount, price)
                    placed_orders.append({
                        "order_id": order.get("id"),
                        "side": "buy",
                        "price": price,
                        "amount": amount,
                        "grid_level": i,
                    })
                except Exception as e:
                    logger.error(f"Failed to place buy order at level {i}: {e}")

            # Place limit sell orders above current price (if holding position)
            # For initial setup, we only place buy orders

            self.grid_states[symbol] = {
                "upper": bands["upper"],
                "lower": bands["lower"],
                "center": bands["ema"],
                "atr": bands["atr"],
                "grid_spacing": bands["grid_spacing"],
                "grid_prices": grid_prices,
                "global_stop": bands["global_stop"],
                "orders": placed_orders,
                "filled_buys": [],  # track filled buy orders
                "paused": False,
                "updated_at": datetime.utcnow().isoformat(),
            }

            logger.info(
                f"GRID SETUP: {symbol} | Center={bands['ema']:.4f} | "
                f"Range=[{bands['lower']:.4f}, {bands['upper']:.4f}] | "
                f"Spacing={bands['grid_spacing']:.6f} | Orders={len(placed_orders)}"
            )

            return {
                "status": "active",
                "symbol": symbol,
                "upper": bands["upper"],
                "lower": bands["lower"],
                "center": bands["ema"],
                "orders_placed": len(placed_orders),
            }

        except Exception as e:
            logger.error(f"Error setting up grid for {symbol}: {e}")
            return None

    def update_grid(self, symbol: str, allocated_capital: float) -> Optional[dict]:
        """Recalculate and update grid on 1h kline close."""
        state = self.grid_states.get(symbol)
        if not state or state.get("paused"):
            return None

        try:
            bands = self._compute_bands(symbol)
            if bands is None:
                return None

            # Check if center has shifted significantly (> 0.5 * grid_spacing)
            center_shift = abs(bands["ema"] - state["center"])
            shift_threshold = state["grid_spacing"] * 0.5

            if center_shift < shift_threshold:
                logger.debug(f"Grid {symbol}: center shift {center_shift:.6f} < threshold {shift_threshold:.6f}, no update")
                return None

            logger.info(
                f"GRID UPDATE: {symbol} | Center shifted from {state['center']:.4f} to {bands['ema']:.4f}"
            )

            # Cancel all far-end orders (those outside new bands)
            for order_info in state["orders"]:
                price = order_info["price"]
                if price < bands["lower"] or price > bands["upper"]:
                    try:
                        self.exchange.cancel_order(order_info["order_id"], symbol)
                        logger.debug(f"  Cancelled far-end order: {order_info['order_id']} @ {price}")
                    except Exception as e:
                        logger.debug(f"  Failed to cancel order {order_info['order_id']}: {e}")

            # Re-setup the grid with new center
            return self.setup_grid(symbol, allocated_capital)

        except Exception as e:
            logger.error(f"Error updating grid for {symbol}: {e}")
            return None

    def check_filled_orders(self, symbol: str, allocated_capital: float) -> list[dict]:
        """Check if any grid orders were filled and place corresponding sell orders."""
        state = self.grid_states.get(symbol)
        if not state or state.get("paused"):
            return []

        actions = []
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            open_order_ids = {o["id"] for o in open_orders}

            for order_info in state["orders"]:
                oid = order_info["order_id"]
                if oid in open_order_ids:
                    continue  # Still open

                if oid in [f["order_id"] for f in state["filled_buys"]]:
                    continue  # Already processed

                # This order was filled (not open and not already processed)
                if order_info["side"] == "buy":
                    # Find next grid level up for sell
                    current_level = order_info["grid_level"]
                    sell_level = current_level + 1

                    if sell_level < len(state["grid_prices"]):
                        sell_price = state["grid_prices"][sell_level]
                        amount = order_info["amount"]

                        sell_order = self.exchange.create_limit_order(
                            symbol, "sell", amount, sell_price
                        )

                        actions.append({
                            "action": "GRID_FILL",
                            "symbol": symbol,
                            "buy_price": order_info["price"],
                            "sell_price": sell_price,
                            "sell_order_id": sell_order.get("id"),
                            "amount": amount,
                        })

                        state["filled_buys"].append(order_info)

                        logger.info(
                            f"GRID FILL: {symbol} | Bought @ {order_info['price']:.4f} | "
                            f"Sell order placed @ {sell_price:.4f}"
                        )

        except Exception as e:
            logger.error(f"Error checking filled orders for {symbol}: {e}")

        return actions

    def close_all_grids(self) -> list[dict]:
        """Emergency close all grid positions and cancel orders."""
        results = []
        for symbol in list(self.grid_states.keys()):
            try:
                # Cancel all open orders
                self.exchange.cancel_all_orders(symbol)
                # Close any position
                self.exchange.close_position(symbol)
                results.append({"symbol": symbol, "status": "closed"})
                logger.info(f"Emergency grid close: {symbol}")
            except Exception as e:
                results.append({"symbol": symbol, "status": "error", "error": str(e)})
                logger.error(f"Emergency grid close failed for {symbol}: {e}")
        self.grid_states.clear()
        return results

    def get_active_symbols(self) -> list[str]:
        """Get list of symbols with active grids."""
        return [s for s, st in self.grid_states.items() if not st.get("paused")]