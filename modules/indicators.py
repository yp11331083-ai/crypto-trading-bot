"""Technical indicators: RSI, EMA, ATR, Volume MA.
No external TA library dependency — pure pandas + numpy.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_rsi(df: pd.DataFrame, period: int = 14) -> Optional[pd.Series]:
    """Compute RSI using Wilder's smoothing method."""
    try:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi
    except Exception as e:
        logger.error(f"RSI calculation failed: {e}")
        return None


def compute_ema(df: pd.DataFrame, period: int = 20) -> Optional[pd.Series]:
    """Compute EMA."""
    try:
        return df["close"].ewm(span=period, adjust=False).mean()
    except Exception as e:
        logger.error(f"EMA calculation failed: {e}")
        return None


def compute_atr(df: pd.DataFrame, period: int = 14) -> Optional[pd.Series]:
    """Compute ATR (Average True Range)."""
    try:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
        return atr
    except Exception as e:
        logger.error(f"ATR calculation failed: {e}")
        return None


def compute_volume_ma(df: pd.DataFrame, period: int = 20) -> Optional[pd.Series]:
    """Compute Volume Simple Moving Average."""
    try:
        return df["volume"].rolling(window=period, min_periods=period).mean()
    except Exception as e:
        logger.error(f"Volume MA calculation failed: {e}")
        return None


def compute_all_indicators(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Compute all required indicators and append to DataFrame."""
    result = df.copy()

    rsi = compute_rsi(result, config.get("rsi_period", 14))
    if rsi is not None:
        result["RSI"] = rsi

    ema = compute_ema(result, config.get("ema_period", 20))
    if ema is not None:
        result["EMA_20"] = ema

    atr = compute_atr(result, config.get("atr_period", 14))
    if atr is not None:
        result["ATR_14"] = atr

    vma = compute_volume_ma(result, config.get("volume_ma_period", 20))
    if vma is not None:
        result["VOL_MA"] = vma

    return result
