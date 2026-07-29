from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


def compute_rsi(df: pd.DataFrame, period: int = 14) -> Optional[pd.Series]:
    """Compute RSI indicator."""
    try:
        rsi = ta.rsi(df["close"], length=period)
        return rsi
    except Exception as e:
        logger.error(f"RSI calculation failed: {e}")
        return None


def compute_ema(df: pd.DataFrame, period: int = 20) -> Optional[pd.Series]:
    """Compute EMA indicator."""
    try:
        ema = ta.ema(df["close"], length=period)
        return ema
    except Exception as e:
        logger.error(f"EMA calculation failed: {e}")
        return None


def compute_atr(df: pd.DataFrame, period: int = 14) -> Optional[pd.Series]:
    """Compute ATR indicator."""
    try:
        atr = ta.atr(df["high"], df["low"], df["close"], length=period)
        return atr
    except Exception as e:
        logger.error(f"ATR calculation failed: {e}")
        return None


def compute_volume_ma(df: pd.DataFrame, period: int = 20) -> Optional[pd.Series]:
    """Compute Volume Moving Average."""
    try:
        vma = ta.sma(df["volume"], length=period)
        return vma
    except Exception as e:
        logger.error(f"Volume MA calculation failed: {e}")
        return None


def compute_all_indicators(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Compute all required indicators and append to DataFrame.

    Args:
        df: DataFrame with OHLCV columns
        config: dict with keys: rsi_period, ema_period, atr_period, volume_ma_period

    Returns:
        DataFrame with indicator columns added
    """
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
