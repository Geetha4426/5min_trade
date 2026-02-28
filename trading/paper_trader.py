"""
Paper Trader — Aggressive Execution

Small bets, many trades. Dynamic hold/sell decisions.
Rides cheap entries to settlement when it makes sense.
"""

import uuid
import time
from typing import Dict, List, Optional
from datetime import datetime

from config import Config
from trading.risk_manager import RiskManager
from data.database import Database
from strategies.base_strategy import TradeSignal


class PaperTrader:
    """Aggressive paper trading — trades a LOT."""

    def __init__(self, db: Database, risk_mgr: RiskManager):
        self.db = db
        self.risk = risk_mgr
        self.positions: Dict[str, Dict] = {}
        self.trade_history: List[Dict] = []

    async def execute_signal(self, signal: TradeSignal) -> Optional[Dict]:
        """Execute a trade. Fast, minimal checks."""
        allowed, reason = self.risk.can_trade()
        if not allowed:
            return None

        size = self.risk.calculate_position_size(signal.timeframe, signal.confidence)

        # Realistic slippage: can be positive or negative (mostly against you)
        import random
        slippage = random.uniform(-0.005, 0.015)  # -0.5% to +1.5% (skewed against)
        fill_price = signal.entry_price * (1 + slippage)

        shares = size / fill_price if fill_price > 0 else 0

        trade_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        trade = {
            'id': trade_id,
            'market_id': signal.market_id,
            'coin': signal.coin,
            'timeframe': signal.timeframe,
            'strategy': signal.strategy,
            'direction': signal.direction,
            'token_id': signal.token_id,
            'entry_price': fill_price,
            'exit_price': None,
            'size_usd': size,
            'shares': shares,
            'pnl': None,
            'pnl_pct': None,
            'confidence': signal.confidence,
            'entry_time': now,
            'exit_time': None,
            'exit_reason': None,
            'status': 'open',
            'rationale': signal.rationale,
            'metadata': signal.metadata,
        }

        self.positions[trade_id] = trade
        self.risk.open_positions += 1

        await self.db.save_trade(trade)

        potential = 1.0 / fill_price if fill_price > 0 else 0
        print(f"🎰 BOUGHT: {signal.coin} {signal.direction} "
              f"@${fill_price:.3f} x{shares:.0f} shares (${size:.2f}) "
              f"= {potential:.0f}x potential")

        return trade

    async def check_positions(self, current_prices: Dict[str, float],
                                seconds_remaining_map: Dict[str, int] = None) -> List[Dict]:
        """
        Check positions with DYNAMIC hold/sell logic.
        Cheap entries near expiry = ride to settlement.
        """
        closed = []
        seconds_remaining_map = seconds_remaining_map or {}

        for trade_id, pos in list(self.positions.items()):
            token_id = pos['token_id']

            # Handle BOTH direction (arb trades)
            if pos['direction'] == 'BOTH' and '|' in token_id:
                tokens = token_id.split('|')
                # Check if either side has settled
                for t in tokens:
                    price = current_prices.get(t)
                    if price and price >= 0.95:
                        # Settlement! One side won
                        pnl = (1.0 - pos['entry_price']) * (pos.get('shares', 0) / 2)
                        await self._close_position(trade_id, 1.0, pnl, 'arb_settlement')
                        closed.append(pos)
                        break
                continue

            current_price = current_prices.get(token_id)
            if current_price is None:
                continue

            # Get time remaining for this position's market
            secs = seconds_remaining_map.get(pos.get('market_id', ''), 999)

            # Dynamic hold/sell decision
            decision = self.risk.should_hold_or_sell(
                pos['entry_price'], current_price, secs
            )

            pnl = (current_price - pos['entry_price']) * pos.get('shares', 0)

            if decision == 'sell':
                await self._close_position(trade_id, current_price, pnl, 'profit_take')
                closed.append(pos)
            elif decision == 'cut_loss':
                await self._close_position(trade_id, current_price, pnl, 'stop_loss')
                closed.append(pos)
            # If 'hold' — keep riding

        return closed

    async def close_at_settlement(self, token_id: str, final_price: float):
        """Close all positions for a settled market."""
        closed = []
        for trade_id, pos in list(self.positions.items()):
            if pos['token_id'] == token_id or (
                '|' in pos['token_id'] and token_id in pos['token_id'].split('|')
            ):
                pnl = (final_price - pos['entry_price']) * pos.get('shares', 0)
                await self._close_position(trade_id, final_price, pnl, 'settlement')
                closed.append(pos)
        return closed

    async def _close_position(self, trade_id: str, exit_price: float, pnl: float, reason: str):
        pos = self.positions.pop(trade_id, None)
        if not pos:
            return

        # Deduct estimated taker fees (entry + exit) using correct quadratic formula
        entry_p = max(0.001, min(0.999, pos['entry_price']))
        exit_p = max(0.001, min(0.999, exit_price))
        C = 0.0156 / 0.015625  # calibrated for 1.56% at p=0.50
        entry_fee_rate = C * 0.25 * (entry_p * (1 - entry_p)) ** 2
        exit_fee_rate = C * 0.25 * (exit_p * (1 - exit_p)) ** 2
        shares = pos.get('shares', 0)
        entry_fee = pos['entry_price'] * shares * entry_fee_rate
        exit_fee = exit_price * shares * exit_fee_rate
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
        self.risk.open_positions = max(0, self.risk.open_positions - 1)
        self.risk.record_trade_result(net_pnl, net_pnl > 0)

        await self.db.close_trade(trade_id, exit_price, net_pnl, reason)
        await self.db.update_strategy_stats(pos['strategy'], net_pnl > 0, net_pnl)

        gain = exit_price / pos['entry_price'] if pos['entry_price'] > 0 else 0
        emoji = '🤑' if net_pnl > 0 else '💸'
        print(f"{emoji} CLOSED: {pos['coin']} {pos['direction']} — "
              f"Entry:${pos['entry_price']:.3f} -> Exit:${exit_price:.3f} | "
              f"Gross:${pnl:+.2f} Fees:${total_fees:.2f} Net:${net_pnl:+.2f} "
              f"({gain:.1f}x) [{reason}] | "
              f"Bal:${self.risk.balance:.2f}", flush=True)

    def get_open_positions(self) -> List[Dict]:
        return list(self.positions.values())

    def get_summary(self) -> Dict:
        stats = self.risk.get_stats()
        return {
            **stats,
            'open_count': len(self.positions),
            'closed_count': len(self.trade_history),
        }
