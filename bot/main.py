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
        self.app.add_handler(CommandHandler("mode", self.cmd_mode))
        self.app.add_handler(CommandHandler("risk", self.cmd_risk))
        self.app.add_handler(CommandHandler("positions", self.cmd_positions))
        self.app.add_handler(CommandHandler("estop", self.cmd_estop))
        self.app.add_handler(CommandHandler("seed", self.cmd_seed))
        self.app.add_handler(CommandHandler("debug", self.cmd_debug))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("drawdown", self.cmd_drawdown))
        self.app.add_handler(CommandHandler("stratstats", self.cmd_stratstats))
        self.app.add_handler(CommandHandler("cheaphunter", self.cmd_cheaphunter))
        self.app.add_handler(CommandHandler("redeem", self.cmd_redeem))
        self.app.add_handler(CommandHandler("logs", self.cmd_logs))
        self.app.add_handler(CommandHandler("automigrate", self.cmd_automigrate))

        # Callback handlers
        self.app.add_handler(CallbackQueryHandler(self.cb_timeframe, pattern=r"^tf_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_strategy, pattern=r"^strat_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_coin, pattern=r"^coin_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_command, pattern=r"^cmd_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_back, pattern=r"^back_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_mode, pattern=r"^mode_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_risk, pattern=r"^risk_"))

        # Set bot commands menu (may fail before initialize — that's ok)
        try:
            await self.app.bot.set_my_commands([
                BotCommand("start", "Welcome & menu"),
                BotCommand("trade", "Start trading"),
                BotCommand("stop", "Stop trading"),
                BotCommand("mode", "Switch paper/live mode"),
                BotCommand("risk", "Set risk mode"),
                BotCommand("status", "Position & P&L status"),
                BotCommand("balance", "Check balance"),
                BotCommand("positions", "View open positions"),
                BotCommand("strategy", "View/change strategy"),
                BotCommand("markets", "Scan live markets"),
                BotCommand("estop", "Emergency stop"),
                BotCommand("seed", "$1 start — arb-only seed mode"),
                BotCommand("cheaphunter", "🎰 Lottery ticket mode — $1 bets across all TFs"),
                BotCommand("redeem", "Claim resolved positions on-chain"),
                BotCommand("logs", "Download trade log CSV"),
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
        is_live = self.engine and self.engine.trading_mode == 'live'
        mode = "🔴 LIVE" if is_live else "📋 PAPER"
        trading = "✅ Running" if (self.engine and self.engine.is_running) else "⏹️ Stopped"

        if is_live:
            balance = self.engine.live_balance_mgr.balance
            m = self.engine.live_balance_mgr.mode
            risk_line = f"Risk: {m.emoji} {m.name}\n"
        else:
            balance = self.engine.paper_trader.risk.balance if self.engine else Config.STARTING_BALANCE
            risk_line = ""

        text = (
            f"⚡ *5MIN_TRADE — Polymarket Crypto Scalper*\n\n"
            f"Mode: {mode}\n"
            f"{risk_line}"
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
            f"🎰 /cheaphunter — Lottery ticket mode ($1 bets)\n\n"
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

        trader = self.engine.active_trader
        positions = trader.get_open_positions()
        stats = trader.get_summary()
        is_live = stats.get('_live', False)
        mode_tag = '🔴 LIVE' if is_live else '📋 PAPER'

        if not positions:
            pos_text = "  No open positions"
        else:
            lines = []
            for p in positions:
                emoji = '🟢' if (p.get('pnl') or 0) >= 0 else '🔴'
                status = p.get('status', 'open')
                status_tag = ' [PENDING]' if status == 'pending' else ''
                lines.append(
                    f"{emoji} {p['coin']} {p['direction']} "
                    f"@${p['entry_price']:.3f} (${p.get('size_usd', 0):.2f}) "
                    f"[{p['strategy']}]{status_tag}"
                )
            pos_text = '\n'.join(lines)

        text = (
            f"📊 Status {mode_tag}\n\n"
            f"💰 Balance: ${stats.get('balance', 0):.2f}\n"
        )

        if is_live:
            text += (
                f"🛡️ Reserve: ${stats.get('reserve', 0):.2f}\n"
                f"🎯 Mode: {stats.get('mode_emoji', '')} {stats.get('mode', '')}\n"
            )
        else:
            text += (
                f"🎯 Tier: {stats.get('tier_emoji', '')} {stats.get('tier', '')}\n"
            )

        text += (
            f"🎯 Win Rate: {stats.get('win_rate', 0):.0f}%\n"
            f"📊 Trades: {stats.get('total_trades', 0)} "
            f"(W:{stats.get('wins', 0)} L:{stats.get('losses', 0)})\n"
            f"💸 Total PnL: ${stats.get('total_pnl', 0):+.2f}\n\n"
            f"Open Positions ({stats.get('open_count', len(positions))}):\n"
            f"{pos_text}"
        )
        await update.message.reply_text(text)

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show balance details with real Polymarket balance."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        paper_stats = self.engine.paper_trader.get_summary()

        text = (
            f"💰 BALANCE\n\n"
            f"📋 Paper: ${paper_stats.get('balance', 0):.2f} "
            f"(PnL: ${paper_stats.get('total_pnl', 0):+.2f})\n"
        )

        if self.engine.live_trader.is_ready:
            live_stats = self.engine.live_trader.get_summary()
            tracked_bal = live_stats.get('balance', 0)

            # Fetch REAL balance from Polymarket
            real_bal = await self.engine.live_trader.fetch_balance()
            if real_bal is not None and real_bal > 0:
                # Sync tracked balance with real balance
                self.engine.live_balance_mgr.update_balance(real_bal)
                bal_text = f"${real_bal:.2f}"
                if abs(real_bal - tracked_bal) > 0.01:
                    bal_text += f" (tracked: ${tracked_bal:.2f})"
            else:
                bal_text = f"${tracked_bal:.2f}"

            text += (
                f"🔴 Live: {bal_text}\n"
                f"   PnL: ${live_stats.get('total_pnl', 0):+.2f}\n"
                f"   Mode: {live_stats.get('mode_emoji', '')} {live_stats.get('mode', '')}\n"
                f"   Reserve: ${live_stats.get('reserve', 0):.2f}\n"
                f"   Tradeable: ${live_stats.get('tradeable', 0):.2f}\n"
            )

            # Show seed/plant mode progress
            if self.engine.live_balance_mgr.mode_name in ('seed', 'plant'):
                from trading.live_balance_manager import GRADUATION_THRESHOLDS
                mode_name = self.engine.live_balance_mgr.mode_name
                grad_target, grad_balance = GRADUATION_THRESHOLDS.get(mode_name, ('', 5.0))
                current = real_bal if real_bal else tracked_bal
                progress = min(100, current / grad_balance * 100)
                bar_filled = int(progress / 10)
                bar = '█' * bar_filled + '░' * (10 - bar_filled)
                emoji = '🌱' if mode_name == 'seed' else '🌿'
                text += f"   {emoji} {mode_name.upper()}: [{bar}] {progress:.0f}% → ${grad_balance:.2f}\n"
        else:
            text += "🔴 Live: Not configured\n"

        mode = self.engine.trading_mode.upper()
        text += f"\nActive: {mode}"
        await update.message.reply_text(text)

    async def cmd_redeem(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Force-check and redeem all resolved positions."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        if not self.engine.auto_redeemer:
            await update.message.reply_text(
                "⚠️ Auto-redeemer not available.\n"
                "Check that POLYMARKET_PRIVATE_KEY and proxy wallet are set."
            )
            return

        await update.message.reply_text("🔍 Checking for resolved positions...")

        try:
            result = await self.engine.auto_redeemer.force_check()
            redeemed = result.get('redeemed', 0)
            failed = result.get('failed', 0)
            skipped = result.get('skipped', 0)
            total_usd = result.get('total_redeemed_usd', 0)

            if redeemed > 0:
                text = (
                    f"💰 Redeemed {redeemed} position(s)!\n"
                    f"Recovered: ~${total_usd:.2f} USDC\n"
                )
                if failed > 0:
                    text += f"⚠️ {failed} failed (will retry next cycle)\n"

                # Re-sync balance
                real_bal = await self.engine.live_trader.fetch_balance()
                if real_bal is not None:
                    self.engine.live_balance_mgr.update_balance(real_bal)
                    text += f"\n💰 Balance: ${real_bal:.2f}"
            elif failed > 0:
                text = (
                    f"⚠️ {failed} position(s) failed to redeem.\n"
                    f"Will retry automatically next cycle."
                )
            else:
                text = "✅ No resolved positions to redeem right now."

            await update.message.reply_text(text)

        except Exception as e:
            await update.message.reply_text(f"❌ Redeem error: {e}")

    async def cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Download trade log CSV file.
        Usage: /logs — sends full CSV
               /logs 50 — sends last 50 trades
        """
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        from trading.trade_logger import TradeLogger, LOG_FILE
        import os

        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
            await update.message.reply_text("📭 No trade logs yet. Trades will be logged as they happen.")
            return

        # Check for optional line limit
        limit = None
        if context.args:
            try:
                limit = int(context.args[0])
            except ValueError:
                pass

        try:
            if limit:
                # Send only last N trades as a trimmed file
                import tempfile
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                header = lines[0] if lines else ''
                data_lines = lines[1:]
                tail = data_lines[-limit:] if len(data_lines) > limit else data_lines
                tmp_path = os.path.join(tempfile.gettempdir(), f'trades_last_{limit}.csv')
                with open(tmp_path, 'w', encoding='utf-8', newline='') as tmp:
                    tmp.write(header)
                    tmp.writelines(tail)
                send_path = tmp_path
                caption = f"📊 Last {len(tail)} trades"
            else:
                send_path = LOG_FILE
                logger = TradeLogger()
                caption = f"📊 Full trade log — {logger.trade_count} trades ({logger.file_size_kb:.1f} KB)"

            with open(send_path, 'rb') as f:
                await update.message.reply_document(
                    document=f,
                    filename='trades_log.csv',
                    caption=caption,
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error sending log: {e}")

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

        trader = self.engine.active_trader
        trades = trader.trade_history[-10:]
        if not trades:
            await update.message.reply_text("📜 No trade history yet.")
            return

        mode_tag = '🔴LIVE' if self.engine.trading_mode == 'live' else '📋PAPER'
        lines = [f"📜 Recent Trades ({mode_tag}):\n"]
        for t in reversed(trades):
            emoji = '✅' if (t.get('pnl') or 0) > 0 else '❌'
            pnl = t.get('pnl', 0) or 0
            lines.append(
                f"{emoji} {t['coin']} {t['direction']} — "
                f"${pnl:+.2f} ({t.get('pnl_pct', 0):+.1f}%) "
                f"[{t.get('strategy', '?')}]"
            )

        await update.message.reply_text('\n'.join(lines))

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings."""
        await update.message.reply_text(
            "⚙️ Settings:",
            reply_markup=settings_keyboard()
        )

    # ═══════════════════════════════════════════════════════════════════
    # LIVE TRADING COMMANDS
    # ═══════════════════════════════════════════════════════════════════

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch between paper and live trading mode."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        # Check if user passed argument: /mode live or /mode paper
        args = context.args
        if args:
            mode = args[0].lower()
            ok, msg = self.engine.switch_mode(mode)
            await update.message.reply_text(msg)
            return

        # Show mode selection buttons
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        current = self.engine.trading_mode
        live_ready = self.engine.live_trader.is_ready

        buttons = [
            [InlineKeyboardButton(
                f"{'✅ ' if current == 'paper' else ''}📋 Paper Mode",
                callback_data="mode_paper"
            )],
        ]
        if live_ready:
            buttons.append([InlineKeyboardButton(
                f"{'✅ ' if current == 'live' else ''}🔴 Live Mode",
                callback_data="mode_live"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                "🔴 Live Mode (not configured)", callback_data="mode_na"
            )])

        mode_label = '🔴 LIVE' if current == 'live' else '📋 PAPER'
        text = (
            f"⚡ TRADING MODE\n\n"
            f"Current: {mode_label}\n"
        )
        if current == 'live':
            m = self.engine.live_balance_mgr.mode
            text += f"Risk: {m.emoji} {m.name}\n"

        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set live trading risk mode."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        # Check if user passed argument: /risk aggressive
        args = context.args
        if args:
            risk = args[0].lower()
            ok, msg = self.engine.set_risk_mode(risk)
            if ok:
                self.engine.live_balance_mgr.auto_migrate = False
                msg += "\n🔒 Auto-migrate OFF (mode locked to your choice)"
            await update.message.reply_text(msg)
            return

        # Show risk mode buttons
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        current = self.engine.live_balance_mgr.mode_name
        from trading.live_balance_manager import LIVE_MODES

        buttons = []
        for key, mode in LIVE_MODES.items():
            check = '✅ ' if key == current else ''
            buttons.append([InlineKeyboardButton(
                f"{check}{mode.emoji} {mode.name} — {mode.description}",
                callback_data=f"risk_{key}"
            )])

        text = (
            f"🎯 RISK MODE\n\n"
            f"Current: {self.engine.live_balance_mgr.mode.emoji} "
            f"{self.engine.live_balance_mgr.mode.name}\n\n"
            f"Select a mode:"
        )
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def cmd_seed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Activate SEED mode — designed for $1 starts.
        Usage: /seed [amount] — e.g., /seed 1, /seed 2.50
        Automatically switches to LIVE mode.
        """
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        # Check if live trading is ready
        if not self.engine.live_trader.is_ready:
            await update.message.reply_text(
                "❌ Live trading not configured!\n\n"
                "Set POLY_PRIVATE_KEY in Railway environment variables.\n"
                "(0x + 64 hex chars from MetaMask)\n\n"
                "⚠️ POLY_API_KEY/SECRET/PASSPHRASE are NOT needed —\n"
                "they are auto-derived from your private key!\n\n"
                "Then redeploy. Check logs for:\n"
                "✅ Live trader initialized successfully"
            )
            return

        # Parse optional starting balance
        args = context.args
        starting_balance = 1.0  # Default $1
        if args:
            try:
                starting_balance = float(args[0])
                if starting_balance < 0.50:
                    await update.message.reply_text("❌ Minimum starting balance is $0.50")
                    return
                if starting_balance > 100:
                    await update.message.reply_text(
                        "💡 With $100+ you should use /risk concentration or medium instead."
                    )
                    return
            except ValueError:
                await update.message.reply_text("❌ Usage: /seed [amount]\nExample: /seed 1")
                return

        # Try to fetch real balance from Polymarket
        real_balance = await self.engine.live_trader.fetch_balance()
        if real_balance is not None and real_balance > 0:
            starting_balance = real_balance

        # Set seed mode and update balance
        self.engine.live_balance_mgr.update_balance(starting_balance)
        self.engine.live_balance_mgr.set_mode('seed')

        # AUTO-SWITCH TO LIVE MODE
        self.engine.trading_mode = 'live'

        from trading.live_balance_manager import SEED_GRADUATE_BALANCE
        bal_source = "fetched from Polymarket" if real_balance else "manual"
        msg = (
            f"🌱 SEED MODE ACTIVATED — LIVE\n\n"
            f"💰 Balance: ${starting_balance:.2f} ({bal_source})\n"
            f"🔴 Trading: LIVE MODE\n"
            f"🎯 Goal: Grow to ${SEED_GRADUATE_BALANCE:.2f}\n\n"
            f"📋 Rules:\n"
            f"• Only guaranteed-profit strategies (arb-only)\n"
            f"• 90%+ confidence required\n"
            f"• 1 position at a time (focused)\n"
            f"• Zero reserve — every cent works\n"
            f"• Auto-upgrades to � PLANT at ${SEED_GRADUATE_BALANCE:.2f}\n\n"
            f"Strategies active:\n"
            f"  ✅ YES+NO Arb (guaranteed)\n"
            f"  ✅ Cross-Timeframe Arb (guaranteed)\n"
            f"  ✅ Oracle Arb (high-confidence)\n"
            f"  ❌ Everything else (too risky)\n\n"
            f"Start trading with /trade"
        )

        await update.message.reply_text(msg)

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed open positions."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        trader = self.engine.active_trader
        positions = trader.get_open_positions()
        mode_tag = '🔴LIVE' if self.engine.trading_mode == 'live' else '📋PAPER'

        if not positions:
            await update.message.reply_text(f"📊 No open positions ({mode_tag})")
            return

        lines = [f"📊 Open Positions ({mode_tag}) — {len(positions)} total\n"]
        for i, p in enumerate(positions, 1):
            status = p.get('status', 'open')
            status_tag = '⏳' if status == 'pending' else '🟢'
            entry = p.get('entry_price', 0)
            size = p.get('size_usd', 0)
            coin = p.get('coin', '?')
            direction = p.get('direction', '?')
            strategy = p.get('strategy', '?')
            entry_time = p.get('entry_time', '?')

            lines.append(
                f"{status_tag} #{i}. {coin} {direction}\n"
                f"   Entry: ${entry:.3f} | Size: ${size:.2f}\n"
                f"   Strategy: {strategy}\n"
                f"   Time: {entry_time}\n"
            )

        await update.message.reply_text('\n'.join(lines))

    async def cmd_estop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Emergency stop — cancel all orders and stop trading."""
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        text = "🛑 EMERGENCY STOP\n\n"

        # Cancel live orders
        if self.engine.live_trader.is_ready:
            count = await self.engine.live_trader.cancel_all_orders()
            text += f"Cancelled {count} live orders\n"

        # Stop engine
        await self.engine.stop()
        text += "Trading stopped\n"

        # Switch to paper
        self.engine.trading_mode = 'paper'
        text += "Switched to paper mode"

        await update.message.reply_text(text)

    async def cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show live trading diagnostics — helps debug Railway config issues."""
        lines = ["🔧 LIVE TRADING DEBUG\n"]

        # 1. Check env vars
        pk = Config.POLY_PRIVATE_KEY.strip() if Config.POLY_PRIVATE_KEY else ''
        lines.append(f"POLY_PRIVATE_KEY: {'✅ set (' + str(len(pk)) + ' chars)' if pk else '❌ NOT SET'}")
        if pk:
            lines.append(f"  Starts with 0x: {'✅' if pk.startswith('0x') else '❌ (will auto-add)'}")
            expected = 66 if pk.startswith('0x') else 64
            lines.append(f"  Length: {len(pk)} (expected {expected})")

        funder = Config.POLY_FUNDER_ADDRESS.strip() if Config.POLY_FUNDER_ADDRESS else ''
        lines.append(f"POLY_FUNDER_ADDRESS: {'set' if funder else 'blank (auto-derive)'}")

        api = Config.POLY_API_KEY.strip() if Config.POLY_API_KEY else ''
        lines.append(f"POLY_API_KEY: {'set (manual)' if api else 'blank (auto-derive) ✅'}")

        lines.append(f"POLY_SIGNATURE_TYPE: {Config.POLY_SIGNATURE_TYPE}")
        lines.append(f"TRADING_MODE (env): {Config.TRADING_MODE}")
        lines.append(f"CLOB_API_URL: {Config.CLOB_API_URL}")

        proxy = Config.PROXY_URL
        lines.append(f"PROXY_URL: {'✅ ' + proxy[:25] + '...' if proxy else '❌ NOT SET (geoblock risk!)'}")


        # 2. Derive wallet address
        wallet = Config.derive_wallet_address()
        if wallet:
            lines.append(f"\nDerived wallet: {wallet[:10]}...{wallet[-4:]}")
        else:
            lines.append(f"\nDerived wallet: ❌ FAILED")

        # 3. Check live trader state
        if self.engine:
            lt = self.engine.live_trader
            lines.append(f"\n_initialized: {lt._initialized}")
            lines.append(f"clob_client: {'✅ set' if lt.clob_client else '❌ None'}")
            lines.append(f"is_ready: {lt.is_ready}")
            lines.append(f"trading_paused: {lt._trading_paused}")
            if lt._pause_reason:
                lines.append(f"pause_reason: {lt._pause_reason}")
            if hasattr(lt, '_init_error') and lt._init_error:
                lines.append(f"\n❌ INIT ERROR:\n{lt._init_error}")
            lines.append(f"\nRuntime mode: {self.engine.trading_mode}")

        # 4. Geoblock check
        try:
            import requests
            geo = requests.get('https://polymarket.com/api/geoblock', timeout=5).json()
            blocked = geo.get('blocked', True)
            country = geo.get('country', '?')
            ip = geo.get('ip', '?')
            lines.append(f"\nGeoblock: {'🚫 BLOCKED' if blocked else '✅ OK'} (country: {country}, ip: {ip})")
        except Exception as e:
            lines.append(f"\nGeoblock check failed: {e}")

        await update.message.reply_text('\n'.join(lines))

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reset drawdown tracking and consecutive loss counter."""
        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized")
            return
        msg = self.engine.live_balance_mgr.reset_tracking()
        await update.message.reply_text(msg)

    async def cmd_drawdown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show risk tracking status (alert-only, never blocks trading)."""
        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized")
            return
        mgr = self.engine.live_balance_mgr
        lines = [
            "📊 RISK TRACKING STATUS\n",
            f"Balance: ${mgr.balance:.2f}",
            f"Peak balance: ${mgr.peak_balance:.2f}",
            f"Drawdown from peak: {mgr.drawdown_pct:.1f}%",
            f"Daily PnL: {mgr.daily_pnl_pct:+.1f}%",
            f"Alert sent: {'🟡 YES' if mgr._drawdown_alerted else '🟢 NO'}",
            f"\n📊 CONSECUTIVE TRACKING",
            f"Losses: {mgr._consecutive_losses} | Wins: {mgr._consecutive_wins}",
            f"Size multiplier: {mgr._size_multiplier:.2f}×",
            f"\nℹ️ Bot NEVER stops trading. Use /resume to reset tracking.",
        ]
        await update.message.reply_text('\n'.join(lines))

    async def cmd_stratstats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show strategy win rate tracking stats."""
        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized")
            return
        tracker = getattr(self.engine.dynamic_picker, 'tracker', None)
        if not tracker or not tracker.records:
            await update.message.reply_text("📊 No strategy data yet — need trade results first.")
            return

        lines = ["📊 STRATEGY WIN RATES\n"]
        stats = tracker.get_stats()
        # Sort by total trades descending
        for name, s in sorted(stats.items(), key=lambda x: x[1]['total'], reverse=True):
            emoji = '🟢' if s['win_rate'] >= 55 else '🔴' if s['win_rate'] < 45 else '🟡'
            adj = s['adjustment']
            adj_str = f"+{adj:.2f}" if adj >= 0 else f"{adj:.2f}"
            lines.append(
                f"{emoji} {name}: {s['wins']}W/{s['losses']}L "
                f"({s['win_rate']:.0f}%) PnL:${s['pnl']:+.2f} adj:{adj_str}"
            )
        await update.message.reply_text('\n'.join(lines))

    async def cmd_automigrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle auto-migration (auto-graduate + auto-demote).
        
        Usage:
          /automigrate       — show current status + toggle
          /automigrate on    — enable auto-migrate
          /automigrate off   — disable (lock current mode)
        """
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        mgr = self.engine.live_balance_mgr
        args = context.args
        if args:
            arg = args[0].lower()
            if arg in ('on', 'true', '1', 'yes'):
                mgr.auto_migrate = True
            elif arg in ('off', 'false', '0', 'no'):
                mgr.auto_migrate = False
            else:
                await update.message.reply_text("❌ Usage: /automigrate [on|off]")
                return
        else:
            # Toggle
            mgr.auto_migrate = not mgr.auto_migrate

        status = "ON ✅" if mgr.auto_migrate else "OFF 🔒"
        mode = f"{mgr.mode.emoji} {mgr.mode.name}"
        msg = (
            f"🔄 Auto-Migrate: {status}\n\n"
            f"Current mode: {mode}\n"
        )
        if mgr.auto_migrate:
            msg += "Mode will auto-graduate/demote based on balance."
        else:
            msg += f"Mode locked to {mode} — won't change with balance."
        await update.message.reply_text(msg)

    async def cmd_cheaphunter(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle Cheap Hunter lottery mode.
        
        When ON: scans ALL timeframes (5, 15, 30) with ONLY cheap_hunter
        and penny_sniper strategies. Fixed $1 bets, up to 10 positions.
        
        The math: lose $1 on 10 markets = -$10. Win $100 on 1 = +$90 net.
        
        Usage: /cheaphunter — toggles on/off
        """
        if not self.engine:
            await update.message.reply_text("⚠️ Engine not initialized")
            return

        if not self.engine.live_trader.is_ready:
            await update.message.reply_text(
                "❌ Live trading not configured!\n"
                "Set POLY_PRIVATE_KEY first, then /cheaphunter"
            )
            return

        # Toggle mode
        if self.engine.cheap_hunter_mode:
            # TURN OFF
            self.engine.cheap_hunter_mode = False
            self.engine.live_balance_mgr.cheap_hunter_mode = False
            # Restore previous timeframes
            if self.engine._cheap_hunter_prev_timeframes:
                self.engine.active_timeframes = self.engine._cheap_hunter_prev_timeframes
                self.engine._cheap_hunter_prev_timeframes = None

            msg = (
                "🎰 CHEAP HUNTER MODE: OFF\n\n"
                "Restored normal trading.\n"
                f"Timeframes: {self.engine.active_timeframes}\n"
                f"Mode: {self.engine.live_balance_mgr.mode.emoji} "
                f"{self.engine.live_balance_mgr.mode.name}"
            )
        else:
            # TURN ON
            self.engine._cheap_hunter_prev_timeframes = list(self.engine.active_timeframes)
            self.engine.active_timeframes = [5, 15, 30]  # All timeframes
            self.engine.cheap_hunter_mode = True
            self.engine.live_balance_mgr.cheap_hunter_mode = True

            # Auto-switch to live mode
            if self.engine.trading_mode != 'live':
                self.engine.trading_mode = 'live'

            # Start trading if not already
            if not self.engine.is_running:
                await self.engine.start()

            bal = self.engine.live_balance_mgr.balance
            max_bets = int(bal / 1.0) if bal >= 1.0 else 0

            msg = (
                f"🎰 CHEAP HUNTER MODE: ON\n\n"
                f"💰 Balance: ${bal:.2f} — up to {max_bets} simultaneous $1 bets\n"
                f"⏱️ Timeframes: 5min, 15min, 30min\n"
                f"🧠 Strategies: cheap_hunter + penny_sniper ONLY\n"
                f"💵 Bet size: $1 per market (fixed)\n"
                f"📊 Position limit: 10 max\n\n"
                f"📐 THE MATH:\n"
                f"  • Buy outcomes at $0.01-$0.08\n"
                f"  • Lose $1 on 10 markets = -$10\n"
                f"  • 1 winner pays $1.00 per share\n"
                f"  • $0.01 → $1.00 = 100x ($100 on $1)\n"
                f"  • Net: +$90 if 1-in-11 hits\n\n"
                f"⚠️ Safety: No buying in last 45 seconds\n"
                f"    (market already decided by then)\n\n"
                f"Use /cheaphunter again to turn OFF"
            )

        await update.message.reply_text(msg)

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

        # Use runtime trading_mode, NOT Config.is_paper()
        is_live = self.engine and self.engine.trading_mode == 'live'
        mode_tag = '🔴 LIVE' if is_live else '📋 Paper'

        extra = ''
        if is_live:
            m = self.engine.live_balance_mgr.mode
            bal = self.engine.live_balance_mgr.balance
            extra = f"\nRisk: {m.emoji} {m.name}\nBalance: ${bal:.2f}"

        await query.edit_message_text(
            f"▶️ Trading started on: **{', '.join(f'{t}min' for t in timeframes)}**\n\n"
            f"Strategy: ⚡ Dynamic (auto-select)\n"
            f"Mode: {mode_tag}{extra}",
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

    async def cb_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle mode selection inline buttons."""
        query = update.callback_query
        await query.answer()
        data = query.data  # e.g. "mode_paper" or "mode_live" or "mode_na"

        if data == 'mode_na':
            await query.edit_message_text(
                "❌ Live trading not configured\n\n"
                "Set POLY_PRIVATE_KEY in Railway env vars.\n"
                "API_KEY/SECRET/PASSPHRASE are auto-derived — leave them blank!\n\n"
                "Then redeploy."
            )
            return

        mode = data.replace('mode_', '')
        if self.engine:
            ok, msg = self.engine.switch_mode(mode)
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text("⚠️ Engine not initialized")

    async def cb_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle risk mode selection inline buttons."""
        query = update.callback_query
        await query.answer()
        data = query.data  # e.g. "risk_concentration"

        risk_mode = data.replace('risk_', '')
        if self.engine:
            ok, msg = self.engine.set_risk_mode(risk_mode)
            if ok:
                # User explicitly chose a mode — lock it (disable auto-migrate)
                self.engine.live_balance_mgr.auto_migrate = False
                msg += "\n🔒 Auto-migrate OFF (mode locked to your choice)"
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text("⚠️ Engine not initialized")

    # ═══════════════════════════════════════════════════════════════════
    # NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════════════

    async def send_message(self, text: str):
        """Send a general message to the configured chat."""
        if not Config.TELEGRAM_CHAT_ID or not self.app:
            return
        try:
            await self.app.bot.send_message(
                chat_id=Config.TELEGRAM_CHAT_ID, text=text,
            )
        except Exception as e:
            print(f"❌ Telegram send error: {e}")

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
