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
    POLY_FUNDER_ADDRESS = os.getenv('POLY_FUNDER_ADDRESS', '')  # Auto-derived if blank
    POLY_API_KEY = os.getenv('POLY_API_KEY', '')      # Auto-derived from private key
    POLY_API_SECRET = os.getenv('POLY_API_SECRET', '')  # Auto-derived from private key
    POLY_PASSPHRASE = os.getenv('POLY_PASSPHRASE', '')  # Auto-derived from private key
    POLY_SIGNATURE_TYPE = int(os.getenv('POLY_SIGNATURE_TYPE', '0'))  # 0=EOA, 1=Magic
    POLY_CHAIN_ID = int(os.getenv('POLY_CHAIN_ID', '137'))  # Polygon mainnet

    # ═══════════════════════════════════════════════════════════════════
    # API ENDPOINTS
    # ═══════════════════════════════════════════════════════════════════
    GAMMA_API_URL = 'https://gamma-api.polymarket.com'
    CLOB_API_URL = 'https://clob.polymarket.com'
    BINANCE_WS_URL = 'wss://stream.binance.com:9443/ws'
    POLYMARKET_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
    POLYMARKET_LIVE_WS_URL = 'wss://ws-live-data.polymarket.com'

    # ═══════════════════════════════════════════════════════════════════
    # PROXY (bypass Polymarket geoblock on Railway)
    # ═══════════════════════════════════════════════════════════════════
    # Set this to a proxy URL in a non-blocked country (IN, JP, BR, KR)
    # Formats: http://host:port  |  socks5://host:port  |  socks5://user:pass@host:port
    PROXY_URL = os.getenv('PROXY_URL', '')

    # ═══════════════════════════════════════════════════════════════════
    # TRADING MODE
    # ═══════════════════════════════════════════════════════════════════
    TRADING_MODE = os.getenv('TRADING_MODE', 'paper')  # 'paper' or 'live'
    LIVE_RISK_MODE = os.getenv('LIVE_RISK_MODE', 'concentration')  # concentration/medium/aggressive
    STARTING_BALANCE = float(os.getenv('STARTING_BALANCE', '100.0'))
    POLYMARKET_MIN_ORDER_SIZE = 1.0  # Polymarket minimum order = $1

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
            'scan_interval': 1,        # FAST: scan every 1 second (500ms delay removed)
            'position_size_pct': 3.0,   # Small bets, many trades
            'max_positions': 20,
            'take_profit_pct': 200.0,   # Dynamic exit, not % based
            'stop_loss_pct': 16.0,      # Cut at 16%
            'min_confidence': 0.40,     # Low bar — trade often
            'preferred_strategies': ['cheap_hunter', 'momentum_reversal', 'prob_closer'],
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
    FLASH_LOOKBACK_SECONDS = int(os.getenv('FLASH_LOOKBACK_SECONDS', '15'))

    # Dynamic taker fees (5m/15m crypto markets)
    TAKER_FEE_RATE = float(os.getenv('TAKER_FEE_RATE', '0.0156'))  # ~1.56% at 50% prob

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
    def is_live_ready(cls) -> bool:
        """Check if minimum live trading config is set (just private key)."""
        pk = cls.POLY_PRIVATE_KEY.strip() if cls.POLY_PRIVATE_KEY else ''
        return bool(pk)

    @classmethod
    def derive_wallet_address(cls) -> str:
        """Derive wallet address from private key. Returns '' on failure."""
        pk = cls.POLY_PRIVATE_KEY.strip() if cls.POLY_PRIVATE_KEY else ''
        if not pk:
            return ''
        try:
            from eth_account import Account
            if not pk.startswith('0x'):
                pk = '0x' + pk
            wallet = Account.from_key(pk)
            return wallet.address
        except Exception:
            return ''

    @classmethod
    def get_funder_address(cls) -> str:
        """Get funder address — uses explicit config or auto-derives from key."""
        if cls.POLY_FUNDER_ADDRESS and cls.POLY_FUNDER_ADDRESS.strip():
            return cls.POLY_FUNDER_ADDRESS.strip()
        # Auto-derive for EOA wallets (signature_type=0)
        if cls.POLY_SIGNATURE_TYPE == 0:
            return cls.derive_wallet_address()
        return ''

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN)

    @classmethod
    def get_timeframe_params(cls, minutes: int) -> dict:
        return cls.TIMEFRAME_PARAMS.get(minutes, cls.TIMEFRAME_PARAMS[15])

    @classmethod
    def print_status(cls):
        mode = '📋 PAPER' if cls.is_paper() else '🔴 LIVE'
        pk_ok = bool(cls.POLY_PRIVATE_KEY and cls.POLY_PRIVATE_KEY.strip())
        wallet = cls.derive_wallet_address() if pk_ok else ''
        funder = cls.get_funder_address()
        api_auto = not bool(cls.POLY_API_KEY and cls.POLY_API_KEY.strip())

        print(f"\n{'='*60}", flush=True)
        print(f"⚡ 5MIN_TRADE — Polymarket Crypto Scalper", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"Mode: {mode}", flush=True)
        print(f"Coins: {', '.join(cls.ENABLED_COINS)}", flush=True)
        print(f"Timeframes: {cls.ENABLED_TIMEFRAMES}", flush=True)
        print(f"Telegram: {'✅' if cls.TELEGRAM_BOT_TOKEN else '❌'}", flush=True)
        print(f"{'─'*60}", flush=True)
        print(f"🔐 LIVE TRADING CONFIG:", flush=True)
        print(f"  Private Key: {'✅ set' if pk_ok else '❌ NOT SET — set POLY_PRIVATE_KEY'}", flush=True)
        if pk_ok:
            print(f"  Wallet: {wallet[:8]}...{wallet[-4:]}" if wallet else "  Wallet: ❌ could not derive", flush=True)
            print(f"  Funder: {funder[:8]}...{funder[-4:]}" if funder else "  Funder: ⚠️ not set (required for proxy wallets)", flush=True)
            print(f"  API Creds: {'🔑 auto-derive from key' if api_auto else '✅ manually set'}", flush=True)
            print(f"  Sig Type: {cls.POLY_SIGNATURE_TYPE} ({'EOA/MetaMask' if cls.POLY_SIGNATURE_TYPE == 0 else 'Email/Magic' if cls.POLY_SIGNATURE_TYPE == 1 else 'Proxy'})", flush=True)
        print(f"Balance: ${cls.STARTING_BALANCE:.2f}", flush=True)
        print(f"{'='*60}\n", flush=True)
