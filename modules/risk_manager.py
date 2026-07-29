from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from config import AppConfig, RiskConfig
from modules.exchange import ExchangeClient

logger = logging.getLogger(__name__)


class RiskManager:
    """Global Risk Management Module.

    Features:
    - Daily drawdown circuit breaker (-5% equity)
    - Max total exposure check
    - Cooldown timer after circuit break
    - Equity tracking
    """

    def __init__(self, exchange: ExchangeClient, config: RiskConfig):
        self.exchange = exchange
        self.config = config
        self._circuit_broken = False
        self._circuit_break_time: Optional[datetime] = None
        self._daily_start_equity: Optional[float] = None
        self._last_equity_check_date: Optional[str] = None

    def initialize_daily_equity(self, allocated_capital: float):
        """Set the starting equity for the current day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_equity_check_date != today:
            self._daily_start_equity = self._get_total_equity(allocated_capital)
            self._last_equity_check_date = today
            self._circuit_broken = False  # Reset on new day
            logger.info(f"Daily equity initialized: {self._daily_start_equity:.2f} USDT ({today})")

    def _get_total_equity(self, allocated_capital: float) -> float:
        """Get current total equity (balance + unrealized PnL)."""
        try:
            balance = self.exchange.fetch_balance()
            # USDT balance (total equity including unrealized)
            usdt_balance = balance.get("USDT", {})
            total = usdt_balance.get("total", allocated_capital)
            return float(total)
        except Exception as e:
            logger.error(f"Failed to fetch equity: {e}")
            return allocated_capital

    def check_daily_drawdown(self, allocated_capital: float) -> bool:
        """Check if daily drawdown limit has been breached.

        Returns True if circuit should break (drawdown exceeded).
        """
        self.initialize_daily_equity(allocated_capital)

        if self._daily_start_equity is None or self._daily_start_equity == 0:
            return False

        current_equity = self._get_total_equity(allocated_capital)
        drawdown = (current_equity - self._daily_start_equity) / self._daily_start_equity

        logger.debug(
            f"Equity: {current_equity:.2f} | Start: {self._daily_start_equity:.2f} | "
            f"Drawdown: {drawdown*100:.2f}% | Limit: {-self.config.daily_drawdown_limit*100:.1f}%"
        )

        if drawdown <= -self.config.daily_drawdown_limit:
            self._circuit_broken = True
            self._circuit_break_time = datetime.now(timezone.utc)
            logger.critical(
                f"CIRCUIT BREAKER TRIGGERED! Drawdown: {drawdown*100:.2f}% "
                f"> Limit: {-self.config.daily_drawdown_limit*100:.1f}%"
            )
            return True

        return False

    def is_circuit_broken(self) -> bool:
        """Check if circuit breaker is currently active."""
        if not self._circuit_broken:
            return False

        # Check if cooldown has passed
        if self._circuit_break_time:
            elapsed_hours = (datetime.now(timezone.utc) - self._circuit_break_time).total_seconds() / 3600
            if elapsed_hours >= self.config.cooldown_hours:
                self._circuit_broken = False
                self._circuit_break_time = None
                logger.info("Circuit breaker cooldown ended. Trading resumed.")
                return False

        remaining = self.config.cooldown_hours
        if self._circuit_break_time:
            remaining -= (datetime.now(timezone.utc) - self._circuit_break_time).total_seconds() / 3600
        logger.warning(f"Circuit breaker ACTIVE. Remaining cooldown: {remaining:.1f} hours")
        return True

    def check_max_exposure(self, allocated_capital: float) -> bool:
        """Check if total exposure exceeds limit."""
        try:
            positions = self.exchange.fetch_positions()
            total_exposure = 0
            for pos in positions:
                notional = abs(float(pos.get("notional", 0)))
                total_exposure += notional

            max_exposure = allocated_capital * self.config.max_total_exposure_pct
            if total_exposure > max_exposure:
                logger.warning(
                    f"Max exposure reached: {total_exposure:.2f} > {max_exposure:.2f}"
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Error checking exposure: {e}")
            return False

    def should_halt_trading(self, allocated_capital: float) -> tuple[bool, str]:
        """Check if trading should be halted. Returns (halted, reason)."""
        if self.is_circuit_broken():
            return True, "Circuit breaker active (daily drawdown limit breached)"

        if self.check_daily_drawdown(allocated_capital):
            return True, f"Daily drawdown exceeded -{self.config.daily_drawdown_limit*100:.1f}%"

        if self.check_max_exposure(allocated_capital):
            return True, "Max total exposure reached"

        return False, ""

    def emergency_stop_all(self, breakout_strategy=None, grid_strategy=None) -> dict:
        """Emergency stop all trading: cancel orders, close positions."""
        logger.critical("EXECUTING EMERGENCY STOP ALL")

        results = {
            "breakout_closed": [],
            "grids_closed": [],
            "all_orders_cancelled": [],
        }

        if breakout_strategy:
            results["breakout_closed"] = breakout_strategy.close_all_positions()

        if grid_strategy:
            results["grids_closed"] = grid_strategy.close_all_grids()

        return results
