"""
Discord Bot Module
Handles all Discord bot commands, events, and message formatting.
Uses discord.py (rewrite) with slash commands.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import AppConfig, load_config, validate_config
from modules.exchange import ExchangeClient
from modules.data_fetcher import DataFetcher
from modules.indicators import compute_all_indicators
from modules.risk_manager import RiskManager
from modules.strategy_breakout import BreakoutStrategy
from modules.strategy_grid import GridStrategy

logger = logging.getLogger(__name__)


class TradingBot(commands.Bot):
    """Main Discord trading bot class."""

    def __init__(self, config: AppConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=config.discord.command_prefix,
            intents=intents,
        )
        self.config = config
        self.exchange: Optional[ExchangeClient] = None
        self.data_fetcher: Optional[DataFetcher] = None
        self.risk_manager: Optional[RiskManager] = None
        self.breakout: Optional[BreakoutStrategy] = None
        self.grid: Optional[GridStrategy] = None
        self._ready = False

    async def setup_hook(self):
        """Initialize exchange and strategies when bot connects."""
        logger.info("Setting up trading bot...")

        # Initialize exchange
        self.exchange = ExchangeClient(self.config.exchange, self.config.risk)
        self.data_fetcher = DataFetcher(self.exchange)
        self.risk_manager = RiskManager(self.exchange, self.config.risk)
        self.breakout = BreakoutStrategy(self.exchange, self.data_fetcher, self.config.strategy_a)
        self.grid = GridStrategy(self.exchange, self.data_fetcher, self.config.strategy_b)

        # Register slash commands
        self.tree.add_command(status_cmd)
        self.tree.add_command(balance_cmd)
        self.tree.add_command(start_breakout_cmd)
        self.tree.add_command(stop_breakout_cmd)
        self.tree.add_command(start_grid_cmd)
        self.tree.add_command(stop_grid_cmd)
        self.tree.add_command(set_capital_cmd)
        self.tree.add_command(positions_cmd)
        self.tree.add_command(hot_symbols_cmd)
        self.tree.add_command(grid_status_cmd)
        self.tree.add_command(emergency_stop_cmd)
        self.tree.add_command(risk_status_cmd)

        # Start background loops
        if self.config.strategy_a.enabled:
            breakout_loop.start(self)
        if self.config.strategy_b.enabled:
            grid_update_loop.start(self)
            grid_fill_check_loop.start(self)
        risk_check_loop.start(self)

        self._ready = True
        logger.info("Trading bot setup complete!")

        # Notify log channel
        if self.config.discord.log_channel_id:
            channel = self.get_channel(int(self.config.discord.log_channel_id))
            if channel:
                embed = discord.Embed(
                    title="🟢 Trading Bot Online",
                    description="Bot has started and is monitoring markets.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="Strategy A (Breakout)", value=f"{'✅ Enabled' if self.config.strategy_a.enabled else '❌ Disabled'}", inline=True)
                embed.add_field(name="Strategy B (Grid)", value=f"{'✅ Enabled' if self.config.strategy_b.enabled else '❌ Disabled'}", inline=True)
                embed.add_field(name="Capital", value=f"{self.config.allocated_capital:.2f} USDT", inline=True)
                embed.add_field(name="Exchange", value=f"{self.config.exchange.exchange_id} ({'Testnet' if self.config.exchange.testnet else 'Mainnet'})", inline=True)
                await channel.send(embed=embed)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")


# ════════════════════════════════════════════════════════════
# Background Task Loops
# ════════════════════════════════════════════════════════════

@tasks.loop(seconds=60)
async def risk_check_loop(bot: TradingBot):
    """Check risk conditions every 60 seconds."""
    if not bot._ready:
        return
    try:
        halted, reason = bot.risk_manager.should_halt_trading(bot.config.allocated_capital)
        if halted:
            logger.critical(f"RISK HALT: {reason}")
            results = bot.risk_manager.emergency_stop_all(bot.breakout, bot.grid)

            # Send alert to Discord
            if bot.config.discord.alert_channel_id:
                channel = bot.get_channel(int(bot.config.discord.alert_channel_id))
                if channel:
                    embed = discord.Embed(
                        title="🚨 CIRCUIT BREAKER TRIGGERED",
                        description=reason,
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.add_field(name="Action Taken", value="All positions closed, all orders cancelled")
                    embed.add_field(name="Cooldown", value=f"{bot.config.risk.cooldown_hours} hours")
                    await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Risk check loop error: {e}")


@tasks.loop(seconds=900)  # Every 15 minutes
async def breakout_loop(bot: TradingBot):
    """Breakout strategy main loop."""
    if not bot._ready:
        return
    try:
        # Check risk
        halted, reason = bot.risk_manager.should_halt_trading(bot.config.allocated_capital)
        if halted:
            return

        # Step 1: Scan hot symbols
        hot = bot.breakout.scan_hot_symbols()

        # Step 2: Check entry signals for hot symbols
        for symbol in hot:
            signal = bot.breakout.check_entry_signal(symbol)
            if signal:
                result = bot.breakout.execute_entry(signal, bot.config.allocated_capital)
                if result:
                    await send_trade_alert(bot, result)

        # Step 3: Check exit signals for active positions
        if bot.breakout.active_positions:
            exits, to_remove = bot.breakout.check_exit_signals(bot.config.allocated_capital)
            for exit_sig in exits:
                result = bot.breakout.execute_exit(exit_sig)
                if result:
                    await send_trade_alert(bot, result)
            for sym in to_remove:
                bot.breakout.active_positions.pop(sym, None)

    except Exception as e:
        logger.error(f"Breakout loop error: {e}")


@tasks.loop(seconds=3600)  # Every hour
async def grid_update_loop(bot: TradingBot):
    """Grid strategy: update grids on 1h kline close."""
    if not bot._ready:
        return
    try:
        halted, reason = bot.risk_manager.should_halt_trading(bot.config.allocated_capital)
        if halted:
            return

        active_symbols = bot.grid.get_active_symbols()
        for symbol in active_symbols:
            # Check global stop
            if bot.grid.check_global_stop(symbol):
                bot.exchange.cancel_all_orders(symbol)
                bot.exchange.close_position(symbol)
                await send_trade_alert(bot, {
                    "action": "GLOBAL_STOP",
                    "symbol": symbol,
                    "reason": "Price below global stop level",
                })
                continue

            # Update grid
            result = bot.grid.update_grid(symbol, bot.config.allocated_capital)
            if result:
                logger.info(f"Grid updated for {symbol}: {result}")

    except Exception as e:
        logger.error(f"Grid update loop error: {e}")


@tasks.loop(seconds=120)  # Every 2 minutes
async def grid_fill_check_loop(bot: TradingBot):
    """Grid strategy: check for filled orders and place corresponding sells."""
    if not bot._ready:
        return
    try:
        active_symbols = bot.grid.get_active_symbols()
        for symbol in active_symbols:
            actions = bot.grid.check_filled_orders(symbol, bot.config.allocated_capital)
            for action in actions:
                await send_trade_alert(bot, action)
    except Exception as e:
        logger.error(f"Grid fill check loop error: {e}")


async def send_trade_alert(bot: TradingBot, result: dict):
    """Send a trade alert to the configured Discord channel."""
    if not bot.config.discord.alert_channel_id:
        return
    channel = bot.get_channel(int(bot.config.discord.alert_channel_id))
    if not channel:
        return

    action = result.get("action", "")
    symbol = result.get("symbol", "")

    color_map = {
        "ENTRY": discord.Color.green(),
        "STOP_LOSS": discord.Color.red(),
        "TRAILING_STOP": discord.Color.orange(),
        "GRID_FILL": discord.Color.blue(),
        "GLOBAL_STOP": discord.Color.dark_red(),
    }
    color = color_map.get(action, discord.Color.greyple())

    emoji_map = {
        "ENTRY": "🟢",
        "STOP_LOSS": "🔴",
        "TRAILING_STOP": "🟠",
        "GRID_FILL": "🔵",
        "GLOBAL_STOP": "🚨",
    }
    emoji = emoji_map.get(action, "📋")

    embed = discord.Embed(
        title=f"{emoji} {action} - {symbol}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    for key, value in result.items():
        if key == "action":
            continue
        if isinstance(value, float):
            embed.add_field(name=key.replace("_", " ").title(), value=f"{value:.4f}", inline=True)
        else:
            embed.add_field(name=key.replace("_", " ").title(), value=str(value), inline=True)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send trade alert: {e}")


# ════════════════════════════════════════════════════════════
# Slash Commands
# ════════════════════════════════════════════════════════════

@app_commands.command(name="status", description="Show bot status and configuration")
async def status_cmd(interaction: discord.Interaction):
    """Show bot status."""
    bot: TradingBot = interaction.client  # type: ignore
    if not bot._ready:
        await interaction.response.send_message("⚠️ Bot is still initializing...", ephemeral=True)
        return

    config = bot.config
    embed = discord.Embed(
        title="📊 Bot Status",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Exchange", value=f"{config.exchange.exchange_id} ({'Testnet' if config.exchange.testnet else 'Mainnet'})", inline=True)
    embed.add_field(name="Capital", value=f"{config.allocated_capital:.2f} USDT", inline=True)
    embed.add_field(name="Strategy A", value=f"{'✅ ON' if config.strategy_a.enabled else '❌ OFF'} (Leverage {config.strategy_a.leverage}x)", inline=True)
    embed.add_field(name="Strategy B", value=f"{'✅ ON' if config.strategy_b.enabled else '❌ OFF'} (Leverage {config.strategy_b.leverage}x)", inline=True)
    embed.add_field(name="Breakout Positions", value=str(len(bot.breakout.active_positions)), inline=True)
    embed.add_field(name="Active Grids", value=str(len(bot.grid.get_active_symbols())), inline=True)

    halted, reason = bot.risk_manager.should_halt_trading(config.allocated_capital)
    risk_status = f"🔴 HALTED: {reason}" if halted else "🟢 Normal"
    embed.add_field(name="Risk Status", value=risk_status, inline=False)

    await interaction.response.send_message(embed=embed)


@app_commands.command(name="balance", description="Check account balance and equity")
async def balance_cmd(interaction: discord.Interaction):
    """Check balance."""
    bot: TradingBot = interaction.client  # type: ignore
    try:
        balance = bot.exchange.fetch_balance()
        usdt = balance.get("USDT", {})

        embed = discord.Embed(
            title="💰 Account Balance",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Available", value=f"{float(usdt.get('free', 0)):.4f} USDT", inline=True)
        embed.add_field(name="In Orders", value=f"{float(usdt.get('used', 0)):.4f} USDT", inline=True)
        embed.add_field(name="Total", value=f"{float(usdt.get('total', 0)):.4f} USDT", inline=True)

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


@app_commands.command(name="start_breakout", description="Start breakout strategy scanning")
async def start_breakout_cmd(interaction: discord.Interaction):
    """Manually trigger a breakout scan."""
    bot: TradingBot = interaction.client  # type: ignore
    await interaction.response.send_message("🔍 Scanning for breakout targets... This may take a few minutes.")
    try:
        hot = bot.breakout.scan_hot_symbols()
        if not hot:
            await interaction.followup.send("No hot symbols found at this time.")
            return

        msg = f"🔥 Found {len(hot)} hot symbol(s):\n"
        for s in hot[:10]:
            msg += f"  • `{s}`\n"
        if len(hot) > 10:
            msg += f"  ... and {len(hot) - 10} more"

        await interaction.followup.send(msg)
    except Exception as e:
        await interaction.followup.send(f"❌ Scan error: {e}")


@app_commands.command(name="stop_breakout", description="Close all breakout positions and stop")
async def stop_breakout_cmd(interaction: discord.Interaction):
    """Stop breakout strategy and close positions."""
    bot: TradingBot = interaction.client  # type: ignore
    try:
        results = bot.breakout.close_all_positions()
        bot.config.strategy_a.enabled = False
        breakout_loop.stop()
        msg = "✅ Breakout strategy stopped.\n"
        for r in results:
            msg += f"  • {r['symbol']}: {r['status']}\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


@app_commands.command(name="start_grid", description="Start grid trading for a symbol")
@app_commands.describe(symbol="Trading pair (e.g., BTC/USDT:USDT)")
async def start_grid_cmd(interaction: discord.Interaction, symbol: str):
    """Start grid for a symbol."""
    bot: TradingBot = interaction.client  # type: ignore
    await interaction.response.send_message(f"📊 Setting up grid for `{symbol}`...")
    try:
        result = bot.grid.setup_grid(symbol, bot.config.allocated_capital)
        if result:
            embed = discord.Embed(
                title=f"📐 Grid Setup: {symbol}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc),
            )
            for k, v in result.items():
                if isinstance(v, float):
                    embed.add_field(name=k.replace("_", " ").title(), value=f"{v:.4f}", inline=True)
                else:
                    embed.add_field(name=k.replace("_", " ").title(), value=str(v), inline=True)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"⚠️ Could not set up grid for `{symbol}` (trend protection or error).")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@app_commands.command(name="stop_grid", description="Stop grid trading for a symbol (close all)")
@app_commands.describe(symbol="Trading pair (e.g., BTC/USDT:USDT)")
async def stop_grid_cmd(interaction: discord.Interaction, symbol: str):
    """Stop grid for a symbol."""
    bot: TradingBot = interaction.client  # type: ignore
    try:
        bot.exchange.cancel_all_orders(symbol)
        bot.exchange.close_position(symbol)
        bot.grid.grid_states.pop(symbol, None)
        await interaction.response.send_message(f"✅ Grid stopped and closed for `{symbol}`.")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


@app_commands.command(name="set_capital", description="Set allocated trading capital")
@app_commands.describe(amount="Capital amount in USDT")
async def set_capital_cmd(interaction: discord.Interaction, amount: float):
    """Set allocated capital."""
    bot: TradingBot = interaction.client  # type: ignore
    if amount <= 0:
        await interaction.response.send_message("❌ Capital must be positive.", ephemeral=True)
        return
    bot.config.allocated_capital = amount
    await interaction.response.send_message(f"✅ Allocated capital set to **{amount:.2f} USDT**")


@app_commands.command(name="positions", description="Show all open positions")
async def positions_cmd(interaction: discord.Interaction):
    """Show open positions."""
    bot: TradingBot = interaction.client  # type: ignore
    try:
        positions = bot.exchange.fetch_positions()
        open_pos = [p for p in positions if float(p.get("contracts", 0)) != 0]

        if not open_pos:
            await interaction.response.send_message("📋 No open positions.")
            return

        embed = discord.Embed(
            title=f"📋 Open Positions ({len(open_pos)})",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        for pos in open_pos[:10]:
            symbol = pos.get("symbol", "")
            side = pos.get("side", "")
            pnl = float(pos.get("unrealizedPnl", 0))
            pnl_pct = float(pos.get("percentage", 0))
            contracts = float(pos.get("contracts", 0))
            leverage = pos.get("leverage", "?")

            emoji = "🟢" if pnl >= 0 else "🔴"
            name = f"{emoji} {symbol}"
            value = f"{side} | {leverage}x | PnL: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)"
            embed.add_field(name=name, value=value, inline=False)

        if len(open_pos) > 10:
            embed.set_footer(text=f"... and {len(open_pos) - 10} more positions")

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


@app_commands.command(name="hot_symbols", description="Scan for hot symbols (volume spike + high amplitude)")
async def hot_symbols_cmd(interaction: discord.Interaction):
    """Scan hot symbols."""
    bot: TradingBot = interaction.client  # type: ignore
    await interaction.response.send_message("🔍 Scanning all USDT-M perpetuals...")
    try:
        hot = bot.breakout.scan_hot_symbols()
        if not hot:
            await interaction.followup.send("No hot symbols found.")
            return

        msg = f"🔥 **{len(hot)} Hot Symbol(s)** (Vol≥200%, Amp≥8%):\n"
        for s in hot[:20]:
            msg += f"  • `{s}`\n"
        if len(hot) > 20:
            msg += f"  ... and {len(hot) - 20} more"

        await interaction.followup.send(msg)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@app_commands.command(name="grid_status", description="Show status of all active grids")
async def grid_status_cmd(interaction: discord.Interaction):
    """Show grid status."""
    bot: TradingBot = interaction.client  # type: ignore
    grids = bot.grid.grid_states

    if not grids:
        await interaction.response.send_message("📐 No active grids.")
        return

    embed = discord.Embed(
        title=f"📐 Grid Status ({len(grids)})",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    for symbol, state in grids.items():
        paused = state.get("paused", False)
        status_emoji = "⏸️" if paused else "✅"

        if paused:
            value = f"Paused: {state.get('reason', 'unknown')}"
        else:
            upper = state.get("upper", 0)
            lower = state.get("lower", 0)
            center = state.get("center", 0)
            orders = len(state.get("orders", []))
            value = f"Center: {center:.4f} | Range: [{lower:.4f}, {upper:.4f}] | Orders: {orders}"

        embed.add_field(name=f"{status_emoji} {symbol}", value=value, inline=False)

    await interaction.response.send_message(embed=embed)


@app_commands.command(name="emergency_stop", description="🚨 EMERGENCY: Close all positions and cancel all orders")
async def emergency_stop_cmd(interaction: discord.Interaction):
    """Emergency stop everything."""
    bot: TradingBot = interaction.client  # type: ignore
    await interaction.response.send_message("🚨 **EMERGENCY STOP** - Closing all positions and cancelling orders...")
    try:
        results = bot.risk_manager.emergency_stop_all(bot.breakout, bot.grid)

        # Stop all loops
        if breakout_loop.is_running():
            breakout_loop.stop()
        if grid_update_loop.is_running():
            grid_update_loop.stop()
        if grid_fill_check_loop.is_running():
            grid_fill_check_loop.stop()

        msg = "🚨 **Emergency Stop Complete**\n\n"
        msg += f"**Breakout positions closed:** {len(results.get('breakout_closed', []))}\n"
        msg += f"**Grids closed:** {len(results.get('grids_closed', []))}\n\n"
        msg += "⚠️ All strategies are now STOPPED. Use `/status` to check."

        await interaction.followup.send(msg)
    except Exception as e:
        await interaction.followup.send(f"❌ Error during emergency stop: {e}")


@app_commands.command(name="risk_status", description="Show risk management status")
async def risk_status_cmd(interaction: discord.Interaction):
    """Show risk status."""
    bot: TradingBot = interaction.client  # type: ignore
    config = bot.config
    rm = bot.risk_manager

    embed = discord.Embed(
        title="🛡️ Risk Management Status",
        color=discord.Color.purple(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(name="Daily Drawdown Limit", value=f"-{config.risk.daily_drawdown_limit*100:.1f}%", inline=True)
    embed.add_field(name="Max Exposure", value=f"{config.risk.max_total_exposure_pct*100:.0f}% of capital", inline=True)
    embed.add_field(name="Cooldown Period", value=f"{config.risk.cooldown_hours}h", inline=True)

    if rm._daily_start_equity:
        current_eq = rm._get_total_equity(config.allocated_capital)
        dd = (current_eq - rm._daily_start_equity) / rm._daily_start_equity * 100
        color = discord.Color.green() if dd > -config.risk.daily_drawdown_limit * 100 else discord.Color.red()
        embed.add_field(name="Today's Drawdown", value=f"{dd:+.2f}%", inline=True)
        embed.color = color
    else:
        embed.add_field(name="Today's Drawdown", value="Not initialized", inline=True)

    is_broken = rm.is_circuit_broken()
    embed.add_field(name="Circuit Breaker", value="🔴 ACTIVE" if is_broken else "🟢 Normal", inline=True)

    if rm._circuit_break_time:
        elapsed = (datetime.now(timezone.utc) - rm._circuit_break_time).total_seconds() / 3600
        remaining = config.risk.cooldown_hours - elapsed
        if remaining > 0:
            embed.add_field(name="Cooldown Remaining", value=f"{remaining:.1f} hours", inline=True)

    await interaction.response.send_message(embed=embed)
