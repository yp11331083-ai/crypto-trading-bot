"""
Configuration Management Module
Loads environment variables and provides validated config objects.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExchangeConfig:
    api_key: str = ""
    api_secret: str = ""
    exchange_id: str = "bybit"
    testnet: bool = True
    demo_trading: bool = False
    default_type: str = "swap"


@dataclass
class StrategyAConfig:
    """Breakout Strategy Parameters"""
    enabled: bool = True
    leverage: int = 5
    margin_mode: str = "isolated"  # isolated / cross
    scan_interval: int = 3600       # seconds, hourly scan
    volume_spike_pct: float = 2.0   # >= 200% volume increase
    amplitude_pct: float = 0.08     # >= 8% 24h range
    lookback_klines: int = 96       # 24h of 15m klines
    volume_ma_period: int = 20
    volume_multiplier: float = 2.5
    rsi_period: int = 14
    rsi_threshold: float = 65.0
    stop_loss_pct: float = 0.015    # 1.5% below entry
    trailing_activate_pct: float = 0.02  # +2% unrealized profit
    trailing_atr_period: int = 14
    trailing_atr_multiplier: float = 1.0
    trailing_fallback_pct: float = 0.01   # 1% fallback
    max_open_positions: int = 3
    position_size_pct: float = 0.1   # 10% of allocated capital per trade


@dataclass
class StrategyBConfig:
    """Grid Strategy Parameters"""
    enabled: bool = True
    leverage: int = 3
    margin_mode: str = "isolated"
    ema_period: int = 20
    atr_period: int = 14
    band_multiplier: float = 2.5
    grid_count: int = 12
    rsi_period: int = 14
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0
    stop_multiplier: float = 3.5      # EMA - 3.5*ATR = global stop
    update_interval: int = 3600      # 1h kline close
    max_open_positions: int = 2
    position_size_pct: float = 0.05  # 5% per grid level


@dataclass
class RiskConfig:
    """Global Risk Management"""
    daily_drawdown_limit: float = 0.05  # -5% equity drawdown
    max_total_exposure_pct: float = 0.5  # max 50% of equity in positions
    api_retry_max: int = 5
    api_retry_base_delay: float = 1.0   # exponential backoff base
    cooldown_hours: int = 24             # halt trading after circuit break


@dataclass
class DiscordConfig:
    token: str = ""
    command_prefix: str = "!"
    log_channel_id: Optional[str] = None
    alert_channel_id: Optional[str] = None


@dataclass
class AppConfig:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    strategy_a: StrategyAConfig = field(default_factory=StrategyAConfig)
    strategy_b: StrategyBConfig = field(default_factory=StrategyBConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    allocated_capital: float = 1000.0  # user-defined dynamic capital (USDT)
    log_level: str = "INFO"


def load_config() -> AppConfig:
    """Load configuration from environment variables."""
    config = AppConfig()

    # Exchange
    config.exchange.api_key = os.getenv("BYBIT_API_KEY", "")
    config.exchange.api_secret = os.getenv("BYBIT_API_SECRET", "")
    config.exchange.exchange_id = os.getenv("EXCHANGE_ID", "bybit")
    config.exchange.testnet = os.getenv("TESTNET", "true").lower() == "true"
    config.exchange.demo_trading = os.getenv("DEMO_TRADING", "false").lower() == "true"

    # Discord
    config.discord.token = os.getenv("DISCORD_BOT_TOKEN", "")
    config.discord.log_channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID")
    config.discord.alert_channel_id = os.getenv("DISCORD_ALERT_CHANNEL_ID")

    # Capital
    capital_str = os.getenv("ALLOCATED_CAPITAL", "1000")
    try:
        config.allocated_capital = float(capital_str)
    except ValueError:
        config.allocated_capital = 1000.0

    # Log level
    config.log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # Strategy A overrides from env
    if os.getenv("STRATEGY_A_ENABLED"):
        config.strategy_a.enabled = os.getenv("STRATEGY_A_ENABLED", "true").lower() == "true"
    if os.getenv("STRATEGY_A_LEVERAGE"):
        config.strategy_a.leverage = int(os.getenv("STRATEGY_A_LEVERAGE", "5"))

    # Strategy B overrides from env
    if os.getenv("STRATEGY_B_ENABLED"):
        config.strategy_b.enabled = os.getenv("STRATEGY_B_ENABLED", "true").lower() == "true"
    if os.getenv("STRATEGY_B_LEVERAGE"):
        config.strategy_b.leverage = int(os.getenv("STRATEGY_B_LEVERAGE", "3"))

    # Risk overrides from env
    if os.getenv("DAILY_DRAWDOWN_LIMIT"):
        config.risk.daily_drawdown_limit = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.05"))

    return config


def validate_config(config: AppConfig) -> list[str]:
    """Validate configuration and return list of errors."""
    errors = []
    if not config.exchange.api_key:
        errors.append("BYBIT_API_KEY is not set")
    if not config.exchange.api_secret:
        errors.append("BYBIT_API_SECRET is not set")
    if not config.discord.token:
        errors.append("DISCORD_BOT_TOKEN is not set")
    if config.allocated_capital <= 0:
        errors.append("ALLOCATED_CAPITAL must be positive")
    if config.strategy_a.leverage < 1 or config.strategy_a.leverage > 100:
        errors.append("STRATEGY_A_LEVERAGE must be between 1 and 100")
    if config.strategy_b.leverage < 1 or config.strategy_b.leverage > 100:
        errors.append("STRATEGY_B_LEVERAGE must be between 1 and 100")
    return errors
