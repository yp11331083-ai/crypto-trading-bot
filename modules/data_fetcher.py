from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from modules.exchange import ExchangeClient

logger = logging.getLogger(__name__)


class DataFetcher:
    """Fetches and structures OHLCV data from the exchange."""

    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange

    def get_ohlcv_df(
        self, symbol: str, timeframe: str = "15m", limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV data and return as a pandas DataFrame."""
        try:
            raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV for {symbol} ({timeframe}): {e}")
            return None

    def get_24h_volume_change(self, symbol: str) -> Optional[float]:
        """Get the 24h volume change percentage.
        Compares recent 24h volume to the previous 24h volume.
        """
        try:
            # Fetch 48h of 1h klines (enough for 2x 24h windows)
            df = self.get_ohlcv_df(symbol, "1h", limit=48)
            if df is None or len(df) < 48:
                return None

            recent_vol = df.tail(24)["volume"].sum()
            prev_vol = df.head(24)["volume"].sum()

            if prev_vol == 0:
                return None

            change_pct = (recent_vol - prev_vol) / prev_vol
            return change_pct
        except Exception as e:
            logger.error(f"Failed to calculate 24h volume change for {symbol}: {e}")
            return None

    def get_24h_amplitude(self, symbol: str) -> Optional[float]:
        """Get 24h price amplitude (high - low) / low."""
        try:
            df = self.get_ohlcv_df(symbol, "1h", limit=24)
            if df is None or len(df) < 24:
                return None

            period_high = df["high"].max()
            period_low = df["low"].min()

            if period_low == 0:
                return None

            amplitude = (period_high - period_low) / period_low
            return amplitude
        except Exception as e:
            logger.error(f"Failed to calculate 24h amplitude for {symbol}: {e}")
            return None

    def scan_hot_symbols(
        self,
        symbols: list[str],
        volume_spike_pct: float = 2.0,
        amplitude_pct: float = 0.08,
    ) -> list[str]:
        """Scan all symbols and return hot ones meeting criteria."""
        hot_symbols = []
        total = len(symbols)
        logger.info(f"Scanning {total} symbols for hot targets...")

        for i, symbol in enumerate(symbols):
            try:
                vol_change = self.get_24h_volume_change(symbol)
                amplitude = self.get_24h_amplitude(symbol)

                if vol_change is None or amplitude is None:
                    continue

                if vol_change >= volume_spike_pct and amplitude >= amplitude_pct:
                    hot_symbols.append(symbol)
                    logger.info(
                        f"  [{i+1}/{total}] HOT: {symbol} | "
                        f"Vol+{vol_change*100:.1f}% | Amp {amplitude*100:.2f}%"
                    )
                else:
                    logger.debug(
                        f"  [{i+1}/{total}] skip: {symbol} | "
                        f"Vol+{vol_change*100:.1f}% | Amp {amplitude*100:.2f}%"
                    )
            except Exception as e:
                logger.debug(f"  [{i+1}/{total}] error scanning {symbol}: {e}")
                continue

        logger.info(f"Scan complete. Found {len(hot_symbols)} hot symbols: {hot_symbols}")
        return hot_symbols
