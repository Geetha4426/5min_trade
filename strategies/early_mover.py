"""
Early Mover Strategy — Buy cheap tokens on Binance reversal signals.

THE EDGE: In the first half of 5-minute markets, price action creates
a "dominant" side (60-80¢) and a "cheap" side (10-25¢). When Binance
shows the real crypto price REVERSING toward the cheap side, that side
is massively underpriced — the market hasn't caught up yet.

KEY DIFFERENCE from penny_sniper (blocked in SEED):
  - penny_sniper buys blind at 1-5¢ hoping for $1 (lottery, ~5% win)
  - early_mover buys 5-22¢ tokens ONLY when Binance confirms reversal
  - Takes profit at 2-5x instead of holding for settlement
  - Win rate ~30-40% at 3-5x payoff = strong positive EV

KEY DIFFERENCE from binance_momentum:
  - binance_momentum buys the WINNING side (follows momentum)
  - early_mover buys the LOSING/CHEAP side (catches reversals)
  - They're complementary: momentum early → reversal mid-market

LIFECYCLE COVERAGE with other strategies:
  early_mover: minutes 0-3 (first half, catch reversals cheap)
  binance_momentum: minutes 1-4 (momentum lag exploitation)
  time_decay: minutes 3-5 (near expiry, confirmed winners at discount)

RISK PROFILE (SEED-optimized):
  Entry: 5-22¢ → $1.50 buys 7-30 tokens
  Stop: -10% (SEED) = 1-2¢/token loss = $0.07-0.60 max loss
  Target: +150% (2.5x) from exit system → sell at 25-55¢
  Expected: ~35% win rate at 2.5x payoff = ~0.875 EV per dollar risked
"""

import time
from typing import Dict, List, Optional
from config import Config
from strategies.base_strategy import BaseStrategy, TradeSignal
from data.quant_formulas import adverse_selection_prob, microprice_signal


class EarlyMoverStrategy(BaseStrategy):
    """Buy underpriced tokens confirmed by Binance reversal toward them."""

    name = "early_mover"
    description = "Catches Binance reversals early — buys cheap side before market reprices"

    # ── Time window ──
    MIN_SECONDS_LEFT = 100     # At least ~1:40 remaining (need time for move)
    MAX_SECONDS_LEFT = 280     # First ~3 minutes of 5min market

    # ── Entry conditions ──
    MIN_ENTRY_PRICE = 0.04     # Don't buy below 4¢ (too speculative)
    MAX_ENTRY_PRICE = 0.25     # Don't pay above 25¢ (not cheap enough)
    MIN_OPPONENT_PRICE = 0.55  # Dominant side must be 55¢+ (clear leader)
    MIN_ASK_DEPTH = 0.30       # Need liquidity to fill

    # ── Binance reversal detection ──
    REVERSAL_LOOKBACK = 30     # Detect reversal in last 30 seconds
    OVERALL_LOOKBACK = 90      # Overall market direction from last 90 seconds
    MIN_REVERSAL_PCT = 0.04    # BTC must move 0.04%+ toward cheap side
    MIN_OVERALL_PCT = 0.02     # Overall move must be 0.02%+ to establish direction

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """Find cheap tokens where Binance shows reversal starting."""
        binance_feed = context.get('binance_feed')
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not binance_feed or not clob:
            return None

        # Time window check
        if seconds_remaining < self.MIN_SECONDS_LEFT or seconds_remaining > self.MAX_SECONDS_LEFT:
            return None

        coin = market['coin']
        timeframe = market.get('timeframe', 5)

        # ── STEP 1: Get Binance price history ──
        price_hist = binance_feed.price_history.get(coin)
        if not price_hist or len(price_hist) < 6:
            return None

        now = time.time()

        # Recent snapshots: last 30 seconds (reversal detection)
        recent = [s for s in price_hist if s.timestamp > now - self.REVERSAL_LOOKBACK]
        if len(recent) < 3:
            return None

        # Longer history: last 90 seconds (overall direction)
        longer = [s for s in price_hist if s.timestamp > now - self.OVERALL_LOOKBACK]
        if len(longer) < 5:
            return None

        # Overall direction: where has BTC been going?
        overall_change = (longer[-1].price - longer[0].price) / longer[0].price * 100

        # Recent direction: last 30 seconds (should oppose overall = reversal)
        recent_change = (recent[-1].price - recent[0].price) / recent[0].price * 100

        # ── STEP 2: Detect reversal (recent opposes overall) ──
        if overall_change > self.MIN_OVERALL_PCT and recent_change < -self.MIN_REVERSAL_PCT:
            # BTC was going UP overall, but now dropping → cheap side is DOWN
            cheap_side = 'DOWN'
            cheap_token_key = 'down_token_id'
            dominant_token_key = 'up_token_id'
            reversal_strength = abs(recent_change)
        elif overall_change < -self.MIN_OVERALL_PCT and recent_change > self.MIN_REVERSAL_PCT:
            # BTC was going DOWN overall, but now recovering → cheap side is UP
            cheap_side = 'UP'
            cheap_token_key = 'up_token_id'
            dominant_token_key = 'down_token_id'
            reversal_strength = abs(recent_change)
        else:
            return None  # No reversal detected

        # ── STEP 3: Check reversal consistency (not just noise) ──
        if len(recent) >= 4:
            ticks = []
            for i in range(1, len(recent)):
                ticks.append(recent[i].price - recent[i - 1].price)

            if cheap_side == 'UP':
                # UP reversal: most ticks should be positive (price going up)
                favorable_ticks = sum(1 for t in ticks if t > 0)
            else:
                # DOWN reversal: most ticks should be negative (price going down)
                favorable_ticks = sum(1 for t in ticks if t < 0)

            if favorable_ticks < len(ticks) * 0.55:
                return None  # Too noisy — not a real reversal

        # ── STEP 4: Check orderbook prices ──
        cheap_token = market.get(cheap_token_key, '')
        dominant_token = market.get(dominant_token_key, '')

        if not cheap_token or not dominant_token:
            return None

        cheap_book = clob.get_orderbook(cheap_token)
        dominant_book = clob.get_orderbook(dominant_token)

        if not cheap_book or not dominant_book:
            return None

        cheap_ask = cheap_book['best_ask']
        dominant_ask = dominant_book['best_ask']

        # Validate: cheap side is actually cheap
        if not (self.MIN_ENTRY_PRICE <= cheap_ask <= self.MAX_ENTRY_PRICE):
            return None

        # Validate: dominant side is clearly leading
        if dominant_ask < self.MIN_OPPONENT_PRICE:
            return None  # Market is undecided — don't gamble

        # Validate: enough liquidity on cheap side
        if cheap_book.get('ask_depth', 0) < self.MIN_ASK_DEPTH:
            return None

        # ── STEP 5: Extra quality checks ──
        # Reversal should be meaningful relative to overall move
        if reversal_strength < abs(overall_change) * 0.25:
            return None  # Reversal is tiny vs overall trend — probably noise

        # The spread between dominant and cheap should be wide (room to move)
        spread = dominant_ask - cheap_ask
        if spread < 0.30:
            return None  # Not enough asymmetry

        # ── Quant guard: adverse selection ──
        cheap_bid = cheap_book.get('best_bid', 0)
        cheap_spread = cheap_ask - cheap_bid if cheap_bid > 0 else 0.10
        if adverse_selection_prob(cheap_spread) > 0.35:
            return None  # Informed traders dominating cheap side

        # ── Quant guard: MicroPrice must not oppose our direction ──
        cheap_bid_d = cheap_book.get('bid_depth', 0)
        cheap_ask_d = cheap_book.get('ask_depth', 0)
        if cheap_bid_d > 0 or cheap_ask_d > 0:
            mp = microprice_signal(
                cheap_bid or cheap_ask - 0.01, cheap_ask, cheap_bid_d, cheap_ask_d)
            # For cheap side: MicroPrice should be bullish (buyers accumulating)
            if mp['direction'] == 'DOWN' and mp['strength'] > 0.5:
                return None  # MicroPrice says sellers dominating — don't fight it

        # ── STEP 6: Calculate confidence ──
        # Base: 0.72 (reversal confirmed by Binance + cheap entry)
        confidence = 0.72

        # Reversal strength bonus: stronger reversal = more conviction
        # 0.04% → +0.06,  0.08%+ → +0.12 (capped)
        confidence += min(0.12, reversal_strength * 1.5)

        # Price cheapness bonus: cheaper = more room to run
        if cheap_ask <= 0.08:
            confidence += 0.07   # Super cheap — huge upside if reversal holds
        elif cheap_ask <= 0.14:
            confidence += 0.05   # Very cheap — great risk/reward
        elif cheap_ask <= 0.20:
            confidence += 0.03   # Cheap — decent upside
        else:
            confidence += 0.01   # 20-25¢ — less room but still asymmetric

        # Time bonus: more time remaining = more room for reversal to play out
        if seconds_remaining > 220:
            confidence += 0.04   # Plenty of time
        elif seconds_remaining > 160:
            confidence += 0.02   # Decent time

        # Spread bonus: wider spread = more profit potential
        if spread > 0.50:
            confidence += 0.02

        confidence = min(0.95, confidence)

        # Calculate potential profit target for the rationale
        target_price = min(0.55, cheap_ask * 3.5)

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=timeframe,
            direction=cheap_side,
            token_id=cheap_token,
            market_id=market['market_id'],
            entry_price=cheap_ask,
            confidence=confidence,
            rationale=(
                f"🔄 EARLY MOVER: {coin} {cheap_side} at {cheap_ask:.2f} — "
                f"Binance reversing ({recent_change:+.3f}% in {self.REVERSAL_LOOKBACK}s, "
                f"vs overall {overall_change:+.3f}%). "
                f"Dominant side at {dominant_ask:.2f}. "
                f"{seconds_remaining}s left → target ~{target_price:.2f}"
            ),
            metadata={
                'type': 'early_mover',
                'cheap_price': cheap_ask,
                'dominant_price': dominant_ask,
                'spread': spread,
                'reversal_strength': reversal_strength,
                'overall_btc_change': overall_change,
                'recent_btc_change': recent_change,
                'target_price': target_price,
                'seconds_remaining': seconds_remaining,
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]
