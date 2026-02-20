"""
Live Trader — Real CLOB Order Execution

Uses py-clob-client to place real orders on Polymarket.
- GTC limit orders for BUYING (queue in orderbook)
- GTC limit sell at current price for SELLING (fast exit)
- Auto-cancel unfilled orders after timeout
- Tracks positions from CLOB fill confirmations
- Fee-aware PnL (dynamic taker fees on 5m/15m markets)

IMPORTANT: This trades REAL money. Start with $5-10 max.
"""

import uuid
import time
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
    
    Uses limit orders for entries (avoids slippage).
    Uses FOK orders for exits (instant fill).
    Tracks pending orders and auto-cancels stale ones.
    """

    ORDER_TIMEOUT = 60  # Cancel unfilled orders after 60 seconds (thin 5m markets need more time)
    BASE_TAKER_FEE_RATE = 0.0156  # ~1.56% dynamic taker fee on 5m/15m crypto markets
    TAKER_FEE_RATE = 0.0156  # Updated dynamically per-trade

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

    async def init(self):
        """Initialize CLOB client with credentials.
        
        Based on official py-clob-client docs:
        - signature_type=0: EOA/MetaMask (default)
        - signature_type=1: Email/Magic wallet
        - signature_type=2: Browser proxy wallet
        - funder: address holding funds (required for proxy wallets, optional for EOA)
        """
        private_key = Config.POLY_PRIVATE_KEY
        if not private_key:
            print("⚠️ No POLY_PRIVATE_KEY set — live trading disabled", flush=True)
            return False

        # Validate key format
        private_key = private_key.strip()
        if not private_key.startswith('0x'):
            private_key = '0x' + private_key
        if len(private_key) != 66:  # 0x + 64 hex chars
            print(f"❌ POLY_PRIVATE_KEY looks wrong (length={len(private_key)}, expected 66)", flush=True)
            print(f"  Format should be: 0x followed by 64 hex characters", flush=True)
            return False

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            print(f"🔑 Initializing CLOB client...", flush=True)

            host = Config.CLOB_API_URL
            chain_id = Config.POLY_CHAIN_ID  # 137 = Polygon
            sig_type = Config.POLY_SIGNATURE_TYPE  # 0=EOA, 1=Magic, 2=Proxy
            funder = Config.POLY_FUNDER_ADDRESS.strip() if Config.POLY_FUNDER_ADDRESS else None

            # If funder is empty string, set to None
            if funder == '':
                funder = None

            print(f"  Host: {host}", flush=True)
            print(f"  Chain: {chain_id}", flush=True)
            print(f"  Sig type: {sig_type} ({'EOA' if sig_type == 0 else 'Magic' if sig_type == 1 else 'Proxy'})", flush=True)
            print(f"  Funder: {funder or '(none - using EOA address)'}", flush=True)

            # Step 1: Create client
            self.clob_client = ClobClient(
                host,
                key=private_key,
                chain_id=chain_id,
                signature_type=sig_type,
                funder=funder,
            )

            # Step 2: Set or derive API credentials
            if Config.POLY_API_KEY and Config.POLY_API_KEY.strip():
                creds = ApiCreds(
                    api_key=Config.POLY_API_KEY.strip(),
                    api_secret=Config.POLY_API_SECRET.strip(),
                    api_passphrase=Config.POLY_PASSPHRASE.strip(),
                )
                self.clob_client.set_api_creds(creds)
                print(f"🔑 Using provided API credentials", flush=True)
            else:
                print(f"🔑 Deriving API credentials from private key...", flush=True)
                try:
                    derived = self.clob_client.create_or_derive_api_creds()
                    self.clob_client.set_api_creds(derived)
                    print(f"✅ API credentials derived successfully", flush=True)
                except Exception as e:
                    print(f"❌ Failed to derive API creds: {e}", flush=True)
                    print(f"  You may need to set POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE manually", flush=True)
                    return False

            # Step 3: Test connection
            try:
                ok = self.clob_client.get_ok()
                print(f"🟢 CLOB connection: {ok}", flush=True)
            except Exception as e:
                print(f"⚠️ CLOB connection test failed: {e}", flush=True)
                # Non-fatal — might still work

            # Step 4: Fetch dynamic fee rate
            try:
                import requests
                resp = requests.get(f"{host}/fees", timeout=5)
                if resp.status_code == 200:
                    fee_data = resp.json()
                    fee_rate = float(fee_data.get('taker', fee_data.get('fee_rate', self.TAKER_FEE_RATE)))
                    self.TAKER_FEE_RATE = fee_rate
                    self.BASE_TAKER_FEE_RATE = fee_rate
                    print(f"💰 Taker fee rate: {fee_rate:.4f} ({fee_rate*100:.2f}%)", flush=True)
            except Exception as e:
                print(f"⚠️ Fee rate fetch failed, using default: {e}", flush=True)

            # Step 5: Check actual balance
            real_balance = await self._fetch_live_balance()
            if real_balance is not None:
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

            self._initialized = True
            print(f"✅ Live trader initialized successfully", flush=True)
            return True

        except ImportError as e:
            print(f"❌ py-clob-client not installed: {e}", flush=True)
            print(f"  Run: pip install py-clob-client>=0.18.0", flush=True)
            return False
        except Exception as e:
            print(f"❌ CLOB init error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return False

    @property
    def is_ready(self) -> bool:
        return self._initialized and self.clob_client is not None

    async def fetch_balance(self) -> float:
        """
        Fetch real USDC balance from Polymarket.
        Returns balance as float, or None if unavailable.
        Uses multiple fallback methods.
        """
        if not self.is_ready:
            return None

        # Method 1: CLOB get_balance_allowance (no args)
        try:
            bal_resp = self.clob_client.get_balance_allowance()
            if bal_resp:
                balance = float(bal_resp.get('balance', 0))
                # CLOB returns balance in atomic units (6 decimals for USDC)
                if balance > 1_000_000:
                    balance = balance / 1e6
                print(f"💰 Polymarket balance: ${balance:.6f}", flush=True)
                return round(balance, 2)
        except Exception as e:
            print(f"⚠️ CLOB balance failed: {e}", flush=True)

        # Method 2: On-chain USDC balance via Polygon RPC
        try:
            import requests
            from config import Config

            # Derive wallet address from private key
            from eth_account import Account
            wallet = Account.from_key(Config.POLY_PRIVATE_KEY)
            address = wallet.address

            # USDC contract on Polygon (PoS bridged)
            usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            # balanceOf(address) selector = 0x70a08231
            padded_addr = address[2:].lower().zfill(64)
            call_data = f"0x70a08231{padded_addr}"

            resp = requests.post(
                "https://polygon-rpc.com",
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": usdc_contract, "data": call_data}, "latest"],
                    "id": 1,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                result = resp.json().get("result", "0x0")
                balance_wei = int(result, 16)
                balance = balance_wei / 1e6  # USDC has 6 decimals
                print(f"💰 On-chain USDC: ${balance:.6f}", flush=True)
                return round(balance, 2)
        except Exception as e:
            print(f"⚠️ On-chain balance failed: {e}", flush=True)

        # Method 3: Try USDCe (native USDC on Polygon)
        try:
            import requests
            from config import Config
            from eth_account import Account

            wallet = Account.from_key(Config.POLY_PRIVATE_KEY)
            address = wallet.address

            # Native USDC on Polygon
            usdce_contract = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            padded_addr = address[2:].lower().zfill(64)
            call_data = f"0x70a08231{padded_addr}"

            resp = requests.post(
                "https://polygon-rpc.com",
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": usdce_contract, "data": call_data}, "latest"],
                    "id": 1,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                result = resp.json().get("result", "0x0")
                balance_wei = int(result, 16)
                balance = balance_wei / 1e6
                if balance > 0:
                    print(f"💰 On-chain USDCe: ${balance:.6f}", flush=True)
                    return round(balance, 2)
        except Exception as e:
            print(f"⚠️ USDCe balance failed: {e}", flush=True)

        return None

    async def execute_signal(self, signal: TradeSignal) -> Optional[Dict]:
        """Execute a trade signal by placing a LIMIT order on the CLOB."""
        if not self.is_ready:
            print("⚠️ LiveTrader not initialized", flush=True)
            return None

        # Check if trading is paused due to repeated failures
        if self._trading_paused:
            return None

        can_trade, reason = self.balance_mgr.can_trade()
        if not can_trade:
            return None

        size = self.balance_mgr.get_position_size(signal.confidence)
        if size < Config.POLYMARKET_MIN_ORDER_SIZE:
            return None

        # For BOTH-side strategies (arb, straddle), execute both legs
        if signal.direction == 'BOTH' and '|' in signal.token_id:
            return await self._execute_both_sides(signal, size)

        return await self._place_limit_buy(signal, size)

    async def _execute_both_sides(self, signal: TradeSignal, total_size: float) -> Optional[Dict]:
        """Execute a dual-leg trade (arb, straddle).
        
        Uses per-leg prices from strategy metadata when available.
        Falls back to splitting entry_price for simple straddles.
        """
        tokens = signal.token_id.split('|')
        if len(tokens) != 2:
            print(f"❌ BOTH-side: expected 2 tokens, got {len(tokens)}", flush=True)
            return None

        meta = signal.metadata or {}
        half_size = max(Config.POLYMARKET_MIN_ORDER_SIZE, total_size / 2)

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
            result = await self._place_limit_buy(sub_signal, half_size)
            if result:
                results.append(result)
            else:
                # If first leg succeeds but second fails, cancel the first
                if results:
                    first = results[0]
                    try:
                        self.clob_client.cancel(first.get('order_id', ''))
                        print(f"⚠️ Leg 2 failed, cancelled leg 1: {first['order_id']}", flush=True)
                        self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
                        self.balance_mgr.update_balance(self.balance_mgr.balance + first['size_usd'])
                    except Exception:
                        pass
                    return None

        if results:
            print(f"✅ Both legs placed for {signal.strategy}: {signal.coin}", flush=True)
        return results[0] if results else None

    def _get_dynamic_fee_rate(self, price: float) -> float:
        """Calculate per-trade fee rate based on probability (price).
        
        Polymarket's dynamic taker fees are highest near 50% and lowest
        near the extremes (0% / 100%). The fee curve is approximately:
            fee = BASE_FEE * 4 * price * (1 - price)
        This peaks at price=0.50 and drops to ~0 at price=0.01 or 0.99.
        
        For 5m/15m crypto markets: BASE_FEE ~= 1.56% max at 50%.
        """
        # Fee curve: peaks at p=0.50, zero at p=0 and p=1
        probability_factor = 4.0 * price * (1.0 - price)  # 0.0 at edges, 1.0 at 50%
        fee = self.BASE_TAKER_FEE_RATE * probability_factor
        return max(0.0, min(self.BASE_TAKER_FEE_RATE, fee))

    async def _place_limit_buy(self, signal: TradeSignal, size: float) -> Optional[Dict]:
        """Place a limit buy order on the CLOB."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            # Tick-align price to 0.01 increments (Polymarket requirement)
            price = max(0.01, min(0.99, round(signal.entry_price * 100) / 100))

            # Calculate per-trade dynamic fee based on probability
            trade_fee = self._get_dynamic_fee_rate(price)
            self.TAKER_FEE_RATE = trade_fee  # Update for PnL calcs

            shares = round(size / price, 2)
            if shares < 1:
                return None  # Minimum ~1 share

            trade_id = str(uuid.uuid4())[:8]
            now = datetime.now().isoformat()

            print(f"\ud83d\udce4 PLACING ORDER: {signal.coin} {signal.direction} | "
                  f"${size:.2f} @ ${price:.3f} ({shares:.1f} shares) "
                  f"[fee~{trade_fee*100:.2f}%]", flush=True)

            order_args = OrderArgs(
                price=price,
                size=shares,
                side=BUY,
                token_id=signal.token_id,
            )

            signed_order = self.clob_client.create_order(order_args)
            resp = self.clob_client.post_order(signed_order, OrderType.GTC)

            if not resp or resp.get('status') == 'error':
                error_msg = resp.get('errorMsg', 'Unknown error') if resp else 'No response'
                print(f"❌ Order rejected: {error_msg}", flush=True)
                return None

            order_id = resp.get('orderID', resp.get('id', trade_id))
            print(f"✅ ORDER PLACED: {order_id}", flush=True)

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
                'size_usd': size,
                'shares': shares,
                'pnl': None,
                'pnl_pct': None,
                'confidence': signal.confidence,
                'entry_time': now,
                'exit_time': None,
                'exit_reason': None,
                'status': 'pending',
                'rationale': signal.rationale,
                'metadata': signal.metadata,
                'placed_at': time.time(),
                'fee_rate': self.TAKER_FEE_RATE,
                '_live': True,
            }

            self.pending_orders[trade_id] = trade
            self.balance_mgr.open_positions += 1
            self.balance_mgr.update_balance(self.balance_mgr.balance - size)

            await self.db.save_trade(trade)
            self._consecutive_failures = 0  # Reset on success
            return trade

        except Exception as e:
            error_str = str(e).lower()
            print(f"❌ Order error: {e}", flush=True)

            # Detect balance/allowance errors and stop spamming
            if 'balance' in error_str or 'allowance' in error_str:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 5:
                    self._trading_paused = True
                    self._pause_reason = 'Not enough balance/allowance'
                    print(f"\n{'='*60}", flush=True)
                    print(f"🛑 TRADING PAUSED: {self._consecutive_failures} consecutive balance errors", flush=True)
                    print(f"  Deposit USDC at https://polymarket.com", flush=True)
                    print(f"  Then restart the bot.", flush=True)
                    print(f"{'='*60}\n", flush=True)

            return None

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

    async def check_positions(self, current_prices: Dict[str, float],
                                seconds_remaining_map: Dict[str, int] = None) -> List[Dict]:
        """Check open positions for exit signals."""
        closed = []
        seconds_remaining_map = seconds_remaining_map or {}

        await self.check_pending_orders()

        for trade_id, pos in list(self.positions.items()):
            token_id = pos['token_id']
            current_price = current_prices.get(token_id)
            if current_price is None:
                continue

            secs = seconds_remaining_map.get(pos.get('market_id', ''), 999)

            decision = self._exit_decision(pos['entry_price'], current_price, secs)

            if decision in ('sell', 'cut_loss'):
                pnl = (current_price - pos['entry_price']) * pos.get('shares', 0)
                reason = 'profit_take' if decision == 'sell' else 'stop_loss'
                result = await self._close_position(trade_id, current_price, pnl, reason)
                if result:
                    closed.append(pos)

        return closed

    def _exit_decision(self, entry_price: float, current_price: float,
                       seconds_remaining: int) -> str:
        """Exit decision based on current risk mode. Fee-aware."""
        if entry_price <= 0:
            return 'hold'

        # Fee-aware gain calculation: subtract taker fees from both legs
        fee_cost = self.TAKER_FEE_RATE * 2  # entry + exit fee
        raw_gain = current_price / entry_price
        net_gain = (current_price * (1 - self.TAKER_FEE_RATE)) / (entry_price * (1 + self.TAKER_FEE_RATE))
        pnl_pct = (net_gain - 1) * 100

        mode = self.balance_mgr.mode_name

        if mode == 'seed':
            # SEED MODE: Very conservative — protect capital at all costs
            if net_gain >= 1.4:  # Take profit at 40% net gain
                return 'sell'
            if net_gain >= 1.15 and seconds_remaining < 20:
                return 'sell'
            if pnl_pct <= -12:  # Tight stop loss (account for fees)
                return 'cut_loss'
        elif mode == 'concentration':
            if net_gain >= 1.8:
                return 'sell'
            if net_gain >= 1.4 and seconds_remaining < 30:
                return 'sell'
            if pnl_pct <= -18:
                return 'cut_loss'
        elif mode == 'medium':
            if net_gain >= 2.5:
                return 'sell'
            if net_gain >= 1.8 and seconds_remaining < 45:
                return 'sell'
            if pnl_pct <= -22:
                return 'cut_loss'
        else:  # aggressive
            if net_gain >= 4.0:
                return 'sell'
            if net_gain >= 2.5 and seconds_remaining < 60:
                return 'sell'
            if pnl_pct <= -28:
                return 'cut_loss'

        # Near expiry: if we're in profit net of fees, take it
        if seconds_remaining < 15 and pnl_pct > 5:
            return 'sell'

        # Hold penny bets through expiry (let them settle)
        if seconds_remaining < 15 and entry_price < 0.15:
            return 'hold'

        return 'hold'

    async def _close_position(self, trade_id: str, exit_price: float,
                              pnl: float, reason: str) -> bool:
        """Close a position by placing a sell order.
        
        Uses GTC limit sell at current price (acts as aggressive limit order).
        Falls back to a slightly lower price if first attempt fails.
        """
        pos = self.positions.get(trade_id)
        if not pos:
            return False

        shares = pos.get('shares', 0)
        if shares <= 0:
            return False

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            # Tick-align exit price
            sell_price = max(0.01, min(0.99, round(exit_price * 100) / 100))

            print(f"📤 SELL ORDER: {pos['coin']} {pos['direction']} | "
                  f"{shares:.1f} shares @ ${sell_price:.3f} [{reason}]", flush=True)

            # Attempt 1: Limit sell at current price (GTC)
            sell_args = OrderArgs(
                price=sell_price,
                size=shares,
                side=SELL,
                token_id=pos['token_id'],
            )
            signed_order = self.clob_client.create_order(sell_args)
            resp = self.clob_client.post_order(signed_order, OrderType.GTC)

            if resp and resp.get('status') != 'error':
                self._finalize_close(trade_id, exit_price, pnl, reason)
                return True

            # Attempt 2: Sell slightly cheaper to ensure fill
            print(f"⚠️ Sell attempt 1 failed, trying 1¢ lower", flush=True)
            retry_price = max(0.01, sell_price - 0.01)
            sell_args_retry = OrderArgs(
                price=retry_price,
                size=shares,
                side=SELL,
                token_id=pos['token_id'],
            )
            signed_retry = self.clob_client.create_order(sell_args_retry)
            resp2 = self.clob_client.post_order(signed_retry, OrderType.GTC)

            if resp2 and resp2.get('status') != 'error':
                # Adjust PnL for lower fill price
                adj_pnl = (retry_price - pos['entry_price']) * shares
                self._finalize_close(trade_id, retry_price, adj_pnl, reason)
                return True

            error_msg = resp2.get('errorMsg', 'Unknown') if resp2 else 'No response'
            print(f"❌ Both sell attempts failed: {error_msg}", flush=True)

        except Exception as e:
            print(f"❌ Sell error: {e}", flush=True)

        return False

    def _finalize_close(self, trade_id: str, exit_price: float,
                        pnl: float, reason: str):
        """Finalize a closed position. Fee-aware PnL."""
        pos = self.positions.pop(trade_id, None)
        if not pos:
            return

        # Subtract estimated taker fees from PnL (entry + exit)
        fee_rate = pos.get('fee_rate', self.TAKER_FEE_RATE)
        entry_fee = pos['entry_price'] * pos.get('shares', 0) * fee_rate
        exit_fee = exit_price * pos.get('shares', 0) * fee_rate
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
        self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
        self.balance_mgr.update_balance(self.balance_mgr.balance + pos['size_usd'] + net_pnl)

        gain = exit_price / pos['entry_price'] if pos['entry_price'] > 0 else 0
        emoji = '🤑' if net_pnl > 0 else '💸'
        print(f"{emoji} LIVE CLOSED: {pos['coin']} {pos['direction']} — "
              f"Entry:${pos['entry_price']:.3f} -> Exit:${exit_price:.3f} | "
              f"Gross:${pnl:+.2f} Fees:${total_fees:.2f} Net:${net_pnl:+.2f} "
              f"({gain:.1f}x) [{reason}] | "
              f"Bal:${self.balance_mgr.balance:.2f}", flush=True)

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
