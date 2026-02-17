"""
5min_trade — Configuration

All settings with environment variable overrides.
Timeframe-specific parameters for 5m, 15m, 30m markets.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Central configuration for the 5min_trade bot."""

    # ═══════════════════════════════════════════════════════════════════
    # TELEGRAM
    # ═══════════════════════════════════════════════════════════════════
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # ═══════════════════════════════════════════════════════════════════
    # POLYMARKET WALLET
    # ═══════════════════════════════════════════════════════════════════
    POLY_PRIVATE_KEY = os.getenv('POLY_PRIVATE_KEY', '')
    POLY_SAFE_ADDRESS = os.getenv('POLY_SAFE_ADDRESS', '')
    POLY_API_KEY = os.getenv('POLY_API_KEY', '')
    POLY_API_SECRET = os.getenv('POLY_API_SECRET', '')
    POLY_PASSPHRASE = os.getenv('POLY_PASSPHRASE', '')

    # ═══════════════════════════════════════════════════════════════════
    # API ENDPOINTS
    # ═══════════════════════════════════════════════════════════════════
    GAMMA_API_URL = 'https://gamma-api.polymarket.com'
    CLOB_API_URL = 'https://clob.polymarket.com'
    BINANCE_WS_URL = 'wss://stream.binance.com:9443/ws'
    POLYMARKET_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
    POLYMARKET_LIVE_WS_URL = 'wss://ws-live-data.polymarket.com'

    # ═══════════════════════════════════════════════════════════════════
    # TRADING MODE
    # ═══════════════════════════════════════════════════════════════════
    TRADING_MODE = os.getenv('TRADING_MODE', 'paper')  # 'paper' or 'live'
    STARTING_BALANCE = float(os.getenv('STARTING_BALANCE', '100.0'))

    # ═══════════════════════════════════════════════════════════════════
    # COINS
    # ═══════════════════════════════════════════════════════════════════
    ENABLED_COINS = [c.strip().upper() for c in os.getenv('ENABLED_COINS', 'BTC,ETH,SOL').split(',')]

    # Binance symbol mapping
    BINANCE_SYMBOLS = {
        'BTC': 'btcusdt',
        'ETH': 'ethusdt',
        'SOL': 'solusdt',
        'XRP': 'xrpusdt',
    }

    # Polymarket slug prefixes per coin (for slug computation)
    COIN_PM = {
        'BTC': 'btc', 'ETH': 'eth', 'SOL': 'sol', 'XRP': 'xrp',
    }

    # Polymarket series slugs per timeframe (for series_id discovery)
    SERIES_SLUGS = {
        5: '{coin}-up-or-down-5m',    # e.g. btc-up-or-down-5m
        15: '{coin}-up-or-down-15m',
        30: '{coin}-up-or-down-30m',
    }

    # ═══════════════════════════════════════════════════════════════════
    # TIMEFRAME SETTINGS
    # Each timeframe has its own risk parameters
    # ═══════════════════════════════════════════════════════════════════
    ENABLED_TIMEFRAMES = [int(t) for t in os.getenv('ENABLED_TIMEFRAMES', '5,15').split(',')]

    TIMEFRAME_PARAMS = {
        5: {
            'name': '5 min',
            'scan_interval': 2,        # FAST: scan every 2 seconds
            'position_size_pct': 3.0,   # Small bets, many trades
            'max_positions': 20,
            'take_profit_pct': 200.0,   # Dynamic exit, not % based
            'stop_loss_pct': 16.0,      # Cut at 16%
            'min_confidence': 0.40,     # Low bar — trade often
            'preferred_strategies': ['cheap_hunter', 'momentum_reversal'],
        },
        15: {
            'name': '15 min',
            'scan_interval': 3,
            'position_size_pct': 3.0,
            'max_positions': 20,
            'take_profit_pct': 200.0,
            'stop_loss_pct': 16.0,
            'min_confidence': 0.40,
            'preferred_strategies': ['cheap_hunter', 'oracle_arb', 'yes_no_arb'],
        },
        30: {
            'name': '30 min',
            'scan_interval': 5,
            'position_size_pct': 3.0,
            'max_positions': 20,
            'take_profit_pct': 200.0,
            'stop_loss_pct': 16.0,
            'min_confidence': 0.40,
            'preferred_strategies': ['cheap_hunter', 'time_decay', 'oracle_arb'],
        },
    }

    # ═══════════════════════════════════════════════════════════════════
    # STRATEGY DEFAULTS
    # ═══════════════════════════════════════════════════════════════════

    # Flash Crash
    FLASH_DROP_THRESHOLD = float(os.getenv('FLASH_DROP_THRESHOLD', '0.25'))
    FLASH_LOOKBACK_SECONDS = int(os.getenv('FLASH_LOOKBACK_SECONDS', '10'))

    # Oracle Arb
    ORACLE_PRICE_BUFFER = float(os.getenv('ORACLE_PRICE_BUFFER', '0.005'))  # 0.5%
    ORACLE_MIN_EDGE = float(os.getenv('ORACLE_MIN_EDGE', '0.10'))  # 10 cents mispricing

    # YES+NO Arb
    ARB_MAX_COMBINED_PRICE = float(os.getenv('ARB_MAX_COMBINED_PRICE', '0.98'))

    # Time Decay
    DECAY_MAX_REMAINING_SECONDS = int(os.getenv('DECAY_MAX_REMAINING_SECONDS', '120'))
    DECAY_MIN_NO_DISCOUNT = float(os.getenv('DECAY_MIN_NO_DISCOUNT', '0.10'))

    # ═══════════════════════════════════════════════════════════════════
    # RISK MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════
    MAX_DAILY_LOSS_PCT = float(os.getenv('MAX_DAILY_LOSS_PCT', '50.0'))  # Aggressive
    MAX_TOTAL_POSITIONS = int(os.getenv('MAX_TOTAL_POSITIONS', '20'))

    # ═══════════════════════════════════════════════════════════════════
    # DATABASE
    # ═══════════════════════════════════════════════════════════════════
    DATABASE_PATH = os.getenv('DATABASE_PATH', 'data/trades.db')

    # ═══════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════
    @classmethod
    def is_paper(cls) -> bool:
        return cls.TRADING_MODE.lower() == 'paper'

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN)

    @classmethod
    def get_timeframe_params(cls, minutes: int) -> dict:
        return cls.TIMEFRAME_PARAMS.get(minutes, cls.TIMEFRAME_PARAMS[15])

    @classmethod
    def print_status(cls):
        mode = '📋 PAPER' if cls.is_paper() else '🔴 LIVE'
        print(f"\n{'='*50}")
        print(f"⚡ 5MIN_TRADE — Polymarket Crypto Scalper")
        print(f"{'='*50}")
        print(f"Mode: {mode}")
        print(f"Coins: {', '.join(cls.ENABLED_COINS)}")
        print(f"Timeframes: {cls.ENABLED_TIMEFRAMES}")
        print(f"Telegram: {'✅' if cls.TELEGRAM_BOT_TOKEN else '❌'}")
        print(f"Wallet: {'✅' if cls.POLY_PRIVATE_KEY else '❌ (paper only)'}")
        print(f"Balance: ${cls.STARTING_BALANCE:.2f}")
        print(f"{'='*50}\n")
