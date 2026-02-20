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
    TAKER_FEE_RATE = 0.0156  # ~1.56% dynamic taker fee on 5m/15m crypto markets

    def __init__(self, db: Database, balance_mgr: LiveBalanceManager):
        self.db = db
        self.balance_mgr = balance_mgr
        self.positions: Dict[str, Dict] = {}
        self.pending_orders: Dict[str, Dict] = {}
        self.trade_history: List[Dict] = []
        self.clob_client = None
        self._initialized = False

    async def init(self):
        """Initialize CLOB client with credentials."""
        if not Config.POLY_PRIVATE_KEY:
            print("⚠️ No POLY_PRIVATE_KEY — live trading disabled", flush=True)
            return False

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            host = Config.CLOB_API_URL
            funder = Config.POLY_FUNDER_ADDRESS or None

            creds = None
            if Config.POLY_API_KEY:
                creds = ApiCreds(
                    api_key=Config.POLY_API_KEY,
                    api_secret=Config.POLY_API_SECRET,
                    api_passphrase=Config.POLY_PASSPHRASE,
                )

            self.clob_client = ClobClient(
                host,
                key=Config.POLY_PRIVATE_KEY,
                chain_id=Config.POLY_CHAIN_ID,
                signature_type=Config.POLY_SIGNATURE_TYPE,
                funder=funder,
            )

            if creds:
                self.clob_client.set_api_creds(creds)
            else:
                derived = self.clob_client.create_or_derive_api_creds()
                self.clob_client.set_api_creds(derived)
                print(f"🔑 Derived CLOB API credentials", flush=True)

            ok = self.clob_client.get_ok()
            print(f"🟢 CLOB connection: {ok}", flush=True)

            # Fetch dynamic fee rate for 5m/15m markets
            try:
                import requests
                resp = requests.get(
                    f"{host}/fees",
                    timeout=5,
                )
                if resp.status_code == 200:
                    fee_data = resp.json()
                    fee_rate = float(fee_data.get('taker', fee_data.get('fee_rate', self.TAKER_FEE_RATE)))
                    self.TAKER_FEE_RATE = fee_rate
                    print(f"💰 Dynamic taker fee rate: {fee_rate:.4f} ({fee_rate*100:.2f}%)", flush=True)
            except Exception as e:
                print(f"⚠️ Fee rate fetch failed, using default {self.TAKER_FEE_RATE:.4f}: {e}", flush=True)

            self._initialized = True
            return True

        except ImportError:
            print("❌ py-clob-client not installed. Run: pip install py-clob-client", flush=True)
            return False
        except Exception as e:
            print(f"❌ CLOB init error: {e}", flush=True)
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

        can_trade, reason = self.balance_mgr.can_trade()
        if not can_trade:
            return None

        size = self.balance_mgr.get_position_size(signal.confidence)
        if size < Config.POLYMARKET_MIN_ORDER_SIZE:
            return None

        # For straddle (BOTH), split size
        if signal.direction == 'BOTH' and '|' in signal.token_id:
            tokens = signal.token_id.split('|')
            half_size = max(Config.POLYMARKET_MIN_ORDER_SIZE, size / 2)
            results = []
            for i, tid in enumerate(tokens):
                side_name = 'UP' if i == 0 else 'DOWN'
                sub_signal = TradeSignal(
                    strategy=signal.strategy,
                    coin=signal.coin,
                    timeframe=signal.timeframe,
                    direction=side_name,
                    token_id=tid,
                    market_id=signal.market_id,
                    entry_price=signal.entry_price / 2,
                    confidence=signal.confidence,
                    rationale=signal.rationale,
                    metadata=signal.metadata,
                )
                result = await self._place_limit_buy(sub_signal, half_size)
                if result:
                    results.append(result)
            return results[0] if results else None

        return await self._place_limit_buy(signal, size)

    async def _place_limit_buy(self, signal: TradeSignal, size: float) -> Optional[Dict]:
        """Place a limit buy order on the CLOB."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            # Tick-align price to 0.01 increments (Polymarket requirement)
            price = max(0.01, min(0.99, round(signal.entry_price * 100) / 100))

            shares = round(size / price, 2)
            if shares < 1:
                return None  # Minimum ~1 share

            trade_id = str(uuid.uuid4())[:8]
            now = datetime.now().isoformat()

            print(f"📤 PLACING ORDER: {signal.coin} {signal.direction} | "
                  f"${size:.2f} @ ${price:.3f} ({shares:.1f} shares) "
                  f"[fee~{self.TAKER_FEE_RATE*100:.2f}%]", flush=True)

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
            return trade

        except Exception as e:
            print(f"❌ Order error: {e}", flush=True)
            return None

    async def check_pending_orders(self):
        """Check if pending orders have been filled and cancel stale ones."""
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
                        order['status'] = 'open'
                        fill_price = float(clob_order.get('price', order['entry_price']))
                        order['entry_price'] = fill_price
                        self.positions[trade_id] = order
                        to_remove.append(trade_id)
                        print(f"🟢 FILLED: {order['coin']} {order['direction']} "
                              f"@ ${fill_price:.3f}", flush=True)
                        continue
                    elif status == 'cancelled':
                        to_remove.append(trade_id)
                        self.balance_mgr.open_positions = max(0, self.balance_mgr.open_positions - 1)
                        self.balance_mgr.update_balance(
                            self.balance_mgr.balance + order['size_usd']
                        )
                        continue
            except Exception:
                pass

            # Timeout
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
