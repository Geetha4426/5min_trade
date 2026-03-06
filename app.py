"""
5min_trade — Entry Point

Runs the Telegram bot + trading engine concurrently.
The trading engine scans markets, runs strategies, and executes paper trades.
"""

import asyncio
import os
import sys
import signal
import time

from config import Config

# ═══════════════════════════════════════════════════════════════════
# PROXY SETUP — must happen BEFORE any HTTP requests
# ═══════════════════════════════════════════════════════════════════
if Config.PROXY_URL:
    os.environ['HTTP_PROXY'] = Config.PROXY_URL
    os.environ['HTTPS_PROXY'] = Config.PROXY_URL
    # Some libraries check lowercase
    os.environ['http_proxy'] = Config.PROXY_URL
    os.environ['https_proxy'] = Config.PROXY_URL
    print(f"🌐 Proxy configured: {Config.PROXY_URL[:30]}...", flush=True)
else:
    print("🌐 No proxy configured (PROXY_URL not set)", flush=True)

from data.gamma_client import GammaClient
from data.clob_client import ClobClient
from data.websocket_feed import PolymarketFeed, BinanceFeed
from data.database import Database
from strategies.dynamic_picker import DynamicPicker
from strategies.flash_crash import CheapOutcomeHunter, MomentumReversal
from strategies.oracle_arb import OracleArbStrategy
from strategies.yes_no_arb import YesNoArbStrategy
from strategies.time_decay import TimeDecayStrategy
from strategies.continuous import (
    TrendFollower, StraddleStrategy, SpreadScalper, MidPriceSniper
)
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskManager
from trading.live_trader import LiveTrader
from trading.live_balance_manager import LiveBalanceManager
from trading.auto_redeem import AutoRedeemer
from bot.main import TelegramBot


class TradingEngine:
    """Core engine — 9 strategies, continuous trading, balance-tier aware."""

    def __init__(self):
        # Data layer
        self.gamma = GammaClient()
        self.clob = ClobClient()
        self.poly_feed = PolymarketFeed()
        self.binance_feed = BinanceFeed()
        self.db = Database()

        # Trading (with dynamic balance tiers)
        self.risk_manager = RiskManager()
        self.paper_trader = PaperTrader(self.db, self.risk_manager)

        # Live trading — start with $0 balance; real balance fetched from Polymarket on init
        # STARTING_BALANCE is ONLY used for paper trading, never for live
        self.live_balance_mgr = LiveBalanceManager(
            balance=0.0,  # Will be synced to real USDC balance on init()
            mode=Config.LIVE_RISK_MODE,
        )
        self.live_trader = LiveTrader(self.db, self.live_balance_mgr)
        self.live_trader.clob_reader = self.clob  # Orderbook reader for smart exits

        # Active trading mode: 'paper' or 'live'
        self.trading_mode = Config.TRADING_MODE

        # ALL 9 strategies via dynamic picker
        self.dynamic_picker = DynamicPicker()
        self.strategies = {
            'cheap_hunter': CheapOutcomeHunter(),
            'momentum_reversal': MomentumReversal(),
            'oracle_arb': OracleArbStrategy(),
            'yes_no_arb': YesNoArbStrategy(),
            'time_decay': TimeDecayStrategy(),
            'trend_follower': TrendFollower(),
            'straddle': StraddleStrategy(),
            'spread_scalper': SpreadScalper(),
            'mid_sniper': MidPriceSniper(),
            'dynamic': self.dynamic_picker,
        }
        self.active_strategy = 'dynamic'
        self.active_timeframes = list(Config.ENABLED_TIMEFRAMES)

        # Telegram bot
        self.bot = TelegramBot(engine=self)

        # Auto-redeem resolved positions (gasless via builder relayer)
        self.auto_redeemer = None  # Initialized in init() after live_trader

        # State
        self.is_running = False
        self._scan_task = None
        self._ws_tasks = []

        # ── Cheap Hunter dedicated mode ──
        # When True: only runs cheap_hunter + penny_sniper, all timeframes,
        # $1 per bet, up to 10 positions. The math: lose $1 on 10 bets = -$10,
        # but 1 win at $1→$100 = +$90 net. Toggled via /cheaphunter command.
        self.cheap_hunter_mode = False
        self._cheap_hunter_prev_timeframes = None  # Restore when mode exits

    @property
    def active_trader(self):
        """Returns the currently active trader (paper or live)."""
        if self.trading_mode == 'live' and self.live_trader.is_ready:
            return self.live_trader
        return self.paper_trader

    def switch_mode(self, mode: str) -> tuple:
        """Switch trading mode. Returns (success, message)."""
        mode = mode.lower()
        if mode == 'live':
            if not self.live_trader.is_ready:
                return False, '❌ Live trader not initialized. Check POLY_PRIVATE_KEY.'
            self.trading_mode = 'live'
            return True, f'🔴 LIVE MODE — {self.live_balance_mgr.mode.emoji} {self.live_balance_mgr.mode.name}'
        elif mode == 'paper':
            self.trading_mode = 'paper'
            return True, '📋 Paper mode activated'
        return False, f'Unknown mode: {mode}'

    def set_risk_mode(self, risk_mode: str) -> tuple:
        """Set live trading risk mode. Returns (success, message)."""
        if self.live_balance_mgr.set_mode(risk_mode):
            m = self.live_balance_mgr.mode
            return True, f'{m.emoji} {m.name}: {m.description}'
        return False, f'Unknown risk mode: {risk_mode}. Use: seed, concentration, medium, aggressive'

    async def _initial_redeem_check(self):
        """Run a one-time redeem check on startup to unlock stuck funds."""
        try:
            await asyncio.sleep(5)  # Let everything settle
            if self.auto_redeemer:
                result = await self.auto_redeemer.force_check()
                if result.get('redeemed', 0) > 0:
                    amount = result.get('total_redeemed_usd', 0)
                    print(f"💰 Startup redeem: {result['redeemed']} positions → ${amount:.2f} USDC", flush=True)
                    await self.bot.send_message(
                        f"💰 Auto-redeemed {result['redeemed']} resolved positions!\n"
                        f"Recovered: ~${amount:.2f} USDC"
                    )
                    # Re-sync balance
                    real_bal = await self.live_trader.fetch_balance()
                    if real_bal is not None:
                        self.live_balance_mgr.update_balance(real_bal)
                        print(f"💰 Balance re-synced: ${real_bal:.2f}", flush=True)
                else:
                    print("💰 No resolved positions to redeem at startup", flush=True)
        except Exception as e:
            print(f"⚠️ Startup redeem check failed: {e}", flush=True)

    async def _startup_approval_and_redeem(self):
        """Ensure CTF exchange approvals, then run initial redeem check."""
        try:
            await asyncio.sleep(3)
            if self.auto_redeemer:
                print("🔑 Checking CTF exchange approvals on-chain...", flush=True)
                # Check on-chain first — only send tx if NOT approved.
                # Force=True was burning gas on every restart/deploy, creating
                # queued txs that ate the entire MATIC balance.
                await self.auto_redeemer.ensure_ctf_approval(force=False)
        except Exception as e:
            print(f"⚠️ Startup approval failed: {e}", flush=True)

        # Then do the original redeem check
        await self._initial_redeem_check()

    async def init(self):
        """Initialize all components."""
        Config.print_status()
        await self.db.init()

        # Check Polymarket geoblock (WEBSITE only — CLOB API is separate)
        try:
            import requests
            geo = requests.get('https://polymarket.com/api/geoblock', timeout=5).json()
            ip = geo.get('ip', '?')
            country = geo.get('country', '?')
            region = geo.get('region', '?')
            blocked = geo.get('blocked', True)
            if blocked:
                print(f"⚠️ Website geo-blocked: IP {ip} | {country} | {region}", flush=True)
                print(f"   ℹ️ NOTE: This checks the Polymarket WEBSITE, not the CLOB API.", flush=True)
                print(f"   The CLOB API (clob.polymarket.com) works independently from most regions.", flush=True)
                print(f"   EU-West Railway works fine for trading — poly_trade proved this.", flush=True)
                if Config.is_relay_enabled():
                    print(f"  🔀 Relay configured as fallback: {Config.get_clob_url()}", flush=True)
            else:
                print(f"✅ Geoblock OK — IP: {ip} | Country: {country} | Region: {region}", flush=True)
        except Exception as e:
            print(f"⚠️ Geoblock check failed: {e} (non-critical)", flush=True)

        # Initialize live trader (non-blocking — will just warn if no keys)
        live_ok = await self.live_trader.init()
        if live_ok:
            print(f"🟢 Live trader ready — Mode: {self.live_balance_mgr.mode.emoji} "
                  f"{self.live_balance_mgr.mode.name}", flush=True)

            # Sync balance manager with REAL Polymarket balance
            real_bal = await self.live_trader.fetch_balance()
            if real_bal is not None and real_bal > 0:
                self.live_balance_mgr.update_balance(real_bal)
                print(f"💰 Balance synced: ${real_bal:.2f} (real)", flush=True)
            else:
                print(f"⚠️ Using configured balance: ${self.live_balance_mgr.balance:.2f}", flush=True)

            # Auto-demote mode if balance dropped below the tier's entry threshold
            demote_msg = self.live_balance_mgr.check_auto_demote()
            if demote_msg:
                print(f"{demote_msg}", flush=True)

            # Warn if balance is too low to trade at all
            if self.live_balance_mgr.balance < Config.POLYMARKET_MIN_ORDER_SIZE:
                print(f"\n{'='*60}", flush=True)
                print(f"⚠️  BALANCE TOO LOW: ${self.live_balance_mgr.balance:.2f} < ${Config.POLYMARKET_MIN_ORDER_SIZE:.2f} minimum", flush=True)
                print(f"   Signals will fire but NO trades can execute.", flush=True)
                print(f"   Options:", flush=True)
                print(f"   1. Deposit USDC at https://polymarket.com", flush=True)
                print(f"   2. Wait for auto-redeem to claim resolved positions", flush=True)
                print(f"   3. Manually claim resolved positions on polymarket.com", flush=True)
                print(f"{'='*60}\n", flush=True)

            # Initialize auto-redeemer (direct on-chain or gasless relayer)
            try:
                sig_type = getattr(self.live_trader, '_sig_type', 0)
                self.auto_redeemer = AutoRedeemer(
                    self.live_trader.clob_client,
                    sig_type=sig_type
                )
                if self.auto_redeemer.init():
                    status = self.auto_redeemer.get_status()
                    print(f"💰 Auto-redeemer ready ({status['method']}) — "
                          f"will auto-redeem resolved positions", flush=True)
                    # Ensure CTF exchange approvals (required for selling)
                    asyncio.create_task(self._startup_approval_and_redeem())
                else:
                    print("⚠️ Auto-redeem disabled — check logs above", flush=True)
                    self.auto_redeemer = None
            except Exception as e:
                print(f"⚠️ Auto-redeem init failed: {e}", flush=True)
                self.auto_redeemer = None
        else:
            print("📋 Paper trading only (no live credentials)", flush=True)
            self.trading_mode = 'paper'

        if Config.TELEGRAM_BOT_TOKEN:
            await self.bot.setup()
        else:
            print("⚠️ No TELEGRAM_BOT_TOKEN — running without Telegram")

        print(f"✅ All components initialized — Mode: {self.trading_mode.upper()}", flush=True)

    async def start(self):
        """Start the trading loop."""
        if self.is_running:
            return

        self.is_running = True
        print(f"▶️ Trading started — Strategy: {self.active_strategy} | "
              f"Timeframes: {self.active_timeframes}")

        # Start WebSocket feeds in background
        ws_poly = asyncio.create_task(self._run_poly_feed())
        ws_binance = asyncio.create_task(self.binance_feed.run())
        self._ws_tasks = [ws_poly, ws_binance]

        # Start scan loop
        self._scan_task = asyncio.create_task(self._scan_loop())

    async def stop(self):
        """Stop trading."""
        self.is_running = False
        if self._scan_task:
            self._scan_task.cancel()
        for task in self._ws_tasks:
            task.cancel()
        await self.poly_feed.stop()
        await self.binance_feed.stop()
        print("⏹️ Trading stopped")

    async def _run_poly_feed(self):
        """Start Polymarket WebSocket with auto-discovery of tokens."""
        while self.is_running:
            try:
                # Discover current markets
                markets = self.gamma.discover_markets()
                token_ids = []
                for m in markets:
                    if m.get('up_token_id'):
                        token_ids.append(m['up_token_id'])
                    if m.get('down_token_id'):
                        token_ids.append(m['down_token_id'])

                if token_ids:
                    await self.poly_feed.subscribe(token_ids)
                    print(f"🔌 Subscribing to {len(token_ids)} token feeds", flush=True)
                    try:
                        await asyncio.wait_for(self.poly_feed.run(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                else:
                    # Markets found but no token IDs — this is normal for events
                    # The scan loop still works without WS prices
                    if markets:
                        print(f"📡 {len(markets)} markets found but no token IDs for WS — using REST data", flush=True)
                    else:
                        print("⚠️ No markets found for WS subscription", flush=True)
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ Poly feed error: {e}", flush=True)
                await asyncio.sleep(10)

    async def _scan_loop(self):
        """Main trading loop — CONTINUOUS. Scans all markets, runs all strategies."""
        print("🔄 Continuous scan loop started")
        scan_count = 0
        _last_pnl_report = time.time()
        _last_balance_sync = time.time()

        while self.is_running:
            try:
                scan_count += 1
                markets = self.gamma.discover_markets(
                    timeframes=self.active_timeframes
                )

                if not markets:
                    await asyncio.sleep(5)
                    continue

                # Get strategy preferences based on ACTIVE trading mode
                if self.trading_mode == 'live':
                    balance_prefs = self.live_balance_mgr.get_strategy_filter()
                else:
                    balance_prefs = self.risk_manager.get_balance_preferences()

                # ── Cheap Hunter Mode overrides ──
                if self.cheap_hunter_mode:
                    balance_prefs = {
                        'enabled': ['cheap_hunter', 'penny_sniper'],
                        'disabled': [],
                        'min_confidence': 0.40,  # Low bar — these are lottery tickets
                    }

                # Log status periodically
                if scan_count % 50 == 1:
                    if self.cheap_hunter_mode:
                        bal = self.live_balance_mgr.balance
                        pos = self.live_balance_mgr.open_positions
                        print(f"🎰 [CHEAP HUNTER] "
                              f"Balance: ${bal:.2f} | "
                              f"Positions: {pos}/10 | "
                              f"Markets: {len(markets)}")
                    elif self.trading_mode == 'live':
                        m = self.live_balance_mgr.mode
                        bal = self.live_balance_mgr.balance
                        print(f"📊 [{m.emoji} {m.name}] "
                              f"Balance: ${bal:.2f} | "
                              f"Enabled: {balance_prefs.get('enabled', 'all')} | "
                              f"Markets: {len(markets)}")
                    else:
                        stats = self.risk_manager.get_stats()
                        print(f"📊 [{stats.get('tier_emoji','')} {stats.get('tier','')}] "
                              f"Balance: ${stats['balance']:.2f} | "
                              f"Trades: {stats['total_trades']} | "
                              f"Win: {stats['win_rate']:.0f}% | "
                              f"Markets: {len(markets)}")

                # Run strategy on EVERY market
                strategy = self.strategies.get(self.active_strategy, self.dynamic_picker)

                # Feed fallback prices into ClobClient from gamma + WS
                for market in markets:
                    up_tid = market.get('up_token_id', '')
                    dn_tid = market.get('down_token_id', '')
                    if up_tid:
                        # Use WS price if available, else gamma price
                        ws_snap = self.poly_feed.latest_prices.get(up_tid)
                        price = ws_snap.price if ws_snap else market.get('up_price', 0.5)
                        self.clob.set_fallback_price(up_tid, price)
                    if dn_tid:
                        ws_snap = self.poly_feed.latest_prices.get(dn_tid)
                        price = ws_snap.price if ws_snap else market.get('down_price', 0.5)
                        self.clob.set_fallback_price(dn_tid, price)

                for market in markets:
                    if not self.is_running:
                        break

                    seconds_remaining = self.gamma.get_seconds_remaining(market)

                    # Skip expired markets
                    if seconds_remaining <= 0:
                        continue

                    context = {
                        'clob': self.clob,
                        'poly_feed': self.poly_feed,
                        'binance_feed': self.binance_feed,
                        'seconds_remaining': seconds_remaining,
                        'balance_mgr': self.live_balance_mgr if self.trading_mode == 'live' else None,
                    }

                    # Dynamic picker gets balance preferences
                    try:
                        if hasattr(strategy, 'analyze') and self.active_strategy == 'dynamic':
                            signal = await strategy.analyze(market, context, balance_prefs)
                        else:
                            signal = await strategy.analyze(market, context)
                    except Exception as e:
                        if scan_count <= 3:
                            print(f"❌ Strategy error on {market.get('coin','?')}: {e}", flush=True)
                        continue

                    if signal:
                        mode_tag = '🔴LIVE' if self.trading_mode == 'live' else '📋PAPER'
                        print(f"🎯 [{mode_tag}] Signal: {signal.strategy} -> {market.get('coin','?')} "
                              f"{signal.direction} @ {signal.entry_price:.4f} "
                              f"(conf={signal.confidence:.0%})", flush=True)
                        try:
                            trade = await self.active_trader.execute_signal(signal)
                        except Exception as exec_err:
                            print(f"❌ Execute error ({signal.coin} {signal.direction}): {exec_err}", flush=True)
                            trade = None
                        if trade:
                            await self.bot.send_trade_alert(trade)
                            print(f"✅ Trade executed: {trade.get('coin','?')} {trade.get('direction','?')}", flush=True)

                            # Check seed mode auto-graduation
                            grad_msg = self.live_balance_mgr.check_auto_graduate()
                            if grad_msg:
                                print(f"🎉 {grad_msg}", flush=True)
                                await self.bot.send_message(grad_msg)

                # Log how many markets were scanned
                if scan_count <= 5 or scan_count % 100 == 0:
                    active_markets = [m for m in markets if self.gamma.get_seconds_remaining(m) > 0]
                    print(f"🔍 Scan #{scan_count}: {len(active_markets)}/{len(markets)} active markets scanned", flush=True)

                # Check open positions with dynamic hold/sell
                current_prices = {}
                for tid, snap in self.poly_feed.latest_prices.items():
                    current_prices[tid] = snap.price

                secs_map = {}
                for m in markets:
                    secs_map[m['market_id']] = self.gamma.get_seconds_remaining(m)

                closed = await self.active_trader.check_positions(current_prices, secs_map)
                for trade in closed:
                    await self.bot.send_close_alert(trade)
                    # ── Feed result to strategy win rate tracker ──
                    pnl = trade.get('pnl', 0) or 0
                    strat_name = trade.get('strategy', '')
                    if strat_name and hasattr(self.dynamic_picker, 'tracker'):
                        self.dynamic_picker.tracker.record(strat_name, pnl > 0, pnl)

                # ── Periodic balance sync (every 60s) ──
                # Picks up USDC from Polymarket auto-settlements, deposits, etc.
                # Without this, bot stays stuck at $0 until restart
                if self.trading_mode == 'live' and time.time() - _last_balance_sync >= 60:
                    _last_balance_sync = time.time()
                    try:
                        real_bal = await self.live_trader.fetch_balance()
                        if real_bal is not None and real_bal > 0:
                            old_bal = self.live_balance_mgr.balance
                            self.live_balance_mgr.update_balance(real_bal)
                            # If balance changed significantly, log it
                            if abs(real_bal - old_bal) >= 0.10:
                                print(f"💰 Balance sync: ${old_bal:.2f} → ${real_bal:.2f}", flush=True)
                                # Check if we should auto-demote or auto-graduate
                                demote_msg = self.live_balance_mgr.check_auto_demote()
                                if demote_msg:
                                    print(f"📉 {demote_msg}", flush=True)
                                    await self.bot.send_message(demote_msg)
                                grad_msg = self.live_balance_mgr.check_auto_graduate()
                                if grad_msg:
                                    print(f"🎉 {grad_msg}", flush=True)
                                    await self.bot.send_message(grad_msg)
                    except Exception as e:
                        if scan_count % 50 == 1:
                            print(f"⚠️ Balance sync error: {e}", flush=True)

                # ── Auto-redeem resolved positions ──
                if self.auto_redeemer and self.trading_mode == 'live':
                    try:
                        redeem_result = await self.auto_redeemer.check_and_redeem()
                        if redeem_result and redeem_result.get('redeemed', 0) > 0:
                            amt = redeem_result.get('total_redeemed_usd', 0)
                            print(f"💰 Auto-redeemed {redeem_result['redeemed']} positions → ${amt:.2f} USDC", flush=True)
                            await self.bot.send_message(
                                f"💰 Auto-redeemed {redeem_result['redeemed']} resolved positions!\n"
                                f"Recovered: ~${amt:.2f} USDC"
                            )
                            # Re-sync balance after redeem
                            real_bal = await self.live_trader.fetch_balance()
                            if real_bal is not None:
                                self.live_balance_mgr.update_balance(real_bal)
                    except Exception as e:
                        if scan_count % 100 == 1:
                            print(f"⚠️ Auto-redeem error: {e}", flush=True)

                # Scan interval from config
                min_tf = min(self.active_timeframes) if self.active_timeframes else 15
                interval = Config.get_timeframe_params(min_tf).get('scan_interval', 2)

                # Periodic PnL report every 10 minutes
                if time.time() - _last_pnl_report >= 600:
                    _last_pnl_report = time.time()
                    summary = self.active_trader.get_summary()
                    open_pos = self.active_trader.get_open_positions()
                    mode_tag = '🔴LIVE' if self.trading_mode == 'live' else '📋PAPER'
                    print(f"📊 [{mode_tag}] PnL Report | Balance: ${summary.get('balance', 0):.2f} | "
                          f"Trades: {summary.get('total_trades', 0)} | "
                          f"Win: {summary.get('win_rate', 0):.0f}% | "
                          f"Open: {len(open_pos)}", flush=True)
                    await self.bot.send_pnl_report(summary, open_pos)

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ Scan error: {e}")
                await asyncio.sleep(3)

        print("🔄 Scan loop stopped")


async def main():
    """Entry point."""
    engine = TradingEngine()
    await engine.init()

    # Run telegram bot
    if engine.bot.app:
        print("🤖 Starting Telegram bot...", flush=True)
        await engine.bot.app.initialize()
        print("✅ Bot initialized", flush=True)

        # Set commands now that bot is initialized
        if getattr(engine.bot, '_commands_pending', False):
            try:
                from telegram import BotCommand
                await engine.bot.app.bot.set_my_commands([
                    BotCommand("start", "Welcome & menu"),
                    BotCommand("trade", "Start trading"),
                    BotCommand("stop", "Stop trading"),
                    BotCommand("status", "Position & P&L status"),
                    BotCommand("balance", "Check balance"),
                    BotCommand("strategy", "View/change strategy"),
                    BotCommand("markets", "Scan live markets"),
                    BotCommand("history", "Trade history"),
                    BotCommand("settings", "Bot settings"),
                    BotCommand("drawdown", "Risk tracking status"),
                    BotCommand("stratstats", "Strategy win rates"),
                    BotCommand("resume", "Reset drawdown tracking"),
                ])
                print("✅ Bot commands registered", flush=True)
            except Exception as e:
                print(f"⚠️ Commands setup: {e}", flush=True)

        await engine.bot.app.start()
        await engine.bot.app.updater.start_polling(drop_pending_updates=True)
        print("✅ Telegram bot is polling!", flush=True)

        # Send startup notification to Telegram
        if Config.TELEGRAM_CHAT_ID:
            try:
                is_live = engine.trading_mode == 'live'
                mode = "🔴 LIVE" if is_live else "📋 PAPER"

                if is_live:
                    bal = engine.live_balance_mgr.balance
                    m = engine.live_balance_mgr.mode
                    bal_line = f"Balance: ${bal:.2f}\nRisk: {m.emoji} {m.name}\n"
                else:
                    stats = engine.risk_manager.get_stats()
                    bal_line = (
                        f"Balance: ${stats['balance']:.2f}\n"
                        f"Tier: {stats.get('tier_emoji','')} {stats.get('tier','')}\n"
                    )

                msg = (
                    f"🟢 *5MIN_TRADE is ONLINE*\n\n"
                    f"Mode: {mode}\n"
                    f"{bal_line}"
                    f"Strategies: 11 loaded\n"
                    f"Coins: {', '.join(Config.ENABLED_COINS)}\n\n"
                    f"Type /trade to start trading!\n"
                    f"Type /seed 1 for $1 arb-only mode."
                )
                await engine.bot.app.bot.send_message(
                    chat_id=Config.TELEGRAM_CHAT_ID,
                    text=msg,
                    parse_mode='Markdown'
                )
                print("✅ Startup message sent to Telegram", flush=True)
            except Exception as e:
                print(f"⚠️ Couldn't send startup msg: {e}", flush=True)
    else:
        print("⚠️ No Telegram — auto-starting trading loop...", flush=True)
        await engine.start()

    # Keep running
    print("\n💡 Bot is ready! Send /trade in Telegram to start trading.\n", flush=True)

    # Keep running
    try:
        stop_event = asyncio.Event()

        def handle_signal(*args):
            stop_event.set()

        # Handle graceful shutdown
        if sys.platform != 'win32':
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(signal.SIGINT, handle_signal)
            loop.add_signal_handler(signal.SIGTERM, handle_signal)
        
        await stop_event.wait()

    except (KeyboardInterrupt, SystemExit):
        print("\n⏹️ Shutting down...")

    finally:
        await engine.stop()
        if engine.bot.app:
            try:
                await engine.bot.app.updater.stop()
                await engine.bot.app.stop()
                await engine.bot.app.shutdown()
            except Exception:
                pass
        print("👋 Goodbye!")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye!")
