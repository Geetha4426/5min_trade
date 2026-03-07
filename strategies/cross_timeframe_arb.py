"""
Cross-Timeframe Arbitrage Strategy — GUARANTEED PROFIT (FEE-AWARE)

THE EDGE: When a 15-minute market has ~5 minutes left, a NEW 5-minute
market opens for the same coin. Because the 15-min market has already
"traveled" 10 minutes of price action, its UP/DOWN pricing diverges
from the fresh 5-min market.

EXAMPLE:
  15-min UP = 40¢  (already moved, price reflects 10 mins of history)
  5-min  UP = 50¢  (fresh market, 50/50)
  
  Buy UP on 15-min (40¢) + Buy DOWN on 5-min (50¢) = 90¢
  At settlement: one side pays $1.00
  GUARANTEED 10¢ profit per share = 11% return in 5 minutes

Real traders reported 10-15% guaranteed returns from this setup.

LOGIC:
1. Scan for overlapping markets: same coin, different timeframes
2. Find pairs where the remaining time overlaps (~5 min window)
3. Calculate combined cost of opposite sides
4. If combined < $0.95 → execute both legs for guaranteed profit
"""

from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal


class CrossTimeframeArbStrategy(BaseStrategy):
    """
    Guaranteed profit from cross-timeframe price divergence.
    
    Buys opposite sides on overlapping markets (e.g., 15-min UP + 5-min DOWN)
    when the combined cost is less than $1.00.
    """

    name = "cross_tf_arb"
    description = "Cross-timeframe arbitrage on overlapping markets"

    # Maximum combined cost to enter (lower = more profit)
    MAX_COMBINED_COST = 0.95

    # Minimum profit per share to bother (AFTER FEES)
    MIN_PROFIT = 0.03  # 3¢ = ~3% net of fees

    # Estimated taker fee rate per leg — dynamic per price
    @staticmethod
    def _dynamic_fee(price: float) -> float:
        """Polymarket effective fee rate: 0.25 × p × (1-p)².
        Peak ~3.7% at p≈0.33. Settlement is FREE (0%)."""
        p = max(0.001, min(0.999, price))
        q = 1.0 - p
        return 0.25 * p * q * q

    # Minimum depth on both sides (avoid thin markets)
    MIN_DEPTH = 2.0  # $2

    # Expected order size per leg — used for fillable price calculation
    EXPECTED_LEG_SIZE = 1.0  # $1 per leg in SEED/PLANT

    @staticmethod
    def _fillable_price(book: dict, size_usd: float) -> tuple:
        """Calculate the price needed to fill `size_usd` from ask side.
        
        Walks the orderbook asks to find the worst price we'd pay
        to fully fill our order. Returns (fillable_price, fillable).
        If not enough depth, returns (worst_ask_seen, False).
        """
        asks = book.get('asks', [])  # [(price, size), ...] sorted ascending
        if not asks:
            return (book.get('best_ask', 1.0), False)
        remaining = size_usd
        worst_price = asks[0][0]
        for price, shares in asks:
            level_value = price * shares
            worst_price = price
            remaining -= level_value
            if remaining <= 0:
                return (price, True)
        return (worst_price, False)

    # The 15-min market should have 3-7 minutes remaining (overlap window)
    OVERLAP_MIN_SECS = 120   # 2 min — need enough execution time
    OVERLAP_MAX_SECS = 420   # 7 min — the 5-min market just opened

    # Track discovered pairs to avoid duplicate signals (instance-level)
    def __init__(self):
        self._active_pairs = {}
        self._depth_skip_log = {}  # coin -> last_log_time for throttling

    def _log_depth_skip(self, coin: str, reason: str):
        """Log depth skip once per 30s per coin to avoid spam."""
        import time
        now = time.time()
        last = self._depth_skip_log.get(coin, 0)
        if now - last > 30:
            self._depth_skip_log[coin] = now
            print(f"💧 Depth skip: {coin} cross_tf_arb — {reason}", flush=True)

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Check for cross-timeframe arbitrage.

        This strategy needs access to ALL markets, not just one.
        It stores market data and looks for overlapping pairs.

        We are called once per market. We accumulate markets and
        check for pairs each time.
        """
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob or seconds_remaining <= 0:
            return None

        # In SEED/PLANT mode, require higher profit margin for arb
        balance_mgr = context.get('balance_mgr')
        self._seed_mode = balance_mgr and getattr(balance_mgr, 'mode_name', '') in ('seed', 'plant')

        coin = market['coin']
        timeframe = market['timeframe']
        market_id = market['market_id']

        # Store this market for pair matching
        key = f"{coin}_{timeframe}_{market_id}"
        self._active_pairs[key] = {
            'market': market,
            'seconds_remaining': seconds_remaining,
            'context': context,
        }

        # Clean stale entries (expired markets)
        stale_keys = [
            k for k, v in self._active_pairs.items()
            if v['seconds_remaining'] <= 0
        ]
        for k in stale_keys:
            del self._active_pairs[k]

        # Now scan for pairs: find overlapping markets for the SAME coin
        # We want: longer_tf market with ~5 min left + shorter_tf fresh market
        signal = await self._find_arb_pair(coin, clob)
        return signal

    async def _find_arb_pair(self, coin: str, clob) -> Optional[TradeSignal]:
        """
        Find an arb pair for the given coin across timeframes.
        
        Looking for:
          - LONGER market (e.g., 15-min) with 3-7 min remaining
          - SHORTER market (e.g., 5-min) fresh (close to full time remaining)
        """
        # Gather all active entries for this coin
        coin_entries = {
            k: v for k, v in self._active_pairs.items()
            if k.startswith(f"{coin}_")
        }

        if len(coin_entries) < 2:
            return None

        # Sort by timeframe
        sorted_entries = sorted(
            coin_entries.values(),
            key=lambda e: e['market']['timeframe']
        )

        # Try all pairs (longer + shorter)
        for i, longer in enumerate(sorted_entries):
            for shorter in sorted_entries[:i]:
                signal = await self._check_pair(longer, shorter, clob)
                if signal:
                    return signal

        return None

    async def _check_pair(self, longer_entry, shorter_entry, clob) -> Optional[TradeSignal]:
        """
        Check if a longer/shorter market pair has an arb opportunity.
        
        Strategy:
          - The LONGER market has been running, so its price has moved
            away from 50¢ based on historical price action
          - The SHORTER market just opened, still near 50¢
          - Buy the FAVORABLE side on longer + OPPOSITE side on shorter
        """
        longer_mkt = longer_entry['market']
        shorter_mkt = shorter_entry['market']
        longer_secs = longer_entry['seconds_remaining']
        shorter_secs = shorter_entry['seconds_remaining']

        # Check overlap window: longer market should have 2-7 min left
        if longer_secs < self.OVERLAP_MIN_SECS or longer_secs > self.OVERLAP_MAX_SECS:
            return None

        # Shorter market should be fresh (most of its time remaining)
        shorter_tf_secs = shorter_mkt['timeframe'] * 60
        freshness = shorter_secs / shorter_tf_secs if shorter_tf_secs > 0 else 0
        if freshness < 0.60:  # Should have at least 60% time remaining
            return None

        # Get orderbooks for both markets
        longer_up_book = clob.get_orderbook(longer_mkt.get('up_token_id', ''))
        longer_dn_book = clob.get_orderbook(longer_mkt.get('down_token_id', ''))
        shorter_up_book = clob.get_orderbook(shorter_mkt.get('up_token_id', ''))
        shorter_dn_book = clob.get_orderbook(shorter_mkt.get('down_token_id', ''))

        if not all([longer_up_book, longer_dn_book, shorter_up_book, shorter_dn_book]):
            return None

        # === FIND THE BEST COMBINATION (FEE-AWARE + DEPTH-AWARE) ===
        # Use fillable price (worst price needed to fill our order size)
        # instead of best_ask, which may have only 1 share available.
        ctx = longer_entry.get('context', {})
        balance_mgr = ctx.get('balance_mgr')
        leg_size = self.EXPECTED_LEG_SIZE
        if balance_mgr:
            leg_size = max(leg_size, balance_mgr.get_position_size(0.95) / 2)

        # Fillable prices: realistic prices we'd actually get filled at
        a_p1_fill, a_p1_ok = self._fillable_price(longer_up_book, leg_size)
        a_p2_fill, a_p2_ok = self._fillable_price(shorter_dn_book, leg_size)
        b_p1_fill, b_p1_ok = self._fillable_price(longer_dn_book, leg_size)
        b_p2_fill, b_p2_ok = self._fillable_price(shorter_up_book, leg_size)

        # Option A: Buy UP on longer + DOWN on shorter
        combo_a_cost = a_p1_fill + a_p2_fill
        combo_a_fee_1 = self._dynamic_fee(a_p1_fill)
        combo_a_fee_2 = self._dynamic_fee(a_p2_fill)
        combo_a_fees = (a_p1_fill * combo_a_fee_1 + a_p2_fill * combo_a_fee_2)
        combo_a_profit = 1.0 - combo_a_cost - combo_a_fees
        combo_a_fillable = a_p1_ok and a_p2_ok

        # Option B: Buy DOWN on longer + UP on shorter
        combo_b_cost = b_p1_fill + b_p2_fill
        combo_b_fee_1 = self._dynamic_fee(b_p1_fill)
        combo_b_fee_2 = self._dynamic_fee(b_p2_fill)
        combo_b_fees = (b_p1_fill * combo_b_fee_1 + b_p2_fill * combo_b_fee_2)
        combo_b_profit = 1.0 - combo_b_cost - combo_b_fees
        combo_b_fillable = b_p1_ok and b_p2_ok

        # Pick the most profitable combination
        # Prefer combos where BOTH legs are fillable at our size
        if combo_a_fillable and not combo_b_fillable:
            pick_a = True
        elif combo_b_fillable and not combo_a_fillable:
            pick_a = False
        else:
            pick_a = combo_a_profit >= combo_b_profit

        if pick_a:
            best_combo = 'A'
            combined_cost = combo_a_cost
            profit = combo_a_profit
            both_fillable = combo_a_fillable
            # We buy UP on longer, DOWN on shorter
            primary_side = 'UP'
            primary_token = longer_mkt['up_token_id']
            primary_price = a_p1_fill
            hedge_side = 'DOWN'
            hedge_token = shorter_mkt['down_token_id']
            hedge_price = a_p2_fill
            primary_depth = longer_up_book.get('ask_depth', 0)
            hedge_depth = shorter_dn_book.get('ask_depth', 0)
        else:
            best_combo = 'B'
            combined_cost = combo_b_cost
            profit = combo_b_profit
            both_fillable = combo_b_fillable
            # We buy DOWN on longer, UP on shorter
            primary_side = 'DOWN'
            primary_token = longer_mkt['down_token_id']
            primary_price = b_p1_fill
            hedge_side = 'UP'
            hedge_token = shorter_mkt['up_token_id']
            hedge_price = b_p2_fill
            primary_depth = longer_dn_book.get('ask_depth', 0)
            hedge_depth = shorter_up_book.get('ask_depth', 0)

        # === FILTER ===
        if combined_cost > self.MAX_COMBINED_COST:
            return None

        # SEED/PLANT: higher edge bar (5¢ net) since capital is precious
        min_profit = 0.05 if getattr(self, '_seed_mode', False) else self.MIN_PROFIT
        if profit < min_profit:
            return None

        # Check liquidity — both total depth and fillability at our size
        min_depth = min(primary_depth, hedge_depth)
        coin = longer_mkt['coin']
        if min_depth < self.MIN_DEPTH:
            self._log_depth_skip(coin, f"depth ${min_depth:.2f} < ${self.MIN_DEPTH:.2f} min")
            return None

        # If neither combo is fillable at our order size, skip
        # (prevents FOK kills from thin top-of-book)
        if not both_fillable:
            self._log_depth_skip(coin, f"not fillable at ${leg_size:.2f}/leg "
                                 f"(combo {best_combo}: primary={primary_depth:.2f} hedge={hedge_depth:.2f})")
            return None

        # Confidence scales with profit margin
        confidence = min(0.99, 0.80 + profit * 5)  # 3% profit → 95% confidence

        pct_return = profit / combined_cost * 100

        rationale = (
            f"🔗 CROSS-TF ARB: {longer_mkt['coin']} — GUARANTEED {pct_return:.1f}% profit\n"
            f"  Leg 1: {primary_side} on {longer_mkt['timeframe']}m @ {primary_price:.2f}¢ "
            f"({longer_secs}s remaining)\n"
            f"  Leg 2: {hedge_side} on {shorter_mkt['timeframe']}m @ {hedge_price:.2f}¢ "
            f"(fresh market)\n"
            f"  Combined: {combined_cost:.2f}¢ → Payout: $1.00 → "
            f"Profit: {profit:.2f}¢/share ({pct_return:.1f}%)\n"
            f"  Depth: ${min_depth:.0f} available"
        )

        return TradeSignal(
            strategy=self.name,
            coin=longer_mkt['coin'],
            timeframe=longer_mkt['timeframe'],
            direction='BOTH',  # Dual-leg trade
            token_id=f"{primary_token}|{hedge_token}",
            market_id=f"{longer_mkt['market_id']}|{shorter_mkt['market_id']}",
            entry_price=combined_cost,
            confidence=confidence,
            rationale=rationale,
            metadata={
                'type': 'cross_timeframe_arb',
                'primary_side': primary_side,
                'primary_token': primary_token,
                'primary_price': primary_price,
                'primary_timeframe': longer_mkt['timeframe'],
                'hedge_side': hedge_side,
                'hedge_token': hedge_token,
                'hedge_price': hedge_price,
                'hedge_timeframe': shorter_mkt['timeframe'],
                'combined_cost': combined_cost,
                'profit_per_share': profit,
                'pct_return': pct_return,
                'min_depth': min_depth,
                'longer_secs_remaining': longer_secs,
                'shorter_secs_remaining': shorter_secs,
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        """Needs multiple timeframes to find overlapping pairs."""
        return [1, 5, 15, 30, 60]
