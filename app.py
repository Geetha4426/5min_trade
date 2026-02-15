"""
5min_trade — Entry Point

Runs the Telegram bot + trading engine concurrently.
The trading engine scans markets, runs strategies, and executes paper trades.
"""

import asyncio
import sys
import signal

from config import Config
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

        # State
        self.is_running = False
        self._scan_task = None
        self._ws_tasks = []

    async def init(self):
        """Initialize all components."""
        Config.print_status()
        await self.db.init()
        if Config.TELEGRAM_BOT_TOKEN:
            await self.bot.setup()
        else:
            print("⚠️ No TELEGRAM_BOT_TOKEN — running without Telegram")
        print("✅ All components initialized")

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
                    # Run for one cycle, then re-discover
                    try:
                        await asyncio.wait_for(self.poly_feed.run(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                else:
                    print("⚠️ No markets found, retrying in 30s...")
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ Poly feed error: {e}")
                await asyncio.sleep(10)

    async def _scan_loop(self):
        """Main trading loop — CONTINUOUS. Scans all markets, runs all strategies."""
        print("🔄 Continuous scan loop started")
        scan_count = 0

        while self.is_running:
            try:
                scan_count += 1
                markets = self.gamma.discover_markets(
                    timeframes=self.active_timeframes
                )

                if not markets:
                    await asyncio.sleep(5)
                    continue

                # Get balance tier preferences
                balance_prefs = self.risk_manager.get_balance_preferences()

                # Log tier status periodically
                if scan_count % 50 == 1:
                    stats = self.risk_manager.get_stats()
                    print(f"📊 [{stats.get('tier_emoji','')} {stats.get('tier','')}] "
                          f"Balance: ${stats['balance']:.2f} | "
                          f"Trades: {stats['total_trades']} | "
                          f"Win: {stats['win_rate']:.0f}% | "
                          f"Markets: {len(markets)}")

                # Run strategy on EVERY market
                strategy = self.strategies.get(self.active_strategy, self.dynamic_picker)

                for market in markets:
                    if not self.is_running:
                        break

                    seconds_remaining = self.gamma.get_seconds_remaining(market)
                    context = {
                        'clob': self.clob,
                        'poly_feed': self.poly_feed,
                        'binance_feed': self.binance_feed,
                        'seconds_remaining': seconds_remaining,
                    }

                    # Dynamic picker gets balance preferences
                    if hasattr(strategy, 'analyze') and self.active_strategy == 'dynamic':
                        signal = await strategy.analyze(market, context, balance_prefs)
                    else:
                        signal = await strategy.analyze(market, context)

                    if signal:
                        trade = await self.paper_trader.execute_signal(signal)
                        if trade:
                            await self.bot.send_trade_alert(trade)

                # Check open positions with dynamic hold/sell
                current_prices = {}
                for tid, snap in self.poly_feed.latest_prices.items():
                    current_prices[tid] = snap.price

                secs_map = {}
                for m in markets:
                    secs_map[m['market_id']] = self.gamma.get_seconds_remaining(m)

                closed = await self.paper_trader.check_positions(current_prices, secs_map)
                for trade in closed:
                    await self.bot.send_close_alert(trade)

                # Scan interval from config
                min_tf = min(self.active_timeframes) if self.active_timeframes else 15
                interval = Config.get_timeframe_params(min_tf).get('scan_interval', 2)
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
        print("🤖 Starting Telegram bot...")
        await engine.bot.app.initialize()
        await engine.bot.app.start()
        await engine.bot.app.updater.start_polling(drop_pending_updates=True)
        print("✅ Telegram bot is running!")
    else:
        print("⚠️ No Telegram — auto-starting trading loop...")
        await engine.start()

    # Auto-start trading if configured
    print("\n💡 Send /trade in Telegram to start, or press Ctrl+C to quit\n")

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
