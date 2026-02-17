"""
Telegram Bot — Main Handlers

Commands:
  /start    — Welcome + status
  /trade    — Start auto-trading
  /stop     — Stop trading
  /status   — Positions & P&L
  /balance  — Current balance
  /strategy — View/change strategy
  /history  — Trade history
  /settings — Configure risk params
"""

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

from config import Config
from bot.keyboards.inline import (
    main_menu_keyboard, timeframe_keyboard, strategy_keyboard,
    coin_keyboard, settings_keyboard
)


class TelegramBot:
    """Telegram bot for controlling the 5min_trade scalper."""

    def __init__(self, engine=None):
        """
        Args:
            engine: reference to the TradingEngine (set by app.py)
        """
        self.engine = engine
        self.app = None

    async def setup(self):
        """Build the Telegram application."""
        self.app = (
            Application.builder()
            .token(Config.TELEGRAM_BOT_TOKEN)
            .build()
        )

        # Register commands
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("trade", self.cmd_trade))
        self.app.add_handler(CommandHandler("stop", self.cmd_stop))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("balance", self.cmd_balance))
        self.app.add_handler(CommandHandler("strategy", self.cmd_strategy))
        self.app.add_handler(CommandHandler("history", self.cmd_history))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
        self.app.add_handler(CommandHandler("markets", self.cmd_markets))

        # Callback handlers
        self.app.add_handler(CallbackQueryHandler(self.cb_timeframe, pattern=r"^tf_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_strategy, pattern=r"^strat_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_coin, pattern=r"^coin_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_command, pattern=r"^cmd_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_back, pattern=r"^back_"))

        # Set bot commands menu (may fail before initialize — that's ok)
        try:
            await self.app.bot.set_my_commands([
                BotCommand("start", "Welcome & menu"),
                BotCommand("trade", "Start trading"),
                BotCommand("stop", "Stop trading"),
                BotCommand("status", "Position & P&L status"),
                BotCommand("balance", "Check balance"),
                BotCommand("strategy", "View/change strategy"),
                BotCommand("markets", "Scan live markets"),
                BotCommand("history", "Trade history"),
                BotCommand("settings", "Bot settings"),
            ])
        except Exception:
            # Will be set after app.initialize() in main
            self._commands_pending = True

        # Log all errors instead of silently swallowing them
        async def error_handler(update, context):
            print(f"❌ Bot error: {context.error}")

        self.app.add_error_handler(error_handler)

    # ═══════════════════════════════════════════════════════════════════
    # COMMANDS
    # ═══════════════════════════════════════════════════════════════════

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Welcome message."""
        mode = "📋 PAPER" if Config.is_paper() else "🔴 LIVE"
        trading = "✅ Running" if (self.engine and self.engine.is_running) else "⏹️ Stopped"
        balance = self.engine.paper_trader.risk.balance if self.engine else Config.STARTING_BALANCE

        text = (
            f"⚡ *5MIN_TRADE — Polymarket Crypto Scalper*\n\n"
            f"Mode: {mode}\n"
            f"Status: {trading}\n"
            f"Balance: ${balance:.2f}\n"
            f"Coins: {', '.join(Config.ENABLED_COINS)}\n"
            f"Timeframes: {Config.ENABLED_TIMEFRAMES}\n\n"
            f"🎯 *Strategies (9 total):*\n"
            f"  🎰 Cheap Hunter — Buy 1-8c outcomes\n"
            f"  📉📈 Momentum Reversal — Catch dips\n"
            f"  📈 Trend Follower — Ride momentum\n"
            f"  🔀 Straddle — Volatile plays\n"
            f"  📊 Spread Scalper — Bid-ask profit\n"
            f"  🎯 Mid Sniper — Underpriced outcomes\n"
            f"  💰 YES+NO Arb — Guaranteed profit\n"
            f"  🎯 Oracle Arb — Binance edge\n"
            f"  ⏰ Time Decay — Near-expiry\n\n"
            f"Use the menu below or type /trade to start!"
        )
        await update.message.reply_text(text, parse_mode='Markdown',
                                         reply_markup=main_menu_keyboard())

    async def cmd_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start trading — select timeframe."""
        await update.message.reply_text(
            "⏱️ *Select timeframe:*",
            parse_mode='Markdown',
            reply_markup=timeframe_keyboard()
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop trading."""
        if self.engine:
            await self.engine.stop()
        await update.message.reply_text("⏹️ Trading stopped.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current positions and P&L."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        positions = self.engine.paper_trader.get_open_positions()
        stats = self.engine.paper_trader.get_summary()

        if not positions:
            pos_text = "_No open positions_"
        else:
            lines = []
            for p in positions:
                emoji = '🟢' if (p.get('pnl') or 0) >= 0 else '🔴'
                lines.append(
                    f"{emoji} {p['coin']} {p['direction']} "
                    f"@{p['entry_price']:.4f} ({p['strategy']})"
                )
            pos_text = '\n'.join(lines)

        text = (
            f"📊 **Status** ({stats.get('tier_emoji','')} {stats.get('tier','')})\n\n"
            f"💰 Balance: ${stats['balance']:.2f} "
            f"(Tradeable: ${stats.get('tradeable', 0):.2f})\n"
            f"📈 Daily P&L: ${stats['daily_pnl']:+.2f}\n"
            f"🎯 Win Rate: {stats['win_rate']:.0f}%\n"
            f"📊 Trades: {stats['total_trades']} "
            f"(W:{stats['wins']} L:{stats['losses']})\n\n"
            f"**Open Positions ({stats['open_count']}):**\n"
            f"{pos_text}"
        )
        await update.message.reply_text(text, parse_mode='Markdown')

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show balance details."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        stats = self.engine.paper_trader.get_summary()
        mode = "📋 PAPER" if Config.is_paper() else "🔴 LIVE"

        text = (
            f"💰 **Balance** ({mode})\n\n"
            f"Current: ${stats['balance']:.2f}\n"
            f"Starting: ${Config.STARTING_BALANCE:.2f}\n"
            f"Total P&L: ${stats['total_pnl']:+.2f}\n"
            f"Daily P&L: ${stats['daily_pnl']:+.2f}\n"
        )
        await update.message.reply_text(text, parse_mode='Markdown')

    async def cmd_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show strategy selection."""
        await update.message.reply_text(
            "🧠 **Select Strategy:**",
            parse_mode='Markdown',
            reply_markup=strategy_keyboard()
        )

    async def cmd_markets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Scan and show live markets."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        await update.message.reply_text("🔍 Scanning markets...")
        markets = self.engine.gamma.discover_markets()

        if not markets:
            await update.message.reply_text("❌ No active crypto markets found")
            return

        lines = ["📡 **Active Crypto Markets:**\n"]
        for m in markets:
            lines.append(
                f"• {m['coin']} {m['timeframe']}min — "
                f"Up: {m['up_price']:.2f} | Down: {m['down_price']:.2f} "
                f"(Vol: ${m['volume']:,.0f})"
            )

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show trade history."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        trades = self.engine.paper_trader.trade_history[-10:]  # Last 10
        if not trades:
            await update.message.reply_text("📜 No trade history yet.")
            return

        lines = ["📜 **Recent Trades:**\n"]
        for t in reversed(trades):
            emoji = '✅' if (t.get('pnl') or 0) > 0 else '❌'
            pnl = t.get('pnl', 0) or 0
            lines.append(
                f"{emoji} {t['coin']} {t['direction']} — "
                f"${pnl:+.2f} ({t.get('pnl_pct', 0):+.1f}%) "
                f"[{t.get('strategy', '?')}]"
            )

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings."""
        await update.message.reply_text(
            "⚙️ **Settings:**",
            parse_mode='Markdown',
            reply_markup=settings_keyboard()
        )

    # ═══════════════════════════════════════════════════════════════════
    # CALLBACKS
    # ═══════════════════════════════════════════════════════════════════

    async def cb_timeframe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle timeframe selection."""
        query = update.callback_query
        await query.answer()

        tf_str = query.data.replace('tf_', '')

        if tf_str == 'all':
            timeframes = [5, 15, 30]
        else:
            timeframes = [int(tf_str)]

        if self.engine:
            self.engine.active_timeframes = timeframes
            await self.engine.start()

        await query.edit_message_text(
            f"▶️ Trading started on: **{', '.join(f'{t}min' for t in timeframes)}**\n\n"
            f"Strategy: ⚡ Dynamic (auto-select)\n"
            f"Mode: {'📋 Paper' if Config.is_paper() else '🔴 Live'}",
            parse_mode='Markdown'
        )

    async def cb_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle strategy selection."""
        query = update.callback_query
        await query.answer()
        strat = query.data.replace('strat_', '')

        if self.engine:
            self.engine.active_strategy = strat

        names = {
            'cheap_hunter': '🎰 Cheap Hunter',
            'momentum_reversal': '📉📈 Momentum Reversal',
            'trend_follower': '📈 Trend Follower',
            'straddle': '🔀 Straddle',
            'spread_scalper': '📊 Spread Scalper',
            'mid_sniper': '🎯 Mid-Price Sniper',
            'oracle_arb': '🎯 Oracle Arb',
            'yes_no_arb': '💰 YES+NO Arb',
            'time_decay': '⏰ Time Decay',
            'dynamic': '⚡ Dynamic (All 9)',
        }
        await query.edit_message_text(f"Strategy set to: **{names.get(strat, strat)}**",
                                       parse_mode='Markdown')

    async def cb_coin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle coin selection."""
        query = update.callback_query
        await query.answer()
        coin = query.data.replace('coin_', '')

        if coin == 'ALL':
            Config.ENABLED_COINS = ['BTC', 'ETH', 'SOL']
        else:
            Config.ENABLED_COINS = [coin]

        await query.edit_message_text(f"Coins: **{', '.join(Config.ENABLED_COINS)}**",
                                       parse_mode='Markdown')

    async def cb_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle menu command buttons."""
        query = update.callback_query
        await query.answer()
        cmd = query.data.replace('cmd_', '')

        # Map to command handlers
        handlers = {
            'trade': self.cmd_trade,
            'stop': self.cmd_stop,
            'status': self.cmd_status,
            'balance': self.cmd_balance,
            'strategy': self.cmd_strategy,
            'history': self.cmd_history,
            'settings': self.cmd_settings,
        }

        handler = handlers.get(cmd)
        if handler:
            # Create a fake Update-like object for callback context
            # Since callbacks use query.message not update.message
            class FakeUpdate:
                message = query.message
            await handler(FakeUpdate(), context)

    async def cb_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle back buttons."""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "⚡ **5MIN\\_TRADE**\n\nSelect an action:",
            parse_mode='MarkdownV2',
            reply_markup=main_menu_keyboard()
        )

    # ═══════════════════════════════════════════════════════════════════
    # NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════════════

    async def send_trade_alert(self, trade: dict):
        """Send trade execution notification."""
        if not Config.TELEGRAM_CHAT_ID or not self.app:
            return

        emoji = '📈' if trade['direction'] == 'UP' else '📉'
        text = (
            f"{emoji} NEW TRADE\n\n"
            f"Strategy: {trade['strategy']}\n"
            f"Coin: {trade['coin']} {trade['direction']}\n"
            f"Entry: ${trade['entry_price']:.4f}\n"
            f"Size: ${trade['size_usd']:.2f}\n"
            f"Confidence: {trade['confidence']:.0%}\n\n"
            f"{trade.get('rationale', '')}"
        )
        try:
            await self.app.bot.send_message(
                chat_id=Config.TELEGRAM_CHAT_ID,
                text=text,
            )
        except Exception as e:
            print(f"❌ Telegram alert error: {e}")

    async def send_close_alert(self, trade: dict):
        """Send trade close notification."""
        if not Config.TELEGRAM_CHAT_ID or not self.app:
            return

        pnl = trade.get('pnl', 0) or 0
        emoji = '✅' if pnl > 0 else '❌'
        text = (
            f"{emoji} TRADE CLOSED\n\n"
            f"{trade['coin']} {trade['direction']}\n"
            f"Entry: ${trade['entry_price']:.4f} -> "
            f"Exit: ${trade.get('exit_price', 0):.4f}\n"
            f"PnL: ${pnl:+.2f} ({trade.get('pnl_pct', 0):+.1f}%)\n"
            f"Reason: {trade.get('exit_reason', 'unknown')}"
        )
        try:
            await self.app.bot.send_message(
                chat_id=Config.TELEGRAM_CHAT_ID,
                text=text,
            )
        except Exception as e:
            print(f"❌ Telegram alert error: {e}")

    async def send_pnl_report(self, summary: dict, open_positions: list):
        """Send periodic PnL report."""
        if not Config.TELEGRAM_CHAT_ID or not self.app:
            return

        pos_lines = []
        for p in open_positions[:10]:
            entry = p.get('entry_price', 0)
            coin = p.get('coin', '?')
            direction = p.get('direction', '?')
            strategy = p.get('strategy', '?')
            size = p.get('size_usd', 0)
            pos_lines.append(f"  {coin} {direction} @${entry:.3f} (${size:.2f}) [{strategy}]")

        positions_text = '\n'.join(pos_lines) if pos_lines else '  No open positions'

        text = (
            f"📊 PnL REPORT\n\n"
            f"Balance: ${summary.get('balance', 0):.2f}\n"
            f"Tier: {summary.get('tier_emoji', '')} {summary.get('tier', '')}\n"
            f"Total Trades: {summary.get('total_trades', 0)}\n"
            f"Win Rate: {summary.get('win_rate', 0):.0f}%\n"
            f"Total PnL: ${summary.get('total_pnl', 0):+.2f}\n"
            f"Open Positions: {summary.get('open_count', 0)}\n\n"
            f"Positions:\n{positions_text}"
        )
        try:
            await self.app.bot.send_message(
                chat_id=Config.TELEGRAM_CHAT_ID,
                text=text,
            )
        except Exception as e:
            print(f"❌ PnL report error: {e}")
