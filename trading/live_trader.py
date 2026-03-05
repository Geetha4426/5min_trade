"""
Live Trader — Real CLOB Order Execution (FOK + GTC)

Uses py-clob-client to place real orders on Polymarket.
- FOK (Fill-or-Kill) for instant $1 trades — no 5-share minimum!
- GTC limit orders for positioned entries at YOUR desired price
- FOK sells for instant exits, GTC sells as fallback
- Auto-cancel unfilled GTC orders after timeout
- Tracks positions from CLOB fill confirmations
- Fee-aware PnL (dynamic taker fees on 5m/15m markets)

IMPORTANT: This trades REAL money. Start with $1-5 max.
"""

import os
import uuid
import time
import math
import asyncio
from typing import Dict, List, Optional
from datetime import datetime

from config import Config
from trading.live_balance_manager import LiveBalanceManager
from data.database import Database
from strategies.base_strategy import TradeSignal


class LiveTrader:
    """
    Real order execution on Polymarket CLOB.
    
    Two entry modes:
    - FOK (high confidence): Instant fill at $1 minimum, no 5-share rule
    - GTC (positioning): Place limit at desired price, wait for fill
    
    Exit mode:
    - FOK sell for instant exit, GTC sell as fallback
    
    Tracks pending orders and auto-cancels stale ones.
    Logs every trade to CSV (data/trades_log.csv) for Telegram download.
    """

    ORDER_TIMEOUT = 60  # Cancel unfilled orders after 60 seconds (thin 5m markets need more time)
    BASE_TAKER_FEE_RATE = 0.03125  # ~3.125% effective taker fee at p=0.50 (peak ~3.7% at p≈0.33)
    TAKER_FEE_RATE = 0.03125  # Updated dynamically per-trade via _get_dynamic_fee_rate

    def __init__(self, db: Database, balance_mgr: LiveBalanceManager):
        self.db = db
        self.balance_mgr = balance_mgr
        self.positions: Dict[str, Dict] = {}
        self.pending_orders: Dict[str, Dict] = {}
        self.trade_history: List[Dict] = []
        self.clob_client = None
        self._initialized = False
        self._consecutive_failures = 0
        self._trading_paused = False
        self._pause_reason = ''
        self._init_error = ''  # Last init error for /debug
        # ── Trade logger (CSV) ──
        from trading.trade_logger import TradeLogger
        self.trade_logger = TradeLogger()
        # Cached real balance (refreshed every 30s to avoid RPC spam)
        self._cached_real_balance: Optional[float] = None
        self._last_balance_check: float = 0.0
        self._sig_type: int = 0  # 0=EOA, 1=Magic, 2=Proxy
        # ── Cooldown: prevent re-entry after stop-loss ──
        # Maps "COIN_DIRECTION" -> timestamp of last stop-loss exit
        self._stop_loss_cooldowns: Dict[str, float] = {}
        self.STOP_LOSS_COOLDOWN_SECS = 60  # Block same coin+direction for 60s after stop
        # ── Signal dedup: suppress repeated identical signals ──
        self._signal_dedup: Dict[str, float] = {}
        self.SIGNAL_DEDUP_SECS = 10  # Suppress same coin+dir+strategy within 10s
        # ── Global post-loss throttle: brief pause after ANY stop-loss ──
        self._last_stop_loss_time: float = 0
        self.POST_LOSS_THROTTLE_SECS = 5  # 5s pause — just prevents 3s revenge trades
        # ── Orderbook reader for smart exits (set by app.py) ──
        self.clob_reader = None

    async def init(self):
        """Initialize CLOB client with credentials.
        
        Based on official py-clob-client docs:
        - signature_type=0: EOA/MetaMask (default)
        - signature_type=1: Email/Magic wallet
        - signature_type=2: Browser proxy wallet
        - funder: address holding funds (required for proxy wallets, optional for EOA)
        
        Only POLY_PRIVATE_KEY is required. API credentials (key/secret/passphrase)
        are auto-derived from the private key via create_or_derive_api_creds().
        Funder address is auto-derived from private key for EOA wallets.
        """
        private_key = Config.POLY_PRIVATE_KEY
        if not private_key:
            print("⚠️ No POLY_PRIVATE_KEY set — live trading disabled", flush=True)
            print("  To enable live trading, set POLY_PRIVATE_KEY in Railway env vars", flush=True)
            print("  Format: 0x followed by 64 hex characters (from MetaMask)", flush=True)
            self._init_error = 'POLY_PRIVATE_KEY is empty or not set'
            return False

        # Validate key format
        private_key = private_key.strip()
        if not private_key.startswith('0x'):
            private_key = '0x' + private_key
        if len(private_key) != 66:  # 0x + 64 hex chars
            print(f"❌ POLY_PRIVATE_KEY looks wrong (length={len(private_key)}, expected 66)", flush=True)
            print(f"  Format should be: 0x followed by 64 hex characters", flush=True)
            self._init_error = f'POLY_PRIVATE_KEY wrong length: {len(private_key)} (expected 66)'
            return False

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            print(f"🔑 Initializing CLOB client...", flush=True)

            # Use relay URL if configured (bypasses geo-blocking)
            host = Config.get_clob_url()
            if Config.is_relay_enabled():
                print(f"  🔀 Using CLOB relay: {host}", flush=True)
            chain_id = Config.POLY_CHAIN_ID  # 137 = Polygon
            sig_type = Config.POLY_SIGNATURE_TYPE  # 0=EOA, 1=Magic, 2=Proxy

            # Auto-derive funder address if not explicitly set
            funder = Config.get_funder_address()
            if not funder:
                funder = None

            # ── Validate proxy wallet config ──
            if sig_type == 2 and not funder:
                print(f"\n{'='*60}", flush=True)
                print(f"❌ PROXY WALLET MODE (sig_type=2) requires POLY_PROXY_WALLET!", flush=True)
                print(f"  Your Polymarket account uses a browser proxy wallet.", flush=True)
                print(f"  The proxy wallet is the 'maker' address from the order signing popup.", flush=True)
                print(f"  Set in Railway:", flush=True)
                print(f"    POLY_SIGNATURE_TYPE=2", flush=True)
                print(f"    POLY_PROXY_WALLET=0xYourMakerAddress", flush=True)
                print(f"{'='*60}\n", flush=True)
                self._init_error = 'POLY_PROXY_WALLET not set (required for sig_type=2)'
                return False

            print(f"  Host: {host}", flush=True)
            print(f"  Chain: {chain_id}", flush=True)
            print(f"  Sig type: {sig_type} ({'EOA' if sig_type == 0 else 'Magic' if sig_type == 1 else 'Proxy'})", flush=True)
            if funder:
                print(f"  Funder: {funder[:8]}...{funder[-4:]}", flush=True)
            else:
                print(f"  Funder: (none — EOA mode, using signing address)", flush=True)

            # Step 1: Create client
            self._sig_type = sig_type  # Store for balance/allowance queries
            self.clob_client = ClobClient(
                host,
                key=private_key,
                chain_id=chain_id,
                signature_type=sig_type,
                funder=funder,
            )

            # Inject relay auth token if using relay
            if Config.is_relay_enabled() and Config.CLOB_RELAY_AUTH_TOKEN:
                self._patch_relay_auth()

            # Step 2: Set or derive API credentials
            # NOTE: POLY_API_KEY/SECRET/PASSPHRASE are auto-derived from your private key.
            # You do NOT need to set them manually. Leave them blank in Railway.
            if Config.POLY_API_KEY and Config.POLY_API_KEY.strip():
                creds = ApiCreds(
                    api_key=Config.POLY_API_KEY.strip(),
                    api_secret=Config.POLY_API_SECRET.strip(),
                    api_passphrase=Config.POLY_PASSPHRASE.strip(),
                )
                self.clob_client.set_api_creds(creds)
                print(f"🔑 Using manually provided API credentials", flush=True)
            else:
                print(f"🔑 Auto-deriving API credentials from private key...", flush=True)
                try:
                    derived = self.clob_client.create_or_derive_api_creds()
                    self.clob_client.set_api_creds(derived)
                    print(f"✅ API credentials derived successfully", flush=True)
                    print(f"  ℹ️  POLY_API_KEY/SECRET/PASSPHRASE are NOT needed — they are auto-derived!", flush=True)
                except Exception as e:
                    print(f"❌ Failed to derive API creds: {e}", flush=True)
                    print(f"  This usually means the private key is invalid or the CLOB API is down.", flush=True)
                    print(f"  If this persists, try setting POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE manually.", flush=True)
                    self._init_error = f'create_or_derive_api_creds() failed: {e}'
                    return False

            # Step 3: Test connection
            try:
                ok = self.clob_client.get_ok()
                via = " via relay" if Config.is_relay_enabled() else ""
                print(f"🟢 CLOB connection{via}: {ok}", flush=True)
            except Exception as e:
                print(f"⚠️ CLOB connection test failed: {e}", flush=True)
                err_str = str(e).lower()
                if 'forbidden' in err_str or '403' in err_str or 'geo' in err_str:
                    if not Config.is_relay_enabled():
                        print(f"💡 TIP: Set CLOB_RELAY_URL to bypass geo-blocking.", flush=True)
                        print(f"   Deploy relay_server.py on a VPS in an allowed region.", flush=True)
                # Non-fatal — might still work

            # Step 4: Log fee formula info
            # Fee is calculated per-trade via _get_dynamic_fee_rate(price).
            # Formula: rate = 0.25 × p × (1-p)²  (from Polymarket docs)
            # Don't fetch from /fees endpoint — it returns per-token bps that
            # the CLOB handles internally.  Our formula matches the official docs.
            print(f"💰 Fee formula: 0.25×p×(1-p)² | At p=0.50: 3.13% | "
                  f"At p=0.78 (entry cap): 0.94%", flush=True)

            # Step 5: Check actual balance
            real_balance = await self.fetch_balance()
            if real_balance is not None:
                self._cached_real_balance = real_balance
                self._last_balance_check = time.time()
                if real_balance < 0.50:
                    print(f"\n{'='*60}", flush=True)
                    print(f"⚠️  WARNING: Polymarket balance is ${real_balance:.2f}", flush=True)
                    print(f"  You need to:", flush=True)
                    print(f"  1. Deposit USDC at https://polymarket.com", flush=True)
                    print(f"  2. Set token allowances (first-time MetaMask users)", flush=True)
                    print(f"     Run: python -c \"from trading.live_trader import LiveTrader; ...\"", flush=True)
                    print(f"{'='*60}\n", flush=True)
                    self._trading_paused = True
                    self._pause_reason = f'Low balance (${real_balance:.2f})'
                else:
                    print(f"💰 Polymarket balance: ${real_balance:.2f}", flush=True)
                    self.balance_mgr.update_balance(real_balance)

            # Step 6: Check USDC allowance
            await self._check_allowance()

            # Step 6b: Check on-chain CTF approval for sell orders
            await self._check_ctf_approval()

            # Step 7: Startup cleanup — cancel all stale orders from previous session
            # (Inspired by PolyFlup: prevents ghost orders from restarted bots)
            await self._startup_cleanup()

            self._initialized = True
            print(f"✅ Live trader initialized successfully", flush=True)
            return True

        except ImportError as e:
            print(f"❌ py-clob-client not installed: {e}", flush=True)
            print(f"  Run: pip install py-clob-client>=0.18.0", flush=True)
            self._init_error = f'ImportError: {e}'
            return False
        except Exception as e:
            print(f"❌ CLOB init error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            self._init_error = f'CLOB init error: {e}'
            return False

    @property
    def is_ready(self) -> bool:
        return self._initialized and self.clob_client is not None

    def _patch_relay_auth(self):
        """Inject relay auth token into ClobClient's HTTP session.
        
        Orders are signed locally — the relay only forwards the signed request.
        The auth token prevents unauthorized use of your relay endpoint.
        """
        try:
            session = getattr(self.clob_client, 'session', None)
            if session is None:
                http = getattr(self.clob_client, 'http', None)
                if http:
                    session = getattr(http, 'session', None)

            if session and hasattr(session, 'headers'):
                session.headers['Authorization'] = f'Bearer {Config.CLOB_RELAY_AUTH_TOKEN}'
                print(f"  🔑 Relay auth token injected", flush=True)
            else:
                print(f"  ⚠️ Could not inject relay auth token (session not found)", flush=True)
        except Exception as e:
            print(f"  ⚠️ Relay auth injection failed: {e}", flush=True)

    async def fetch_balance(self) -> float:
        """
        Fetch real USDC balance.

        Based on official Polymarket/agents repo (github.com/Polymarket/agents):
        They use on-chain USDC.e balanceOf(wallet_address) — NOT a CLOB endpoint.

        Methods (in order):
        1. On-chain USDC.e balanceOf (official Polymarket method)
        2. CLOB update_balance_allowance → get_balance_allowance
        3. Data API: GET /value?user={address}
        4. STARTING_BALANCE config fallback
        """
        if not self.is_ready:
            return None

        # ═══ Method 1: On-chain USDC.e balanceOf (OFFICIAL Polymarket method) ═══
        # Source: github.com/Polymarket/agents/blob/main/agents/polymarket/polymarket.py
        # self.usdc.functions.balanceOf(address).call() / 10e5
        try:
            import requests
            from config import Config
            from eth_account import Account

            wallet = Account.from_key(Config.POLY_PRIVATE_KEY)
            wallet_address = wallet.address

            # Addresses to check (wallet + proxy/funder/safe)
            addresses_to_check = [(wallet_address, "wallet")]

            # Polymarket proxy wallet — this is where your actual funds live
            proxy_wallet = os.environ.get("POLY_PROXY_WALLET", "").strip()
            if proxy_wallet and proxy_wallet.lower() != wallet_address.lower():
                addresses_to_check.insert(0, (proxy_wallet, "proxy"))  # Check proxy FIRST

            # Also check funder/safe if configured
            try:
                funder = Config.get_funder_address()
                if funder and funder.lower() not in [a.lower() for a, _ in addresses_to_check]:
                    addresses_to_check.append((funder, "funder"))
            except Exception:
                pass
            try:
                if Config.POLY_SAFE_ADDRESS and Config.POLY_SAFE_ADDRESS.lower() not in [a.lower() for a, _ in addresses_to_check]:
                    addresses_to_check.append((Config.POLY_SAFE_ADDRESS, "safe"))
            except Exception:
                pass

            addr_list = ", ".join(f"{label}={addr[:10]}..." for addr, label in addresses_to_check)
            print(f"🔍 Checking on-chain USDC: {addr_list}", flush=True)

            # USDC contracts on Polygon
            usdc_contracts = [
                ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e"),   # Official Polymarket
                ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC"),     # Native USDC
            ]

            # Free public Polygon RPC endpoints (2026 — tested working)
            rpc_endpoints = [
                "https://polygon-bor-rpc.publicnode.com",      # PublicNode/Allnodes
                "https://1rpc.io/matic",                        # 1RPC by Automata
                "https://polygon.drpc.org",                     # dRPC
                "https://polygon.meowrpc.com",                  # MeowRPC
                "https://polygon-mainnet.gateway.tatum.io",     # Tatum
            ]

            # Bypass any HTTPS_PROXY / HTTP_PROXY (Railway's proxy breaks RPC calls)
            no_proxy = {"http": "", "https": ""}

            total_balance = 0.0
            for addr, addr_label in addresses_to_check:
                padded_addr = addr[2:].lower().zfill(64)
                for contract, token_label in usdc_contracts:
                    balance_found = False
                    for rpc_url in rpc_endpoints:
                        if balance_found:
                            break
                        try:
                            call_data = f"0x70a08231{padded_addr}"
                            resp = requests.post(
                                rpc_url,
                                headers={"Content-Type": "application/json"},
                                json={
                                    "jsonrpc": "2.0",
                                    "method": "eth_call",
                                    "params": [{"to": contract, "data": call_data}, "latest"],
                                    "id": 1,
                                },
                                timeout=10,
                                proxies=no_proxy,
                            )
                            if resp.status_code == 200:
                                rpc_data = resp.json()
                                if "error" in rpc_data:
                                    continue  # Try next RPC
                                result = rpc_data.get("result", "0x0")
                                balance_wei = int(result, 16)
                                balance = balance_wei / 1e6  # USDC has 6 decimals
                                print(f"  📊 {addr_label} [{token_label}]: ${balance:.6f}", flush=True)
                                balance_found = True
                                if balance > 0:
                                    total_balance += balance
                        except Exception:
                            continue
                    if not balance_found:
                        print(f"  ❌ {addr_label} [{token_label}]: all RPCs failed", flush=True)

            if total_balance > 0:
                print(f"💰 Total on-chain USDC: ${total_balance:.2f}", flush=True)
                return round(total_balance, 2)
            else:
                print(f"⚠️ No USDC found on any address/contract", flush=True)

        except Exception as e:
            print(f"⚠️ On-chain balance failed: {e}", flush=True)

        # ═══ Method 2: CLOB update + read ═══
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self._sig_type,
            )
            try:
                self.clob_client.update_balance_allowance(params)
            except Exception:
                pass
            bal_resp = self.clob_client.get_balance_allowance(params)
            if bal_resp:
                balance = float(bal_resp.get('balance', 0))
                if balance > 1_000_000:
                    balance = balance / 1e6
                print(f"💰 CLOB balance: ${balance:.2f}", flush=True)
                if balance > 0:
                    return round(balance, 2)
        except Exception as e:
            print(f"⚠️ CLOB balance failed: {e}", flush=True)

        # ═══ Method 3: Data API portfolio value ═══
        try:
            import requests
            from eth_account import Account
            from config import Config

            # Use proxy wallet (where funds live) over signer address
            proxy = os.environ.get("POLY_PROXY_WALLET", "").strip()
            if not proxy:
                wallet = Account.from_key(Config.POLY_PRIVATE_KEY)
                proxy = wallet.address
            resp = requests.get(
                "https://data-api.polymarket.com/value",
                params={"user": proxy.lower()},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, (int, float)):
                    value = float(data)
                elif isinstance(data, dict):
                    value = float(data.get('value', data.get('balance', 0)))
                elif isinstance(data, str):
                    value = float(data)
                else:
                    value = 0.0
                if value > 0:
                    print(f"💰 Data API value: ${value:.2f}", flush=True)
                    return round(value, 2)
        except Exception as e:
            print(f"⚠️ Data API failed: {e}", flush=True)

        # Method 4: Fallback to STARTING_BALANCE from config
        # This ensures the bot can trade even if balance fetching is broken
        try:
            from config import Config
            fallback = Config.STARTING_BALANCE
            if fallback > 0:
                print(f"⚠️ Using fallback balance from STARTING_BALANCE: ${fallback:.2f}", flush=True)
                return fallback
        except Exception:
            pass

        return None

    async def _get_cached_balance(self) -> Optional[float]:
        """Get real USDC balance, refreshing at most every 30 seconds.
        Preserves last-known-good balance if refresh fails."""
        now = time.time()
        if now - self._last_balance_check > 30:
            real = await self.fetch_balance()
            if real is not None and real > 0:
                self._cached_real_balance = real
                self.balance_mgr.update_balance(real)
            # If fetch failed but we have a cached balance, keep using it
            self._last_balance_check = now
        return self._cached_real_balance

    async def _check_allowance(self):
        """Check if USDC allowance is sufficient for trading."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self._sig_type,
            )
            bal_resp = self.clob_client.get_balance_allowance(params)
            if bal_resp:
                allowance_raw = bal_resp.get('allowance', None)
                if allowance_raw is not None:
                    allowance = float(allowance_raw)
                    if allowance > 1_000_000:
                        allowance = allowance / 1e6
                    if allowance < 1.0:
                        print(f"⚠️ USDC allowance too low (${allowance:.2f}) — need to approve CLOB contract", flush=True)
                        print(f"  Visit https://polymarket.com and place a manual trade first to set allowance", flush=True)
                    else:
                        print(f"✅ USDC allowance: ${allowance:.2f}", flush=True)
        except Exception as e:
            print(f"⚠️ Allowance check failed (non-fatal): {e}", flush=True)

    async def _check_ctf_approval(self):
        """Check on-chain setApprovalForAll for both exchange contracts.
        
        Sell orders REQUIRE the CTF conditional token contract to be approved
        for the exchange that is executing the trade.  There are two exchanges:
          - Normal:   0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
          - NegRisk:  0xC5d563A36AE78145C45a50134d48A1215220f80a
        
        Crypto markets (BTC/ETH/SOL) use neg_risk.  If the proxy wallet hasn't
        approved the exchange, EVERY sell order will fail with
        'not enough balance / allowance'.
        """
        try:
            from config import Config
            rpc_url = getattr(Config, 'POLYGON_RPC_URL', '')
            rpcs = getattr(Config, 'POLYGON_RPC_URLS', rpc_url)
            if not rpcs:
                rpcs = 'https://polygon-bor-rpc.publicnode.com'
            rpc_list = [r.strip() for r in rpcs.split(',') if r.strip()] if isinstance(rpcs, str) else rpcs
            if not rpc_list:
                return

            funder = Config.get_funder_address()
            if not funder:
                # EOA wallet — the signing address IS the funder
                from eth_account import Account
                acct = Account.from_key(Config.POLY_PRIVATE_KEY)
                funder = acct.address

            import requests
            CTF_CONTRACT = '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045'
            EXCHANGES = {
                'Normal':  '0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E',
                'NegRisk': '0xC5d563A36AE78145C45a50134d48A1215220f80a',
            }
            # isApprovedForAll(address owner, address operator) → bool
            # selector = 0xe985e9c5
            for label, exchange in EXCHANGES.items():
                try:
                    owner_padded = funder.lower().replace('0x', '').zfill(64)
                    operator_padded = exchange.lower().replace('0x', '').zfill(64)
                    data = f'0xe985e9c5{owner_padded}{operator_padded}'
                    payload = {
                        'jsonrpc': '2.0', 'method': 'eth_call', 'id': 1,
                        'params': [{'to': CTF_CONTRACT, 'data': data}, 'latest']
                    }
                    resp = requests.post(rpc_list[0], json=payload, timeout=10)
                    if resp.status_code == 200:
                        result = resp.json().get('result', '0x0')
                        approved = int(result, 16) == 1 if result else False
                        if approved:
                            print(f"✅ CTF approval ({label} exchange): approved", flush=True)
                        else:
                            print(f"\n{'='*60}", flush=True)
                            print(f"❌ CTF approval ({label} exchange): NOT APPROVED!", flush=True)
                            print(f"   Sells on {label} markets will fail!", flush=True)
                            print(f"   Fix: go to polymarket.com → sell ANY position → this sets approval", flush=True)
                            print(f"   Owner: {funder}", flush=True)
                            print(f"   Exchange: {exchange}", flush=True)
                            print(f"{'='*60}\n", flush=True)
                except Exception as ex:
                    print(f"⚠️ CTF approval check ({label}) failed: {ex}", flush=True)
        except Exception as e:
            print(f"⚠️ CTF approval check skipped: {e}", flush=True)

    async def _startup_cleanup(self):
        """Cancel all open orders from previous sessions on startup.
        
        Prevents ghost orders that the bot lost track of after a restart.
        Inspired by PolyFlup's startup position sync pattern.
        """
        try:
            # Get all open orders from the CLOB
            open_orders = self.clob_client.get_orders()
            if not open_orders:
                print(f"🧹 Startup cleanup: no stale orders found", flush=True)
                return

            # Filter for actually open orders
            if isinstance(open_orders, list):
                live_orders = [o for o in open_orders
                              if o.get('status', '').lower() in ('live', 'open')]
            else:
                live_orders = []

            if live_orders:
                self.clob_client.cancel_all()
                print(f"🧹 Startup cleanup: cancelled {len(live_orders)} stale orders", flush=True)
            else:
                print(f"🧹 Startup cleanup: no stale orders found", flush=True)
        except Exception as e:
            # Non-fatal — just log and continue
            print(f"⚠️ Startup cleanup failed (non-fatal): {e}", flush=True)

    async def execute_signal(self, signal: TradeSignal) -> Optional[Dict]:
        """Execute a trade signal — FOK for instant fills, GTC for positioning.
        
        High confidence (≥0.80): FOK instant fill at $1 (no 5-share minimum)
        Medium confidence: GTC limit at desired price, wait for fill
        """
        if not self.is_ready:
            print("⚠️ LiveTrader not initialized", flush=True)
            return None

        # Check if trading is paused due to repeated failures
        # Auto-unpause after 5 minutes to retry (network/allowance issues are transient)
        if self._trading_paused:
            if not hasattr(self, '_paused_at'):
                self._paused_at = time.time()
            elapsed = time.time() - self._paused_at
            if elapsed >= 300:  # 5 minutes
                print(f"🔄 Auto-resuming trading after {elapsed/60:.0f}min pause", flush=True)
                self._trading_paused = False
                self._consecutive_failures = 0
                self._pause_reason = ''
            else:
                # Log periodically so user knows WHY bot isn't trading
                now = time.time()
                if not hasattr(self, '_last_pause_log') or now - self._last_pause_log > 60:
                    self._last_pause_log = now
                    remaining = 300 - elapsed
                    print(f"⏸️ Trading paused ({self._pause_reason}). "
                          f"Auto-resume in {remaining:.0f}s", flush=True)
                return None

        is_arb = signal.direction == 'BOTH' and '|' in signal.token_id
        can_trade_ok, reason = self.balance_mgr.can_trade(is_arb=is_arb)
        if not can_trade_ok:
            # Log WHY we can't trade (but not every single signal — throttle)
            now = time.time()
            if not hasattr(self, '_last_cant_trade_log') or now - self._last_cant_trade_log > 30:
                self._last_cant_trade_log = now
                print(f"⛔ Can't trade: {reason} | Balance: ${self.balance_mgr.balance:.2f} | "
                      f"Signal: {signal.coin} {signal.direction} @ {signal.entry_price:.3f}", flush=True)
            return None

        # ── Cooldown: block re-entry on same coin+direction after stop-loss ──
        # Only in SEED/CONCENTRATION to protect capital. MEDIUM/AGGRESSIVE trade freely.
        mode = self.balance_mgr.mode_name
        if mode in ('seed', 'concentration'):
            cooldown_key = f"{signal.coin}_{signal.direction}"
            last_stop = self._stop_loss_cooldowns.get(cooldown_key, 0)
            cooldown_secs = self.STOP_LOSS_COOLDOWN_SECS
            if time.time() - last_stop < cooldown_secs:
                remaining = cooldown_secs - (time.time() - last_stop)
                now = time.time()
                if not hasattr(self, '_last_cooldown_log') or now - self._last_cooldown_log > 10:
                    self._last_cooldown_log = now
                    print(f"🧊 Cooldown: {signal.coin} {signal.direction} blocked "
                          f"({remaining:.0f}s after stop-loss)", flush=True)
                return None

        # ── Conflict check: don't enter same coin in opposite direction ──
        # Prevents: buying SOL UP while holding SOL DOWN (different timeframes)
        # Only in SEED/CONCENTRATION. MEDIUM/AGGRESSIVE can multi-position same coin.
        # EXCEPTION: BOTH-side arb signals are always allowed (they hedge).
        if mode in ('seed', 'concentration') and signal.direction != 'BOTH':
            for pos in self.positions.values():
                if pos.get('coin') == signal.coin and pos.get('direction') != signal.direction:
                    return None  # Opposite direction conflict

        # ── Global post-loss throttle: brief pause after ANY stop-loss ──
        # Prevents revenge trading across all coins after a loss.
        # SEED/CONCENTRATION only — higher modes trade freely.
        if mode in ('seed', 'concentration') and self._last_stop_loss_time > 0:
            since_last = time.time() - self._last_stop_loss_time
            if since_last < self.POST_LOSS_THROTTLE_SECS:
                return None

        # ── Signal dedup: suppress repeated signals for same coin+direction+strategy ──
        # Problem: oracle_arb fired 14 BTC UP signals in 60 seconds, all identical.
        # Different from cooldown (which is per stop-loss). This blocks signal spam.
        dedup_key = f"{signal.coin}_{signal.direction}_{signal.strategy}"
        last_fired = self._signal_dedup.get(dedup_key, 0)
        dedup_window = self.SIGNAL_DEDUP_SECS if mode in ('seed', 'concentration') else 3
        if time.time() - last_fired < dedup_window:
            return None
        self._signal_dedup[dedup_key] = time.time()

        # ── Spread guard: reject entries with wide bid-ask spreads ──
        # Wide spread = instant paper loss that often exceeds the potential gain.
        # In thin 5-minute markets, 5%+ spreads are common and deadly.
        spread_pct = (signal.metadata or {}).get('spread_pct', 0)
        max_spread = 6.0 if mode in ('seed', 'concentration') else 10.0
        if spread_pct > max_spread:
            now_t = time.time()
            if not hasattr(self, '_last_spread_log') or now_t - self._last_spread_log > 15:
                self._last_spread_log = now_t
                print(f"⚠️ Skip: {signal.coin} spread {spread_pct:.1f}% > {max_spread:.0f}%", flush=True)
            return None

        size = self.balance_mgr.get_position_size(signal.confidence)
        if size < Config.POLYMARKET_MIN_ORDER_SIZE:
            print(f"⚠️ Skip: position size ${size:.2f} < minimum ${Config.POLYMARKET_MIN_ORDER_SIZE:.2f} | "
                  f"Signal: {signal.coin} {signal.direction}", flush=True)
            return None

        # For BOTH-side strategies (arb, straddle), execute both legs
        if signal.direction == 'BOTH' and '|' in signal.token_id:
            return await self._execute_both_sides(signal, size)

        # Decide: FOK (instant) or GTC (positioned)
        use_fok = getattr(Config, 'USE_FOK_ORDERS', True) and signal.confidence >= 0.80
        return await self._place_buy(signal, size, use_fok=use_fok)

    async def _execute_both_sides(self, signal: TradeSignal, total_size: float) -> Optional[Dict]:
        """Execute a dual-leg trade (arb, straddle).
        
        Uses per-leg prices from strategy metadata when available.
        Falls back to splitting entry_price for simple straddles.
        Pre-validates that real balance can cover BOTH legs before placing either.
        """
        tokens = signal.token_id.split('|')
        if len(tokens) != 2:
            print(f"❌ BOTH-side: expected 2 tokens, got {len(tokens)}", flush=True)
            return None

        meta = signal.metadata or {}
        half_size = max(Config.POLYMARKET_MIN_ORDER_SIZE, total_size / 2)

        # ── PRE-VALIDATION: Ensure real balance can cover BOTH legs ──
        # Calculate ACTUAL cost per leg (5-share min on GTC inflates cost!)
        import math as _math
        real_bal = await self._get_cached_balance()
        use_fok_legs = getattr(Config, 'USE_FOK_ORDERS', True)
        leg_costs = []
        for _p in [signal.entry_price / 2, signal.entry_price / 2]:  # estimate
            tick_p = max(0.01, min(0.99, round(_p * 100) / 100))
            if use_fok_legs:
                _shares = max(1, math.floor(half_size / tick_p * 100) / 100)
            else:
                _shares = max(5, math.floor(half_size / tick_p * 100) / 100)
            leg_costs.append(round(tick_p * _shares, 2))
        needed = sum(leg_costs)

        if real_bal is not None and needed > real_bal:
            print(f"⚠️ Skip dual-leg: need ${needed:.2f} (legs ${leg_costs[0]:.2f}+${leg_costs[1]:.2f}) "
                  f"but only ${real_bal:.2f} available", flush=True)
            return None

        if not self.balance_mgr.can_afford_dual_leg():
            print(f"⚠️ Skip dual-leg: insufficient tradeable balance for 2× ${Config.POLYMARKET_MIN_ORDER_SIZE:.0f} legs", flush=True)
            return None

        # Extract per-leg prices from strategy metadata
        if meta.get('type') == 'cross_timeframe_arb':
            # Cross-TF arb: metadata has primary_price and hedge_price
            prices = [meta.get('primary_price', signal.entry_price / 2),
                      meta.get('hedge_price', signal.entry_price / 2)]
            sides = [meta.get('primary_side', 'UP'), meta.get('hedge_side', 'DOWN')]
            market_ids = signal.market_id.split('|') if '|' in signal.market_id else [signal.market_id, signal.market_id]
        elif meta.get('type') == 'both_sides':
            # Cheap outcome hunter: up_ask and down_ask
            prices = [meta.get('up_ask', signal.entry_price / 2),
                      meta.get('down_ask', signal.entry_price / 2)]
            sides = ['UP', 'DOWN']
            market_ids = [signal.market_id, signal.market_id]
        else:
            # Yes/No arb or generic BOTH: up_ask and down_ask
            prices = [meta.get('up_ask', signal.entry_price / 2),
                      meta.get('down_ask', signal.entry_price / 2)]
            sides = ['UP', 'DOWN']
            market_ids = [signal.market_id, signal.market_id]

        results = []
        for i, (tid, price, side, mid) in enumerate(zip(tokens, prices, sides, market_ids)):
            sub_signal = TradeSignal(
                strategy=signal.strategy,
                coin=signal.coin,
                timeframe=signal.timeframe,
                direction=side,
                token_id=tid,
                market_id=mid,
                entry_price=price,
                confidence=signal.confidence,
                rationale=f"[Leg {i+1}/2] {signal.rationale}",
                metadata={**meta, 'is_dual_leg': True, 'leg_number': i+1},
            )
            # Use FOK for arb legs when enabled (bypasses 5-share min)
            use_fok_legs = getattr(Config, 'USE_FOK_ORDERS', True) and signal.confidence >= 0.80
            result = await self._place_buy(sub_signal, half_size, use_fok=use_fok_legs)
            if result:
                results.append(result)
            else:
                # If first leg succeeds but second fails, cancel the first
                if results:
                    first = results[0]
                    try:
                        self.clob_client.cancel(first.get('order_id', ''))
                        print(f"⚠️ Leg 2 failed, cancelled leg 1: {first['order_id']}", flush=True)
                    except Exception as cancel_err:
                        # FOK orders fill instantly — can't cancel, need to track as orphan
                        print(f"⚠️ Leg 2 failed, couldn't cancel leg 1 (FOK filled?): {cancel_err}", flush=True)
                    # ALWAYS refund balance — even if cancel fails (position tracked separately)
                    self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
                    self.balance_mgr.update_balance(self.balance_mgr.balance + first['size_usd'])
                    return None

        if results:
            print(f"✅ Both legs placed for {signal.strategy}: {signal.coin}", flush=True)
        return results[0] if results else None

    def _get_dynamic_fee_rate(self, price: float) -> float:
        """Calculate per-trade EFFECTIVE fee rate based on share price.
        
        Polymarket's taker fee on 5min/15min crypto markets:
            fee_amount = C × 0.25 × [p × (1-p)]²
        where C = number of shares, p = price (probability 0-1).
        
        Effective fee rate = fee_amount / trade_value (C × p):
            rate = 0.25 × p × (1-p)²
        
        Verified against:
            - docs.polymarket.com/trading/fees
            - Polymarket ctf-exchange/Fees.sol contracts
            - X dev posts: @TVS_Kolia, @DextersSolab, @0xPhilanthrop
        
        Rate at key prices:
            p=0.33: ~3.70% (peak)  |  p=0.50: 3.125%
            p=0.65: 1.99%          |  p=0.78: 0.94%
            p=0.10: 2.03%          |  p=0.90: 0.23%
        
        Key insight: settlement is FREE. Round-trip (buy+sell) costs
        double the rate. Buy+hold-to-settlement costs the rate ONCE.
        """
        p = max(0.001, min(0.999, price))
        q = 1.0 - p
        # Effective rate = 0.25 × p × (1-p)²
        # Peaks at p = 1/3 ≈ 3.70%, NOT at p = 0.50 (3.125%)
        return 0.25 * p * q * q

    async def _place_buy(self, signal: TradeSignal, size: float, use_fok: bool = False) -> Optional[Dict]:
        """Place a buy order on the CLOB.
        
        Two modes:
        - FOK (use_fok=True): Instant fill, $1 minimum, NO 5-share minimum
        - GTC (use_fok=False): Limit order at desired price, 5-share minimum
        """
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            # Tick-align price to 0.01 increments (Polymarket requirement)
            price = max(0.01, min(0.99, round(signal.entry_price * 100) / 100))

            # Calculate per-trade dynamic fee based on probability
            trade_fee = self._get_dynamic_fee_rate(price)
            # Don't mutate self.TAKER_FEE_RATE — fee is stored per-position
            # in trade['fee_rate'] so concurrent positions use their own rate

            # ── Check minimum edge after fees ──
            min_edge = getattr(Config, 'MIN_EDGE_AFTER_FEES', 0.03)
            if signal.metadata and signal.metadata.get('expected_edge', 1.0) < min_edge + trade_fee * 2:
                print(f"⚠️ Skip: edge too small after fees ({trade_fee*200:.1f}%)", flush=True)
                return None

            if use_fok:
                # ═══ FOK MODE: No 5-share minimum! $1 trades possible ═══
                shares = math.floor(size / price * 100) / 100  # floor to 2dp
                if shares < 1:
                    shares = 1  # Minimum 1 share

                # GCD-align shares so price×shares has ≤ 2 decimal places
                # (MUST match _submit_order's adjustment to keep position accounting correct)
                P = int(round(price * 100))
                S = int(math.floor(shares * 100))
                if P > 0 and S > 0:
                    step = 100 // math.gcd(P, 100)
                    S = (S // step) * step
                    # If GCD alignment dropped amount below $1 minimum, round UP
                    if P * S < 10000 and step > 0:  # price×shares < $1
                        S = ((S // step) + 1) * step
                    shares = S / 100.0

                order_amount = round(price * shares, 2)
                if order_amount < Config.POLYMARKET_MIN_ORDER_SIZE:
                    # Still below $1 — compute minimum valid shares
                    min_S = math.ceil(Config.POLYMARKET_MIN_ORDER_SIZE / price * 100)
                    if P > 0:
                        gcd_step = 100 // math.gcd(P, 100)
                        min_S = ((min_S + gcd_step - 1) // gcd_step) * gcd_step
                    shares = min_S / 100.0
                    order_amount = round(price * shares, 2)
                order_type = OrderType.FOK
                order_type_tag = 'FOK'
            else:
                # ═══ GTC MODE: 5-share minimum, queue in orderbook ═══
                MIN_SHARES = 5  # Polymarket CLOB minimum for GTC
                raw_shares = math.floor(size / price * 100) / 100  # floor to 2dp
                shares = max(MIN_SHARES, raw_shares)

                # GCD-align shares (same logic as _submit_order)
                P = int(round(price * 100))
                S = int(math.floor(shares * 100))
                if P > 0 and S > 0:
                    step = 100 // math.gcd(P, 100)
                    S = (S // step) * step
                    if S < MIN_SHARES * 100:
                        S = ((MIN_SHARES * 100 + step - 1) // step) * step
                    shares = S / 100.0

                order_amount = round(price * shares, 2)
                if order_amount < Config.POLYMARKET_MIN_ORDER_SIZE:
                    shares = math.ceil(Config.POLYMARKET_MIN_ORDER_SIZE / price)
                    shares = max(MIN_SHARES, shares)
                    order_amount = round(price * shares, 2)
                order_type = OrderType.GTC
                order_type_tag = 'GTC'

            actual_cost = order_amount

            # ── Check real balance BEFORE placing order ──
            real_bal = await self._get_cached_balance()
            if real_bal is not None and actual_cost > real_bal:
                print(f"⚠️ Skip: order ${actual_cost:.2f} > real balance ${real_bal:.2f}", flush=True)
                return None

            trade_id = str(uuid.uuid4())[:8]
            now = datetime.now().isoformat()

            print(f">> [{order_type_tag}] {signal.coin} {signal.direction} | "
                  f"${actual_cost:.2f} @ ${price:.3f} ({shares:.1f} shares) "
                  f"[fee~{trade_fee*100:.2f}%]", flush=True)

            resp = await self._submit_order(signal.token_id, price, shares, BUY, order_type)

            if not resp or resp.get('status') == 'error':
                error_msg = resp.get('errorMsg', 'Unknown error') if resp else 'No response'
                print(f"❌ Order rejected: {error_msg}", flush=True)

                # FOK rejection is normal (no liquidity) — try GTC fallback
                # BUT: GTC has 5-share minimum, which may exceed our balance
                if use_fok and error_msg and 'not fill' in error_msg.lower():
                    # Pre-check: can we afford 5 shares at this price for GTC?
                    gtc_cost = round(price * 5, 2)
                    real_bal = await self._get_cached_balance()
                    if real_bal is not None and gtc_cost > real_bal:
                        print(f"⚠️ FOK didn't fill. GTC needs ${gtc_cost:.2f} (5×${price:.2f}) "
                              f"but only ${real_bal:.2f} available — skipping", flush=True)
                        return None
                    print(f"🔄 FOK didn't fill — falling back to GTC", flush=True)
                    return await self._place_buy(signal, size, use_fok=False)
                return None

            order_id = resp.get('orderID', resp.get('id', trade_id))
            print(f"✅ [{order_type_tag}] ORDER {'FILLED' if use_fok else 'PLACED'}: {order_id}", flush=True)

            # FOK fills instantly — goes straight to position
            initial_status = 'open' if use_fok else 'pending'

            trade = {
                'id': trade_id,
                'order_id': order_id,
                'market_id': signal.market_id,
                'coin': signal.coin,
                'timeframe': signal.timeframe,
                'strategy': signal.strategy,
                'direction': signal.direction,
                'token_id': signal.token_id,
                'entry_price': price,
                'exit_price': None,
                'size_usd': actual_cost,
                'shares': shares,
                'pnl': None,
                'pnl_pct': None,
                'confidence': signal.confidence,
                'entry_time': now,
                'exit_time': None,
                'exit_reason': None,
                'status': initial_status,
                'rationale': signal.rationale,
                'metadata': signal.metadata,
                'placed_at': time.time(),
                'fee_rate': trade_fee,
                'order_type': order_type_tag,
                '_live': True,
            }

            if use_fok:
                # FOK filled — track as open position immediately
                self.positions[trade_id] = trade
            else:
                # GTC — track as pending, check_pending_orders will move to position on fill
                self.pending_orders[trade_id] = trade

            self.balance_mgr.open_positions += 1
            self.balance_mgr.update_balance(self.balance_mgr.balance - actual_cost)

            # Also update cached real balance
            if self._cached_real_balance is not None:
                self._cached_real_balance = max(0, self._cached_real_balance - actual_cost)

            await self.db.save_trade(trade)
            self._consecutive_failures = 0  # Reset on success

            # ── Log BUY to CSV ──
            try:
                self.trade_logger.log_buy(
                    trade, self.balance_mgr.balance,
                    self.balance_mgr.mode.name, self.balance_mgr)
            except Exception:
                pass  # Never let logging break trading

            return trade

        except Exception as e:
            error_str = str(e).lower()
            print(f"❌ Order error: {e}", flush=True)

            # ── FOK didn't fill: try GTC fallback ──
            if use_fok and ('not fill' in error_str or 'no fill' in error_str):
                # Pre-check: can we afford 5 shares at this price for GTC?
                tick_price = max(0.01, min(0.99, round(signal.entry_price * 100) / 100))
                gtc_cost = round(tick_price * 5, 2)
                real_bal_check = await self._get_cached_balance()
                if real_bal_check is not None and gtc_cost > real_bal_check:
                    print(f"⚠️ FOK exception, GTC needs ${gtc_cost:.2f} but only ${real_bal_check:.2f} — skip", flush=True)
                    return None
                print(f"🔄 FOK didn't fill — falling back to GTC", flush=True)
                return await self._place_buy(signal, size, use_fok=False)

            # ── Auto-retry on minimum size errors (GTC only) ──
            if not use_fok and ('lower than the minimum' in error_str or
                    ('invalid' in error_str and 'size' in error_str)):
                try:
                    import re
                    min_match = re.search(r'minimum[:\s]+(\d+)', error_str)
                    if min_match:
                        retry_shares = max(int(min_match.group(1)), math.ceil(shares))
                    else:
                        retry_shares = max(5, math.ceil(shares))
                    
                    if retry_shares > shares:
                        print(f"🔄 Retrying GTC with {retry_shares} shares", flush=True)
                        from py_clob_client.clob_types import OrderType as OT
                        resp = await self._submit_order(signal.token_id, price, retry_shares, BUY, OT.GTC)
                        if resp and resp.get('status') != 'error':
                            order_id = resp.get('orderID', resp.get('id', str(uuid.uuid4())[:8]))
                            actual_cost = round(price * retry_shares, 2)  # max 2 decimals
                            print(f"✅ [GTC] RETRY PLACED: {order_id} (${actual_cost:.2f})", flush=True)
                            trade_id = str(uuid.uuid4())[:8]
                            trade = {
                                'id': trade_id, 'order_id': order_id,
                                'market_id': signal.market_id, 'coin': signal.coin,
                                'timeframe': signal.timeframe, 'strategy': signal.strategy,
                                'direction': signal.direction, 'token_id': signal.token_id,
                                'entry_price': price, 'exit_price': None,
                                'size_usd': actual_cost, 'shares': retry_shares,
                                'pnl': None, 'pnl_pct': None,
                                'confidence': signal.confidence,
                                'entry_time': datetime.now().isoformat(),
                                'exit_time': None, 'exit_reason': None,
                                'status': 'pending', 'rationale': signal.rationale,
                                'metadata': signal.metadata, 'placed_at': time.time(),
                                'fee_rate': trade_fee, 'order_type': 'GTC',
                                '_live': True,
                            }
                            self.pending_orders[trade_id] = trade
                            self.balance_mgr.open_positions += 1
                            self.balance_mgr.update_balance(self.balance_mgr.balance - actual_cost)
                            if self._cached_real_balance is not None:
                                self._cached_real_balance = max(0, self._cached_real_balance - actual_cost)
                            await self.db.save_trade(trade)
                            self._consecutive_failures = 0
                            return trade
                except Exception as retry_err:
                    print(f"❌ Retry also failed: {retry_err}", flush=True)

            # Detect balance/allowance errors and stop spamming
            if 'balance' in error_str or 'allowance' in error_str:
                self._consecutive_failures += 1
                self._last_balance_check = 0  # Force refresh next time
                if self._consecutive_failures >= 5:
                    self._trading_paused = True
                    self._paused_at = time.time()
                    self._pause_reason = 'Not enough balance/allowance'
                    print(f"\n{'='*60}", flush=True)
                    print(f"🛑 TRADING PAUSED: {self._consecutive_failures} consecutive balance errors", flush=True)
                    print(f"  Will auto-resume in 5 minutes to retry.", flush=True)
                    print(f"  If persistent, deposit USDC at https://polymarket.com", flush=True)
                    print(f"{'='*60}\n", flush=True)

            return None

    # Keep backward compatibility
    async def _place_limit_buy(self, signal: TradeSignal, size: float) -> Optional[Dict]:
        """Backward-compatible wrapper — uses GTC limit order."""
        return await self._place_buy(signal, size, use_fok=False)

    async def _submit_order(self, token_id: str, price: float, shares: float, side, order_type=None) -> Optional[Dict]:
        """Submit a signed order to the CLOB. Supports GTC, FOK, FAK.
        
        Runs synchronous py-clob-client calls in a thread executor to
        avoid blocking the asyncio event loop (each call takes 1-20s).
        
        Polymarket precision requirements:
        - price: max 2 decimal places (tick size 0.01)
        - size/shares: max 2 decimal places (py-clob-client rounds to 2dp)
        - price × shares (maker_amount): max 2 decimal places
        
        The py-clob-client library rounds size to 2dp internally but does NOT
        ensure the product has ≤ 2dp. The CLOB API server rejects orders where
        maker_amount has > 2 decimal places. This method enforces that constraint
        by adjusting shares to the nearest valid step.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        if order_type is None:
            order_type = OrderType.GTC

        # ── Enforce Polymarket decimal precision ──
        # Price: max 2 decimals (tick size 0.01)
        price = round(float(price), 2)
        # Size: floor to 2 decimals (matches py-clob-client's round_down(size, 2))
        shares = math.floor(float(shares) * 100) / 100

        # ── FIX: Ensure price × shares has ≤ 2 decimal places ──
        # The CLOB API rejects BUY orders where maker_amount = price × shares
        # exceeds 2 decimal places (e.g. 0.77 × 1.30 = 1.001 → 3dp → rejected).
        # For the product (P/100)×(S/100) = P×S/10000 to have ≤ 2dp,
        # P×S must be divisible by 100. Valid share steps depend on the price.
        P = int(round(price * 100))
        S = int(math.floor(shares * 100))
        if P > 0 and S > 0:
            step = 100 // math.gcd(P, 100)  # shares_cents must be multiple of this
            S = (S // step) * step
            shares = S / 100.0

        order_args = OrderArgs(
            price=price,
            size=shares,
            side=side,
            token_id=token_id,
        )
        loop = asyncio.get_event_loop()
        signed_order = await loop.run_in_executor(
            None, self.clob_client.create_order, order_args
        )
        return await loop.run_in_executor(
            None, self.clob_client.post_order, signed_order, order_type
        )

    async def check_pending_orders(self):
        """Check if pending orders have been filled, partially filled, or need cancellation."""
        if not self.is_ready:
            return

        now = time.time()
        to_remove = []

        for trade_id, order in list(self.pending_orders.items()):
            order_id = order.get('order_id', '')
            placed_at = order.get('placed_at', now)

            try:
                clob_order = self.clob_client.get_order(order_id)
                if clob_order:
                    status = clob_order.get('status', '').lower()

                    if status in ('matched', 'filled'):
                        # Fully filled
                        fill_price = float(clob_order.get('price', order['entry_price']))
                        fill_size = float(clob_order.get('size_matched', order.get('shares', 0)))
                        order['status'] = 'open'
                        order['entry_price'] = fill_price
                        if fill_size > 0:
                            order['shares'] = fill_size
                        self.positions[trade_id] = order
                        to_remove.append(trade_id)
                        print(f"🟢 FILLED: {order['coin']} {order['direction']} "
                              f"@ ${fill_price:.3f} ({fill_size:.1f} shares)", flush=True)
                        continue

                    elif status == 'cancelled':
                        to_remove.append(trade_id)
                        self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
                        self.balance_mgr.update_balance(
                            self.balance_mgr.balance + order['size_usd']
                        )
                        print(f"❌ CANCELLED externally: {order['coin']} {order['direction']}", flush=True)
                        continue

                    elif status == 'live':
                        # Still in the book — check for partial fills
                        size_matched = float(clob_order.get('size_matched', 0))
                        original_size = order.get('shares', 0)
                        if size_matched > 0 and size_matched < original_size:
                            # Partial fill — log it but keep waiting
                            fill_pct = size_matched / original_size * 100 if original_size > 0 else 0
                            if not order.get('_partial_logged'):
                                print(f"⏳ PARTIAL: {order['coin']} {order['direction']} "
                                      f"{fill_pct:.0f}% filled ({size_matched:.1f}/{original_size:.1f})",
                                      flush=True)
                                order['_partial_logged'] = True
            except Exception:
                pass

            # Timeout: cancel and recover balance
            if now - placed_at > self.ORDER_TIMEOUT:
                try:
                    self.clob_client.cancel(order_id)
                    print(f"⏰ CANCELLED (timeout): {order['coin']} {order['direction']} "
                          f"@ ${order['entry_price']:.3f}", flush=True)
                except Exception:
                    pass
                to_remove.append(trade_id)
                self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
                self.balance_mgr.update_balance(
                    self.balance_mgr.balance + order['size_usd']
                )

        for tid in to_remove:
            self.pending_orders.pop(tid, None)

        # ── Check pending GTC SELL orders on open positions ──
        SELL_TIMEOUT = 120  # 2 minutes max for GTC sell to fill
        for trade_id, pos in list(self.positions.items()):
            pending_sell = pos.get('_pending_sell')
            if not pending_sell:
                continue
            sell_order_id = pending_sell.get('order_id', '')
            sell_placed_at = pending_sell.get('placed_at', now)
            try:
                sell_info = self.clob_client.get_order(sell_order_id)
                if sell_info:
                    sell_status = sell_info.get('status', '').lower()
                    if sell_status in ('matched', 'filled'):
                        fill_price = float(sell_info.get('price', pending_sell['sell_price']))
                        pnl = (fill_price - pos['entry_price']) * pos.get('shares', 0)
                        self._finalize_close(trade_id, fill_price, pnl, pending_sell.get('reason', 'gtc_sell'))
                        print(f"🟢 GTC SELL FILLED: {pos.get('coin','')} {pos.get('direction','')} @ ${fill_price:.3f}", flush=True)
                        continue
                    elif sell_status == 'cancelled':
                        # Sell was cancelled externally — remove tracking, position stays open
                        del pos['_pending_sell']
                        print(f"⚠️ GTC sell cancelled externally: {pos.get('coin','')}", flush=True)
                        continue
            except Exception:
                pass

            # Timeout: cancel GTC sell, position stays open for retry next cycle
            if now - sell_placed_at > SELL_TIMEOUT:
                try:
                    self.clob_client.cancel(sell_order_id)
                    print(f"⏰ GTC sell timeout — cancelled, will retry: {pos.get('coin','')}", flush=True)
                except Exception:
                    pass
                del pos['_pending_sell']

    async def check_positions(self, current_prices: Dict[str, float],
                                seconds_remaining_map: Dict[str, int] = None) -> List[Dict]:
        """Check open positions for exit signals."""
        closed = []
        seconds_remaining_map = seconds_remaining_map or {}

        await self.check_pending_orders()

        # ── Update estimated position value for session breaker accuracy ──
        # This prevents the session breaker from false-triggering when USDC
        # drops purely because we bought a position (not because we lost).
        est_value = 0.0
        for pos in self.positions.values():
            token_id = pos['token_id']
            cur = current_prices.get(token_id)
            if cur is not None:
                est_value += cur * pos.get('shares', 0)
            else:
                # No live price yet — use entry price as conservative estimate
                est_value += pos.get('size_usd', 0)
        self.balance_mgr._estimated_position_value = est_value

        for trade_id, pos in list(self.positions.items()):
            # Skip positions that have a pending GTC sell order
            if pos.get('_pending_sell'):
                continue

            token_id = pos['token_id']
            current_price = current_prices.get(token_id)
            if current_price is None:
                continue

            secs = seconds_remaining_map.get(pos.get('market_id', ''), 999)
            pos['_seconds_remaining'] = secs  # Used by _close_position for near-expiry logic

            # Use per-position fee rate (not global which gets overwritten per-trade)
            pos_fee_rate = pos.get('fee_rate', self.TAKER_FEE_RATE)
            decision = self._exit_decision(
                pos['entry_price'], current_price, secs, pos_fee_rate,
                strategy=pos.get('strategy', ''), pos=pos
            )

            if decision in ('sell', 'cut_loss'):
                pnl = (current_price - pos['entry_price']) * pos.get('shares', 0)
                reason = 'profit_take' if decision == 'sell' else 'stop_loss'
                result = await self._close_position(trade_id, current_price, pnl, reason)
                if result:
                    closed.append(pos)

        return closed

    def _exit_decision(self, entry_price: float, current_price: float,
                       seconds_remaining: int, fee_rate: float = None,
                       strategy: str = '', pos: dict = None) -> str:
        """Dynamic exit decision for 5-minute binary markets.
        
        These markets resolve to $1 (win) or $0 (lose). Prices are probabilities.
        
        Stop-loss is DYNAMIC based on:
          - Strategy: time_decay/expiry_rush near expiry get wider stops
          - Entry price: cheap entries get very wide stops (lottery math)
          - Time remaining: <30s → NO stop, let it settle to $1 or $0
          - Mode: AGGRESSIVE gets 2x wider stops, SEED gets base
        
        Profit-taking:
          - High-probability entries (>0.80): Hold for settlement ($1 payout)
          - Mid entries: Dynamic targets based on time remaining
          - Cheap entries: Hold for 2.5x+ multiples
        """
        if entry_price <= 0:
            return 'hold'

        # Entry fee: stored per-position at buy time
        entry_fee = fee_rate if fee_rate is not None else self._get_dynamic_fee_rate(entry_price)
        # Sell fee: calculated at CURRENT price (sell fee depends on sell price)
        sell_fee = self._get_dynamic_fee_rate(current_price)

        # Fee-aware calculations: what we'd NET if we sell NOW
        net_gain = (current_price * (1 - sell_fee)) / (entry_price * (1 + entry_fee))
        pnl_pct = (net_gain - 1) * 100
        
        # Settlement is FREE (0% exit fee) — max payout if market resolves at $1.00
        max_payout_gain = 1.0 / (entry_price * (1 + entry_fee))
        # How much current gain as fraction of max possible
        gain_captured = (net_gain - 1) / (max_payout_gain - 1) if max_payout_gain > 1 else 0

        mode = self.balance_mgr.mode_name

        # ══════════════════════════════════════════════════════════
        # DYNAMIC STOP-LOSS — adapts to strategy, price, time, mode
        # ══════════════════════════════════════════════════════════

        # 1. Near expiry (<30s) → NEVER stop out. Market settles in seconds.
        #    Selling into thin liquidity near expiry guarantees slippage.
        #    Better to let it resolve to $1 (win) or $0 (lose).
        if seconds_remaining < 30 and pnl_pct < 0:
            return 'hold'  # Let it settle — don't panic sell

        # 2. Base stop by entry price tier (higher entry = tighter stop)
        if entry_price < 0.15:
            base_stop = -60   # Pennies: let them ride, lottery math
        elif entry_price < 0.30:
            base_stop = -40   # Cheap: wide stop, expected to be volatile
        elif entry_price < 0.50:
            base_stop = -25   # Mid-range: moderate room
        elif entry_price < 0.70:
            base_stop = -18   # Higher probability: tighter
        elif entry_price < 0.85:
            base_stop = -12   # High prob: fairly tight
        else:
            base_stop = -8    # Near-certain: very tight

        # 3. Strategy-specific override (some strategies need wider stops)
        strategy = strategy or (pos.get('strategy', '') if pos else '')
        if strategy in ('time_decay', 'expiry_rush'):
            # These bet on settlement direction — they NEED to survive to expiry
            base_stop = min(base_stop, -35)  # At least -35%, never tighter
        elif strategy == 'early_mover':
            base_stop = min(base_stop, -30)  # Cheap reversals need room

        # 4. Mode multiplier (AGGRESSIVE gets widest stops, SEED gets base)
        mode_mult = {'seed': 1.0, 'concentration': 1.2, 'medium': 1.5, 'aggressive': 2.0}
        max_loss = base_stop * mode_mult.get(mode, 1.0)

        # 5. Time-based widening: more time left → wider stop (can recover)
        if seconds_remaining > 120:
            max_loss *= 1.3   # 2+ min left: 30% wider
        elif seconds_remaining > 60:
            max_loss *= 1.15  # 1-2 min left: 15% wider

        # ── Hard stop loss — respect the dynamic threshold ──
        if pnl_pct <= max_loss:
            return 'cut_loss'

        # ── HIGH-PROBABILITY entry (>$0.80): Hold for $1 settlement ──
        if entry_price >= 0.80:
            # Price reversed below entry → market is turning against us
            if current_price < entry_price * 0.92 and seconds_remaining > 30:
                return 'cut_loss'
            # Last 10s: always hold — settlement is imminent and free
            if seconds_remaining < 10:
                return 'hold'
            # Otherwise hold — let it settle at $1
            return 'hold'

        # ── MID-PROBABILITY entry ($0.30-$0.80): Smart exit targets ──
        # Math: selling costs ~1.5% fee. Settlement to $1.00 is FREE.
        # But settlement to $0.00 = total loss. Key: only hold for
        # settlement when IMPLIED WIN PROBABILITY is high enough (≥85%).
        # Below 85%: the 15%+ risk of losing entire position outweighs
        # the 1.5% fee savings, especially with a small bankroll.
        if entry_price >= 0.30:
            # ── LAST 10s: Settlement imminent ──
            if seconds_remaining < 10:
                if current_price >= 0.85:
                    return 'hold'  # 85%+ win prob: hold for FREE settlement
                if pnl_pct < -10:
                    return 'cut_loss'  # Deep red → bail before $0
                if pnl_pct > 3:
                    return 'sell'  # Lock small profit, don't gamble
                return 'hold'  # Breakeven: not worth selling for dust

            # ── LAST 30s: Conservative settlement hold ──
            if seconds_remaining <= 30:
                if current_price >= 0.90:
                    return 'hold'  # 90%+ → near-certain win, save the fee
                if current_price >= 0.85 and pnl_pct > 10:
                    return 'hold'  # 85%+ AND solidly profitable → hold
                if pnl_pct < -10:
                    return 'cut_loss'
                if pnl_pct >= 15:
                    return 'sell'  # Below 85% win prob: SELL to lock profit
                if pnl_pct > 3:
                    return 'sell'  # Small profit + uncertain → lock it
                return 'hold'  # Near-breakeven: hold, fee would eat profit

            # ── 30-60s left: Only sell if strong gain ──
            if seconds_remaining > 30:
                if pnl_pct >= 30:
                    return 'sell'  # 30%+ gain with time left = bank it

            # ── 60+ seconds left: Even higher bar for early exit ──
            if seconds_remaining > 60:
                if pnl_pct >= 40:
                    return 'sell'  # 40%+ early = take profit

            # Time running out and losing → cut before settlement
            if seconds_remaining < 20 and pnl_pct < -5:
                return 'cut_loss'

            return 'hold'

        # ── CHEAP entry (<$0.30): Lottery tickets — smart profit taking ──
        # The math: lose $1 on 10 markets = -$10. Win $100 on 1 = +$90 net.
        # But DON'T always hold to $1 — take profit at multiples to lock gains.

        if entry_price < 0.05:
            # Ultra-cheap penny bets ($0.01-$0.05): let them run further
            if pnl_pct >= 400:       # 5x+ (e.g., $0.01 → $0.05): bank it
                return 'sell'
            if pnl_pct >= 200 and seconds_remaining < 30:  # 3x near expiry
                return 'sell'
            if seconds_remaining < 10 and pnl_pct > 30:    # Last moments, any decent gain
                return 'sell'
        else:
            # Cheap but not penny ($0.05-$0.30)
            if pnl_pct >= 150:       # 2.5x → take profit
                return 'sell'
            if pnl_pct >= 80 and seconds_remaining < 30:   # Near expiry, ~2x
                return 'sell'
            if seconds_remaining < 10 and pnl_pct > 20:
                return 'sell'
            # Cut losers closer to expiry if deep in the red
            if seconds_remaining < 20 and pnl_pct < -30:
                return 'cut_loss'

        # Let cheap bets ride — they settle to $1 or $0
        return 'hold'

    async def _ensure_conditional_allowance(self, token_id: str) -> bool:
        """Refresh conditional token allowance before selling.
        
        This fires update_balance_allowance (a GET that tells the CLOB to
        re-read on-chain state) and immediately returns True.
        
        On-chain setApprovalForAll is ensured at startup. The CLOB's cached
        allowance value is often stale/zero — we never block sells on it.
        This is the same pattern poly_trade uses successfully.
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=self._sig_type,
            )
            self.clob_client.update_balance_allowance(params)
        except Exception as e:
            print(f"⚠️ update_balance_allowance non-fatal: {e}", flush=True)
        return True

    async def _close_position(self, trade_id: str, exit_price: float,
                              pnl: float, reason: str) -> bool:
        """Close a position by placing a sell order.
        
        Strategy: Check orderbook first for real bids, then FOK sell at best_bid,
        GTC fallback if FOK fails. Detects expired markets and auto-settles.
        Limits sell retries to 3 per position to avoid API spam.
        
        Near-expiry logic: If <15s remain, skip selling — the CLOB orderbook
        gets deleted at market close (returns 404), so sell ALWAYS fails.
        Better to let the market settle and auto-redeem the tokens.
        """
        import math
        pos = self.positions.get(trade_id)
        if not pos:
            return False

        shares = pos.get('shares', 0)
        if shares <= 0:
            return False

        # ── Near-expiry: skip sell, let market settle ──
        # The CLOB orderbook is deleted when the market closes (returns 404).
        # Selling in the last ~15s wastes retries and always fails.
        # Let the market settle to $1/$0 and auto-redeem handles the payout.
        secs_left = pos.get('_seconds_remaining', 999)
        if secs_left < 15 and reason in ('profit_take', 'stop_loss'):
            print(f"⏰ <15s left for {pos['coin']} {pos['direction']} — "
                  f"skipping sell, letting market settle", flush=True)
            return False

        # ── Sell retry limiter ──
        MAX_SELL_RETRIES = 3
        sell_fails = pos.get('_sell_fails', 0)
        if sell_fails >= MAX_SELL_RETRIES:
            print(f"⏰ Max sell retries ({MAX_SELL_RETRIES}) reached — auto-settling "
                  f"{pos['coin']} {pos['direction']}", flush=True)
            self._finalize_close(trade_id, exit_price, pnl, 'sell_failed_settle')
            return True

        # ── Smart exit: check orderbook for real bids before selling ──
        orderbook_price = None
        bid_depth = 0
        if self.clob_reader:
            try:
                book = self.clob_reader.get_orderbook(pos['token_id'])
                if book and not book.get('_synthetic'):
                    best_bid = book.get('best_bid', 0)
                    bid_depth = book.get('bid_depth', 0)
                    if best_bid > 0 and bid_depth > 0:
                        orderbook_price = best_bid
                    elif best_bid <= 0 or bid_depth < 0.01:
                        # No real bids in orderbook — selling will fail
                        # Let market settle instead of wasting retries
                        print(f"📊 No bids for {pos['coin']} {pos['direction']} "
                              f"(bid={best_bid:.3f}, depth=${bid_depth:.2f}) — "
                              f"holding for settlement", flush=True)
                        return False
            except Exception as e:
                err_str = str(e).lower()
                # Orderbook 404 = market closed/expired
                if 'does not exist' in err_str or '404' in err_str:
                    print(f"⏰ Orderbook gone for {pos['coin']} {pos['direction']} — "
                          f"market expired, auto-settling", flush=True)
                    self._finalize_close(trade_id, exit_price, pnl, 'market_settled')
                    return True

        # Use orderbook best_bid if available, otherwise use exit_price
        effective_exit = orderbook_price if orderbook_price else exit_price

        # ── Share sizing ──
        # FOK sell: use EXACT share count from position (never ceil — we can't
        # sell more shares than we own; ceil caused 'not enough balance' errors)
        # GTC sell: enforce 5-share minimum, but SKIP if we have fewer than 5
        sell_shares_fok = math.floor(shares * 100) / 100 if shares > 0 else shares
        can_gtc_sell = shares >= 5  # GTC requires min 5 shares — don't even try if < 5
        sell_shares_gtc = max(5, math.ceil(shares)) if can_gtc_sell else sell_shares_fok

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            # Tick-align exit price — use orderbook-aware price
            sell_price = max(0.01, min(0.99, round(effective_exit * 100) / 100))

            # ── Cross the spread: aggressive FOK sell pricing ──
            # In thin 5m markets, FOK at exact price has ~20% fill rate.
            # Lowering the limit price widens the set of matching bids.
            # Exchange fills at BEST available bid, so actual fill ≥ our limit.
            # Use 5% of price (min 2¢, max 4¢) — deeper for thin markets.
            sell_discount = min(0.04, max(0.02, round(sell_price * 0.05 * 100) / 100))
            sell_price_aggressive = max(0.01, sell_price - sell_discount)

            # ── Refresh conditional token allowance (fire-and-forget) ──
            await self._ensure_conditional_allowance(pos['token_id'])

            # ═══ Attempt 1: FOK sell with spread-crossing price ═══
            print(f"📤 [FOK] SELL: {pos['coin']} {pos['direction']} | "
                  f"{sell_shares_fok} shares @ ${sell_price_aggressive:.3f} "
                  f"(cross spread, base ${sell_price:.3f}) [{reason}] "
                  f"(attempt {sell_fails + 1}/{MAX_SELL_RETRIES})", flush=True)

            resp = await self._submit_order(pos['token_id'], sell_price_aggressive, sell_shares_fok, SELL, OrderType.FOK)

            if resp and resp.get('status') != 'error':
                print(f"✅ [FOK] SELL FILLED instantly", flush=True)
                self._finalize_close(trade_id, exit_price, pnl, reason)
                return True

            # ═══ Attempt 2: FOK with slightly fewer shares (rounding safety) ═══
            reduced_shares = math.floor(sell_shares_fok * 0.95 * 100) / 100
            if reduced_shares >= 0.01 and reduced_shares != sell_shares_fok:
                print(f"⚠️ FOK full failed — retrying with {reduced_shares} shares (95%)", flush=True)
                resp_reduced = await self._submit_order(pos['token_id'], sell_price_aggressive, reduced_shares, SELL, OrderType.FOK)
                if resp_reduced and resp_reduced.get('status') != 'error':
                    print(f"✅ [FOK] SELL FILLED with reduced shares", flush=True)
                    self._finalize_close(trade_id, exit_price, pnl, reason)
                    return True

            # ═══ Attempt 3: GTC sell at current price (queue) ═══
            if not can_gtc_sell:
                # Shares < 5: can't do GTC. If this is already retry 2+,
                # auto-settle instead of looping forever on tiny positions.
                pos['_sell_fails'] = sell_fails + 1
                if pos['_sell_fails'] >= 2:
                    print(f"⚠️ FOK failed twice, shares<5 → auto-settling "
                          f"{pos['coin']} {pos['direction']}", flush=True)
                    self._finalize_close(trade_id, exit_price, pnl, 'sell_failed_settle')
                    return True
                print(f"⚠️ FOK sell didn't fill, GTC skipped (shares={shares:.1f} < 5)", flush=True)
                return False
            print(f"⚠️ FOK sell didn't fill — trying GTC", flush=True)
            resp2 = await self._submit_order(pos['token_id'], sell_price, sell_shares_gtc, SELL, OrderType.GTC)

            if resp2 and resp2.get('status') != 'error':
                sell_order_id = resp2.get('orderID', resp2.get('id', ''))
                pos['_pending_sell'] = {
                    'order_id': sell_order_id,
                    'sell_price': sell_price,
                    'placed_at': time.time(),
                    'reason': reason,
                }
                print(f"📋 GTC SELL queued: {sell_order_id} — will confirm fill later", flush=True)
                return True

            # ═══ Attempt 4: GTC sell 1¢ lower ═══
            print(f"⚠️ GTC sell didn't fill — trying 1¢ lower", flush=True)
            retry_price = max(0.01, sell_price - 0.01)
            resp3 = await self._submit_order(pos['token_id'], retry_price, sell_shares_gtc, SELL, OrderType.GTC)

            if resp3 and resp3.get('status') != 'error':
                sell_order_id = resp3.get('orderID', resp3.get('id', ''))
                pos['_pending_sell'] = {
                    'order_id': sell_order_id,
                    'sell_price': retry_price,
                    'placed_at': time.time(),
                    'reason': reason,
                }
                print(f"📋 GTC SELL queued (1¢ lower): {sell_order_id}", flush=True)
                return True

            error_msg = resp3.get('errorMsg', 'Unknown') if resp3 else 'No response'
            print(f"❌ All sell attempts failed: {error_msg}", flush=True)
            pos['_sell_fails'] = sell_fails + 1

        except Exception as e:
            error_str = str(e).lower()

            # ── Detect expired/resolved market ──
            if 'does not exist' in error_str or 'orderbook' in error_str:
                print(f"⏰ Market expired/resolved — auto-settling {pos['coin']} {pos['direction']}", flush=True)
                self._finalize_close(trade_id, exit_price, pnl, 'market_settled')
                return True

            # ── Handle minimum size errors on sell ──
            if 'lower than the minimum' in error_str:
                import re
                min_match = re.search(r'minimum[:\s]+(\d+)', error_str)
                required_min = int(min_match.group(1)) if min_match else 5
                print(f"🔄 Sell size too small, retrying with {required_min}", flush=True)
                try:
                    resp_min = await self._submit_order(pos['token_id'], sell_price, required_min, SELL, OrderType.GTC)
                    if resp_min and resp_min.get('status') != 'error':
                        self._finalize_close(trade_id, exit_price, pnl, reason)
                        return True
                except Exception as retry_e:
                    print(f"❌ Min-size retry also failed: {retry_e}", flush=True)

            # ── Handle balance/allowance errors: retry with 95% shares ──
            # CLOB sometimes reports fewer shares than we have (rounding).
            # A 95% reduced FOK often succeeds when the full amount fails.
            if 'not enough balance' in error_str or 'allowance' in error_str:
                reduced = math.floor(sell_shares_fok * 0.95 * 100) / 100
                if reduced >= 0.01:
                    print(f"🔄 Balance/allowance error — retrying FOK with {reduced} shares (95%)...", flush=True)
                    try:
                        retry_resp = await self._submit_order(
                            pos['token_id'], sell_price_aggressive, reduced, SELL, OrderType.FOK
                        )
                        if retry_resp and retry_resp.get('status') != 'error':
                            print(f"✅ Reduced-share sell succeeded!", flush=True)
                            self._finalize_close(trade_id, exit_price, pnl, reason)
                            return True
                    except Exception as retry_e:
                        print(f"❌ Reduced-share retry also failed: {retry_e}", flush=True)

            # General sell error — count it and move on
            pos['_sell_fails'] = sell_fails + 1
            print(f"❌ Sell error: {e} ({MAX_SELL_RETRIES - pos['_sell_fails']} retries left)", flush=True)

        return False

    def _finalize_close(self, trade_id: str, exit_price: float,
                        pnl: float, reason: str):
        """Finalize a closed position. Fee-aware PnL. Tracks consecutive wins/losses.
        
        Special cases:
        - sell_failed_settle: all sell attempts failed, tokens unredeemed.
        - market_settled: market expired during sell attempt. We DON'T know the
          outcome yet — tokens need auto-redeem. Treat like sell_failed_settle:
          don't fake PnL, let auto_redeem handle the actual payout.
        The balance sync (every 60s) will pick up any real USDC once the
        outcome tokens are auto-redeemed or manually redeemed on-chain.
        """
        pos = self.positions.pop(trade_id, None)
        if not pos:
            return

        # Always free the position slot
        self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)

        # Clear estimated position value (recalculated next check_positions)
        self.balance_mgr._estimated_position_value = 0.0

        # ── Record stop-loss cooldown to prevent immediate re-entry ──
        if reason in ('stop_loss', 'sell_failed_settle'):
            cooldown_key = f"{pos.get('coin', '')}_{pos.get('direction', '')}"
            self._stop_loss_cooldowns[cooldown_key] = time.time()
            # Global post-loss throttle: brief pause on ALL trades after stop
            self._last_stop_loss_time = time.time()

        # ── sell_failed_settle / market_settled: tokens unredeemed ──
        # Don't fake PnL — we don't know the outcome until auto-redeem runs.
        # Balance sync will pick up USDC once tokens are redeemed on-chain.
        if reason in ('sell_failed_settle', 'market_settled'):
            pos['exit_price'] = exit_price
            pos['pnl_gross'] = pnl
            pos['pnl'] = 0        # Can't realize PnL without actually selling
            pos['fees'] = 0
            pos['pnl_pct'] = 0
            pos['exit_time'] = datetime.now().isoformat()
            pos['exit_reason'] = reason
            pos['status'] = 'settled_unredeemed'
            self.trade_history.append(pos)
            # DON'T update balance — USDC is still locked in outcome tokens
            label = "expired" if reason == 'market_settled' else "unsold"
            print(f"⏰ SETTLED ({label}): {pos['coin']} {pos['direction']} — "
                  f"Entry:${pos['entry_price']:.3f} → Mkt:${exit_price:.3f} | "
                  f"~{pos.get('shares',0):.1f} tokens awaiting redemption | "
                  f"Bal stays ${self.balance_mgr.balance:.2f}",
                  flush=True)

            # ── Log SETTLE to CSV ──
            try:
                self.trade_logger.log_sell(
                    pos, self.balance_mgr.balance,
                    self.balance_mgr.mode.name, self.balance_mgr)
            except Exception:
                pass

            return

        # ── Normal close: position was actually sold ──
        # Subtract estimated taker fees from PnL (entry + exit)
        # Entry fee: rate at entry price (stored per-position)
        entry_fee_rate = pos.get('fee_rate', self._get_dynamic_fee_rate(pos['entry_price']))
        # Exit fee: rate at exit price (different from entry — fee depends on price)
        exit_fee_rate = self._get_dynamic_fee_rate(exit_price)
        entry_fee = pos['entry_price'] * pos.get('shares', 0) * entry_fee_rate
        exit_fee = exit_price * pos.get('shares', 0) * exit_fee_rate
        total_fees = entry_fee + exit_fee
        net_pnl = pnl - total_fees

        pos['exit_price'] = exit_price
        pos['pnl_gross'] = pnl
        pos['pnl'] = net_pnl
        pos['fees'] = total_fees
        pos['pnl_pct'] = (net_pnl / pos['size_usd'] * 100) if pos['size_usd'] > 0 else 0
        pos['exit_time'] = datetime.now().isoformat()
        pos['exit_reason'] = reason
        pos['status'] = 'closed'

        self.trade_history.append(pos)
        self.balance_mgr.update_balance(self.balance_mgr.balance + pos['size_usd'] + net_pnl)

        # ── Force on-chain balance sync after sell ──
        # The internal balance calculation (balance + cost + pnl) can drift
        # from reality due to fee rounding, partial fills, etc.
        # Schedule an immediate balance check to ground-truth.
        self._last_balance_check = 0  # Force next _get_cached_balance to refresh

        # ── Track consecutive wins/losses for dynamic sizing ──
        won = net_pnl > 0
        self.balance_mgr.record_result(won)

        gain = exit_price / pos['entry_price'] if pos['entry_price'] > 0 else 0
        emoji = '🤑' if net_pnl > 0 else '💸'
        streak = (f"W{self.balance_mgr._consecutive_wins}" if won
                  else f"L{self.balance_mgr._consecutive_losses}")
        sizing = f"sizing×{self.balance_mgr._size_multiplier:.2f}"
        print(f"{emoji} LIVE CLOSED: {pos['coin']} {pos['direction']} — "
              f"Entry:${pos['entry_price']:.3f} -> Exit:${exit_price:.3f} | "
              f"Gross:${pnl:+.2f} Fees:${total_fees:.2f} Net:${net_pnl:+.2f} "
              f"({gain:.1f}x) [{reason}] | "
              f"Bal:${self.balance_mgr.balance:.2f} {streak} {sizing}", flush=True)

        # ── Log SELL to CSV ──
        try:
            self.trade_logger.log_sell(
                pos, self.balance_mgr.balance,
                self.balance_mgr.mode.name, self.balance_mgr)
        except Exception:
            pass

    async def cancel_all_orders(self):
        """Emergency: cancel all pending orders."""
        if not self.is_ready:
            return 0

        count = 0
        try:
            self.clob_client.cancel_all()
            count = len(self.pending_orders)
            for tid, order in list(self.pending_orders.items()):
                self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
                self.balance_mgr.update_balance(
                    self.balance_mgr.balance + order['size_usd']
                )
            self.pending_orders.clear()
            print(f"🛑 Cancelled {count} pending orders", flush=True)
        except Exception as e:
            print(f"❌ Cancel all error: {e}", flush=True)
        return count

    def get_open_positions(self) -> List[Dict]:
        return list(self.positions.values()) + list(self.pending_orders.values())

    def get_summary(self) -> Dict:
        status = self.balance_mgr.get_status()
        wins = sum(1 for t in self.trade_history if (t.get('pnl', 0) or 0) > 0)
        total = len(self.trade_history)
        total_pnl = sum(t.get('pnl', 0) or 0 for t in self.trade_history)
        return {
            **status,
            'total_trades': total,
            'wins': wins,
            'losses': total - wins,
            'win_rate': (wins / total * 100) if total > 0 else 0,
            'total_pnl': total_pnl,
            'open_count': len(self.positions),
            'pending_count': len(self.pending_orders),
            '_live': True,
        }
