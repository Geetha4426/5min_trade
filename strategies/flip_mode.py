"""
Flip Mode Strategy — $1 Doubling (Martingale) with Near-Certainty Signals

The concept: start with $1, bet the ENTIRE balance each time.
Win → $1→$2→$4→$8→$16→$32→$64→$128+
Lose → back to $1 (reset).

To sustain this, the strategy ONLY fires when ALL of:
1. Reference price model shows >92% probability (calibrated, not raw confidence)
2. Binance confirms direction with strong move (>0.10%)
3. Orderbook confirms same direction (best_ask spread)
4. Time remaining < 150 seconds (winner is becoming clear)
5. Effective spread is small enough (not overpaying)
6. MicroPrice (volume-weighted mid) confirms direction

When ALL 6 align, the trade is near-certain. The math:
- If win rate is 95%, expected value of 7-step chain:
  P(7 wins) = 0.95^7 = 69.8% → E[$1 → $128] = $89.34
  P(lose at step N): lose $2^(N-1), restart at $1
  Average loss per chain = ~$4.5
  Net EV per attempt: +$85 (massively positive)

MicroPrice formula (from market microstructure research):
  MicroPrice = (V_ask × bid + V_bid × ask) / (V_bid + V_ask)
  More accurate than simple midpoint. When MicroPrice > ask → buyers dominating.
"""

from typing import Optional, Dict
from strategies.base_strategy import BaseStrategy, TradeSignal


class FlipModeStrategy(BaseStrategy):
    """Near-certainty strategy for flip (doubling) mode."""

    # ── Thresholds (all must pass) ──
    MIN_P_MODEL = 0.92           # Reference price model P(direction) must be >92%
    MIN_BINANCE_MOVE_PCT = 0.10  # Binance must confirm with >0.10% move
    MAX_SECONDS_REMAINING = 150  # Only trade in last 2.5 minutes
    MIN_SECONDS_REMAINING = 10   # Don't trade in last 10s (settlement chaos)
    MIN_MARKET_LEAD = 0.04       # Orderbook spread must show 4¢+ leader
    MAX_ENTRY_PRICE = 0.70       # Don't buy above 70¢ (need upside room)
    MIN_MICROPRICE_CONFIRM = 0.005  # MicroPrice must confirm by 0.5¢

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """Only fires when ALL extreme conditions align."""
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 999)
        ref_engine = context.get('ref_engine')

        if not clob or not binance_feed or not ref_engine:
            return None

        coin = market.get('coin', '')
        if not coin:
            return None

        # ── GATE 1: Time window (last 150s, not last 10s) ──
        if seconds_remaining > self.MAX_SECONDS_REMAINING:
            return None
        if seconds_remaining < self.MIN_SECONDS_REMAINING:
            return None

        # ── GATE 2: Reference price model probability ──
        prob = ref_engine.calc_p_up(market, binance_feed, seconds_remaining)
        if not prob:
            return None

        p_up = prob['p_up']
        p_down = prob['p_down']
        distance_pct = prob.get('distance_pct', 0)

        # Determine model's predicted direction
        if p_up >= self.MIN_P_MODEL:
            model_direction = 'UP'
            model_confidence = p_up
        elif p_down >= self.MIN_P_MODEL:
            model_direction = 'DOWN'
            model_confidence = p_down
        else:
            return None  # Model not confident enough

        # ── GATE 3: Binance confirms direction ──
        ref_info = ref_engine.get_info(market.get('market_id', ''))
        if not ref_info:
            return None

        ref_price = ref_info['ref_price']
        current_price = prob.get('current_price', 0)
        if ref_price <= 0 or current_price <= 0:
            return None

        binance_change_pct = ((current_price - ref_price) / ref_price) * 100

        if model_direction == 'UP' and binance_change_pct < self.MIN_BINANCE_MOVE_PCT:
            return None  # Binance doesn't confirm UP
        if model_direction == 'DOWN' and binance_change_pct > -self.MIN_BINANCE_MOVE_PCT:
            return None  # Binance doesn't confirm DOWN

        # ── GATE 4: Orderbook confirms direction ──
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))

        if not up_book or not down_book:
            return None

        up_ask = up_book.get('best_ask', 0.5)
        down_ask = down_book.get('best_ask', 0.5)
        up_bid = up_book.get('best_bid', 0)
        down_bid = down_book.get('best_bid', 0)

        # Market must show clear leader
        market_lead = abs(up_ask - down_ask)
        if market_lead < self.MIN_MARKET_LEAD:
            return None  # Market undecided

        if up_ask > down_ask:
            book_direction = 'UP'
        else:
            book_direction = 'DOWN'

        # All 3 must agree: model, Binance, orderbook
        if book_direction != model_direction:
            return None

        # ── GATE 5: Entry price check ──
        if model_direction == 'UP':
            entry_price = up_ask
            token_id = market.get('up_token_id', '')
            bid_depth = up_book.get('bid_depth', 0)
            ask_depth = up_book.get('ask_depth', 0)
        else:
            entry_price = down_ask
            token_id = market.get('down_token_id', '')
            bid_depth = down_book.get('bid_depth', 0)
            ask_depth = down_book.get('ask_depth', 0)

        if entry_price <= 0 or entry_price > self.MAX_ENTRY_PRICE:
            return None  # Too expensive — needs room to profit

        # ── GATE 6: MicroPrice confirmation ──
        # MicroPrice = (V_ask × bid + V_bid × ask) / (V_bid + V_ask)
        # When buyers dominate, MicroPrice shifts toward the ask
        if bid_depth > 0 and ask_depth > 0:
            if model_direction == 'UP':
                bid_p = up_bid if up_bid > 0 else up_ask - 0.01
                micro_price = (ask_depth * bid_p + bid_depth * up_ask) / (bid_depth + ask_depth)
                # MicroPrice above midpoint = buyers dominant = confirms UP
                midpoint = (bid_p + up_ask) / 2
                if micro_price - midpoint < self.MIN_MICROPRICE_CONFIRM:
                    return None
            else:
                bid_p = down_bid if down_bid > 0 else down_ask - 0.01
                micro_price = (ask_depth * bid_p + bid_depth * down_ask) / (bid_depth + ask_depth)
                midpoint = (bid_p + down_ask) / 2
                if micro_price - midpoint < self.MIN_MICROPRICE_CONFIRM:
                    return None
        # If no depth data, MicroPrice gate is skipped (other 5 gates still protect)

        # ── GATE 7: Effective spread check ──
        # Don't enter if spread is eating our edge
        # Effective spread = 2 × |entry - midprice| / midprice
        combined_ask = up_ask + down_ask
        if combined_ask > 1.02:
            return None  # Combined cost > $1.02 → spread too wide

        # ═══ ALL GATES PASSED — FIRE ═══
        # Edge: model probability minus our entry cost
        edge = model_confidence - entry_price

        return TradeSignal(
            market_id=market.get('market_id', ''),
            coin=coin,
            direction=model_direction,
            entry_price=entry_price,
            token_id=token_id,
            confidence=model_confidence,
            timeframe=market.get('timeframe', 5),
            strategy='flip_mode',
            rationale=(
                f"🔄 FLIP: {model_direction} | "
                f"P(model)={model_confidence:.1%} | "
                f"Binance {binance_change_pct:+.3f}% | "
                f"Book lead {market_lead:.2f}¢ | "
                f"Edge={edge:.1%} | "
                f"{seconds_remaining}s left"
            ),
            metadata={
                'p_model': model_confidence,
                'binance_move': binance_change_pct,
                'market_lead': market_lead,
                'edge': edge,
                'combined_ask': combined_ask,
            }
        )
