"""
Exchange Wrapper Module
Provides a unified interface to the exchange via ccxt with retry logic.
"""
import logging
import time
from typing import Any, Optional

import ccxt

from config import ExchangeConfig, RiskConfig

logger = logging.getLogger(__name__)


class ExchangeClient:
    """Wrapper around ccxt exchange with retry and error handling."""

    def __init__(self, config: ExchangeConfig, risk_config: RiskConfig):
        self.config = config
        self.risk_config = risk_config
        self.exchange: Optional[ccxt.Exchange] = None
        self._init_exchange()

    def _init_exchange(self):
        """Initialize the exchange client."""
        exchange_class = getattr(ccxt, self.config.exchange_id)

        params = {
            "apiKey": self.config.api_key,
            "secret": self.config.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": self.config.default_type},
        }

        # Demo trading uses ccxt's built-in enable_demo_trading (NOT sandbox/testnet)
        # sandbox=True is for Bybit Testnet only, and will conflict with demo trading
        if self.config.testnet and not self.config.demo_trading:
            params["sandbox"] = True

        self.exchange = exchange_class(params)

        # Use ccxt built-in method for Bybit Demo Trading
        # This correctly sets api-demo.bybit.com URLs internally
        if self.config.demo_trading and self.config.exchange_id == "bybit":
            self.exchange.enable_demo_trading(True)
            logger.info("Demo Trading enabled via ccxt enable_demo_trading(True)")

        logger.info(
            f"Exchange initialized: {self.config.exchange_id} "
            f"(testnet={self.config.testnet}, demo={self.config.demo_trading})"
        )

    def _retry_request(self, func, *args, **kwargs) -> Any:
        """Execute API call with exponential backoff retry."""
        max_retries = self.risk_config.api_retry_max
        base_delay = self.risk_config.api_retry_base_delay

        for attempt in range(1, max_retries + 1):
            try:
                return func(*args, **kwargs)
            except ccxt.RateLimitExceeded as e:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Rate limit hit, retry {attempt}/{max_retries} in {delay:.1f}s: {e}"
                )
                time.sleep(delay)
            except ccxt.NetworkError as e:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Network error, retry {attempt}/{max_retries} in {delay:.1f}s: {e}"
                )
                time.sleep(delay)
            except ccxt.ExchangeError as e:
                logger.error(f"Exchange error (no retry): {e}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error (no retry): {e}")
                raise

        raise Exception(f"Max retries ({max_retries}) exceeded")

    # ── Market Data ──────────────────────────────────────────

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 100) -> list:
        """Fetch OHLCV klines."""
        return self._retry_request(
            self.exchange.fetch_ohlcv, symbol, timeframe, limit=limit
        )

    def fetch_ticker(self, symbol: str) -> dict:
        """Fetch current ticker."""
        return self._retry_request(self.exchange.fetch_ticker, symbol)

    def fetch_markets(self) -> list:
        """Fetch all available markets."""
        return self._retry_request(self.exchange.fetch_markets)

    def fetch_balance(self) -> dict:
        """Fetch account balance."""
        return self._retry_request(self.exchange.fetch_balance)

    # ── Trading ──────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for a symbol."""
        return self._retry_request(
            self.exchange.set_leverage, leverage, symbol
        )

    def set_margin_mode(self, symbol: str, margin_mode: str):
        """Set margin mode (isolated/cross)."""
        return self._retry_request(
            self.exchange.set_margin_mode, margin_mode, symbol
        )

    def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict = None
    ) -> dict:
        """Create a market order."""
        return self._retry_request(
            self.exchange.create_market_order, symbol, side, amount, params=params or {}
        )

    def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float, params: dict = None
    ) -> dict:
        """Create a limit order."""
        return self._retry_request(
            self.exchange.create_limit_order, symbol, side, amount, price, params=params or {}
        )

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order."""
        return self._retry_request(
            self.exchange.cancel_order, order_id, symbol
        )

    def cancel_all_orders(self, symbol: str) -> list:
        """Cancel all open orders for a symbol."""
        return self._retry_request(
            self.exchange.cancel_all_orders, symbol
        )

    def fetch_open_orders(self, symbol: str = None) -> list:
        """Fetch open orders."""
        return self._retry_request(
            self.exchange.fetch_open_orders, symbol
        )

    def fetch_positions(self, symbols: list = None) -> list:
        """Fetch open positions."""
        return self._retry_request(
            self.exchange.fetch_positions, symbols
        )

    def close_position(self, symbol: str, side: str = None, params: dict = None) -> dict:
        """Close a position by market order."""
        return self._retry_request(
            self.exchange.close_position, symbol, params=params or {}
        )

    def get_usdt_perpetual_symbols(self) -> list[str]:
        """Get all USDT-M perpetual swap symbols."""
        markets = self.fetch_markets()
        symbols = []
        for m in markets:
            if (
                m.get("quote", "") == "USDT"
                and m.get("swap", False)
                and m.get("active", True)
                and m.get("linear", False)
            ):
                symbols.append(m["symbol"])
        return symbols
