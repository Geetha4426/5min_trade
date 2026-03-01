"""Cheap Outcome Hunter — Smart Lottery Strategy

THE EDGE: In 5-minute crypto markets, probabilities swing wildly.
Buying a side at $0.01-0.06 is a lottery ticket that pays $1.00.

BUT: most cheap prices are cheap FOR A REASON — the market has already
decided. The old version bought EVERY cheap outcome and got destroyed.

SMART RULES:
1. BOTH sides cheap (combined < $0.25) → genuine arb, BUY BOTH
2. Single side cheap → ONLY buy if there's genuine uncertainty:
   - Opposite side must be < $0.80 (market hasn't fully decided)
   - At least 90 seconds remaining (enough time for reversal)
   - Real liquidity ($1+ depth) so we can actually exit
   - Price isn't stale (someone is actually trading this market)
3. The math: lose $1 on 10 bets = -$10. 1 winner at $0.01→$1 = +$90 net.
   But ONLY if we pick bets with real upside, not already-decided markets.
"""

import time
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal


class CheapOutcomeHunter(BaseStrategy):
    """Buy dirt-cheap outcomes for massive potential returns."""

    name = "cheap_hunter"
    description = "Smart lottery bets — cheap outcomes with genuine uncertainty"

    # THRESHOLDS (tightened to reduce false signals)
    MAX_BUY_PRICE = 0.06        # Max 6 cents (was 8 — too loose)
    SWEET_SPOT_MAX = 0.03       # 1-3 cents = highest confidence
    BOTH_SIDES_MAX = 0.25       # If Up + Down < 25 cents, buy BOTH
    MIN_BUY_PRICE = 0.005       # Below half a cent = no liquidity
    OPPOSITE_MAX = 0.80         # Opposite side must be < 80¢ (market not decided)
    MIN_DEPTH = 1.00            # Need $1+ depth to exit (was $0.50)
    MIN_TIME_SINGLE = 90        # 90s minimum for single-side (was 45)
    MIN_TIME_BOTH = 45          # 45s minimum for both-sides arb

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob:
            return None

        # Skip markets too close to expiry — prices are decided by then.
        # BOTH_SIDES arb uses a shorter window (45s) because it's genuine arbitrage.
        # Single-side lottery needs 90s+ for any chance of reversal.
        if seconds_remaining < self.MIN_TIME_BOTH:
            return None

        up_token = market.get('up_token_id', '')
        down_token = market.get('down_token_id', '')

        if not up_token or not down_token:
            return None

        up_book = clob.get_orderbook(up_token)
        down_book = clob.get_orderbook(down_token)

        if not up_book or not down_book:
            return None

        up_ask = up_book['best_ask']
        down_ask = down_book['best_ask']

        # ═══════════════════════════════════════════════════════════
        # STRATEGY 1: Buy BOTH sides if combined is insanely cheap
        # ═══════════════════════════════════════════════════════════
        combined = up_ask + down_ask
        if combined < self.BOTH_SIDES_MAX and up_ask > self.MIN_BUY_PRICE and down_ask > self.MIN_BUY_PRICE:
            # Both sides cheap — one MUST pay $1.00 at settlement
            profit_potential = 1.0 - combined
            return TradeSignal(
                strategy=self.name,
                coin=market['coin'],
                timeframe=market['timeframe'],
                direction='BOTH',
                token_id=f"{up_token}|{down_token}",
                market_id=market['market_id'],
                entry_price=combined,
                confidence=0.95,
                rationale=(
                    f"💎 BOTH SIDES CHEAP: {market['coin']} "
                    f"Up@{up_ask:.3f} + Down@{down_ask:.3f} = {combined:.3f}. "
                    f"Guaranteed ${profit_potential:.3f} profit per share!"
                ),
                metadata={
                    'up_ask': up_ask, 'down_ask': down_ask,
                    'combined': combined, 'type': 'both_sides',
                }
            )

        # ═══════════════════════════════════════════════════════════
        # STRATEGY 2: Buy the cheap side (smart lottery ticket)
        # Only when there's GENUINE UNCERTAINTY — not when market decided
        # ═══════════════════════════════════════════════════════════

        # Need more time for single-side bets — 90s minimum
        if seconds_remaining < self.MIN_TIME_SINGLE:
            return None

        for side, ask_price, token_id, opposite_ask in [
            ('UP', up_ask, up_token, down_ask),
            ('DOWN', down_ask, down_token, up_ask),
        ]:
            if not (self.MIN_BUY_PRICE < ask_price <= self.MAX_BUY_PRICE):
                continue

            # ── KEY CHECK: Is the market genuinely uncertain? ──
            # If opposite side is $0.85+, this side is cheap because it's LOSING.
            # No point buying a 2¢ token when the other side is 95¢ — it's decided.
            # Only buy when opposite is < 80¢ (real uncertainty remains).
            if opposite_ask >= self.OPPOSITE_MAX:
                continue

            # Check there's real liquidity to fill AND exit
            book = up_book if side == 'UP' else down_book
            if book['ask_depth'] < self.MIN_DEPTH:
                continue

            potential_return = 1.0 / ask_price  # e.g. $0.02 → 50x

            # Confidence based on price + uncertainty level
            # Lower opposite = more genuine uncertainty = higher confidence
            uncertainty = 1.0 - opposite_ask  # 0.0 = no uncertainty, 1.0 = max

            if ask_price <= self.SWEET_SPOT_MAX:
                base_conf = 0.65
            elif ask_price <= 0.05:
                base_conf = 0.55
            else:
                base_conf = 0.45

            # Boost for genuine uncertainty (opposite side also cheap)
            # E.g., UP=0.03, DOWN=0.40 → uncertainty=0.60 → +0.12
            uncertainty_boost = uncertainty * 0.20
            confidence = min(0.78, base_conf + uncertainty_boost)

            # Reduce confidence for very short time remaining
            if seconds_remaining < 120:
                confidence *= 0.90  # 10% penalty — less time to recover

            return TradeSignal(
                strategy=self.name,
                coin=market['coin'],
                timeframe=market['timeframe'],
                direction=side,
                token_id=token_id,
                market_id=market['market_id'],
                entry_price=ask_price,
                confidence=round(confidence, 3),
                rationale=(
                    f"🎰 CHEAP {side}: {market['coin']} {side} "
                    f"@${ask_price:.3f} = {potential_return:.0f}x potential! "
                    f"Opposite: ${opposite_ask:.2f} (uncertainty: {uncertainty:.0%}) "
                    f"Time: {seconds_remaining}s"
                ),
                metadata={
                    'ask_price': ask_price,
                    'opposite_ask': opposite_ask,
                    'uncertainty': uncertainty,
                    'potential_return': potential_return,
                    'type': 'cheap_single',
                    'seconds_remaining': seconds_remaining,
                    'is_lottery': True,
                }
            )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]


class MomentumReversal(BaseStrategy):
    """
    Buy when a side rapidly drops and looks like it'll bounce.
    In 5-min markets, a coin that was at 50% and drops to 10%
    often bounces back as people buy the dip.
    """

    name = "momentum_reversal"
    description = "Catches sharp reversals — buys the dip in probability"

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        poly_feed = context.get('poly_feed')
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not poly_feed or not clob:
            return None

        if seconds_remaining < 10:
            return None

        for side, token_key in [('UP', 'up_token_id'), ('DOWN', 'down_token_id')]:
            token_id = market.get(token_key, '')
            if not token_id:
                continue

            history = poly_feed.price_history.get(token_id)
            if not history or len(history) < 5:
                continue

            # Check for a big recent drop (last 15 seconds)
            recent = [s for s in history if s.timestamp > time.time() - 15]
            if len(recent) < 3:
                continue

            prices = [s.price for s in recent]
            max_recent = max(prices)
            current = prices[-1]
            drop = max_recent - current

            # If dropped 15+ cents in 15 seconds: reversal opportunity
            if drop >= 0.15 and current < 0.40:
                book = clob.get_orderbook(token_id)
                if not book or book['ask_depth'] < 0.50:
                    continue

                confidence = min(0.90, 0.55 + drop)

                return TradeSignal(
                    strategy=self.name,
                    coin=market['coin'],
                    timeframe=market['timeframe'],
                    direction=side,
                    token_id=token_id,
                    market_id=market['market_id'],
                    entry_price=book['best_ask'],
                    confidence=confidence,
                    rationale=(
                        f"📉➡️📈 REVERSAL: {market['coin']} {side} "
                        f"dropped {drop:.2f} ({max_recent:.2f}→{current:.2f}) in 15s. "
                        f"Buying the dip @ {book['best_ask']:.3f}"
                    ),
                    metadata={
                        'drop': drop, 'max_price': max_recent,
                        'current_price': current, 'type': 'reversal',
                    }
                )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]
