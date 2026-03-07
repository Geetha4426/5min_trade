"""
Time Decay Strategy (Theta Farming) — V2

Near-expiry markets have dramatically reduced uncertainty.
With < 2 minutes left, the winning outcome is nearly certain.
Buy the winning side at a discount when it hasn't fully repriced.

KEY IMPROVEMENT (V2): Uses Binance REAL PRICE to confirm direction.
Old version just followed the orderbook — pure momentum chasing.
New version cross-validates: Binance direction + market direction
must AGREE, otherwise skip. This filters out fake momentum.

Logic:
1. Find markets with < 2 minutes remaining
2. Get Binance price history → determine REAL direction (price vs N seconds ago)
3. Get orderbook → determine MARKET direction (which side is more expensive)
4. BOTH must agree → the winning side is truly winning
5. If the winning side is trading below fair value → buy it (confirmed edge)
"""

import time
from typing import Dict, List, Optional
from config import Config
from strategies.base_strategy import BaseStrategy, TradeSignal


class TimeDecayStrategy(BaseStrategy):
    """Exploit near-expiry markets where outcome is confirmed by Binance."""

    name = "time_decay"
    description = "Buys Binance-confirmed near-certain outcomes at a discount"

    # Minimum market lead: winning side must be at least this much higher
    MIN_MARKET_LEAD = 0.06  # 6¢ minimum spread between up/down ask
    # Minimum Binance move to confirm direction (% change)
    MIN_BINANCE_MOVE_PCT = 0.04  # 0.04% — slightly higher bar to filter noise

    def __init__(self):
        self.max_remaining = Config.DECAY_MAX_REMAINING_SECONDS
        self.min_discount = Config.DECAY_MIN_NO_DISCOUNT

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """Find decayed outcomes confirmed by Binance real price direction."""
        binance_feed = context.get('binance_feed')
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not binance_feed or not clob:
            return None

        # Only trade near expiry
        if seconds_remaining > self.max_remaining or seconds_remaining < 10:
            return None

        coin = market['coin']
        timeframe = market.get('timeframe', 5)

        # ── STEP 1: Get Binance direction (the REAL signal) ──
        real_price = binance_feed.get_price(coin)
        if not real_price:
            return None

        price_hist = binance_feed.price_history.get(coin)
        if not price_hist or len(price_hist) < 3:
            return None

        # Look back ~timeframe minutes to find reference price (candle open proxy)
        now = time.time()
        lookback = timeframe * 60  # 300s for 5min, 900s for 15min
        ref_snapshots = [s for s in price_hist if s.timestamp < now - lookback + 30]

        if ref_snapshots:
            ref_price = ref_snapshots[-1].price  # Closest to candle open
        else:
            # Fallback: use oldest available snapshot
            oldest = list(price_hist)[0]
            ref_price = oldest.price

        if ref_price <= 0:
            return None

        binance_change_pct = ((real_price - ref_price) / ref_price) * 100

        # Determine Binance direction — needs minimum move
        if binance_change_pct > self.MIN_BINANCE_MOVE_PCT:
            binance_direction = 'UP'
        elif binance_change_pct < -self.MIN_BINANCE_MOVE_PCT:
            binance_direction = 'DOWN'
        else:
            return None  # Binance is flat — no clear direction, skip

        # ── STEP 2: Get market direction from orderbook ──
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))

        if not up_book or not down_book:
            return None

        up_ask = up_book['best_ask']
        down_ask = down_book['best_ask']

        # Market must show clear leader (minimum spread)
        market_lead = abs(up_ask - down_ask)
        if market_lead < self.MIN_MARKET_LEAD:
            return None  # Market is undecided — don't gamble

        if up_ask > down_ask:
            market_direction = 'UP'
        else:
            market_direction = 'DOWN'

        # ── STEP 3: Cross-validate — BOTH must agree ──
        if binance_direction != market_direction:
            return None  # Conflicting signals — Binance says one thing, market says another

        # ── STEP 4: Calculate discount and signal ──
        # Fair value increases as expiry approaches (winner → $1.00)
        fair_value = 0.90 + (0.10 * (1 - seconds_remaining / self.max_remaining))

        if market_direction == 'UP':
            winning_ask = up_ask
            winning_depth = up_book.get('ask_depth', 0)
            token_id = market.get('up_token_id', '')
        else:
            winning_ask = down_ask
            winning_depth = down_book.get('ask_depth', 0)
            token_id = market.get('down_token_id', '')

        if not token_id:
            return None

        # Check ask depth — need at least $1 liquidity to fill
        if winning_depth < 1.0:
            return None

        discount = fair_value - winning_ask

        if discount < self.min_discount or winning_ask >= 0.78:
            return None  # Cap entries at 78¢ — above this, fees eat most of the edge

        # ── Confidence: base + discount + Binance strength ──
        confidence = 0.70 + discount  # Base: 0.70 + discount (e.g. 0.20 → 0.90)

        # Binance move strength bonus: stronger Binance move = more certainty
        binance_strength = min(0.10, abs(binance_change_pct) * 0.20)
        confidence += binance_strength

        # Time pressure: closer to expiry = more certain
        if seconds_remaining < 45:
            confidence += 0.03

        confidence = min(0.95, confidence)

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=timeframe,
            direction=market_direction,
            token_id=token_id,
            market_id=market['market_id'],
            entry_price=winning_ask,
            confidence=confidence,
            rationale=(
                f"⏰ TIME DECAY: {coin} {market_direction} confirmed by Binance "
                f"({binance_change_pct:+.3f}%). "
                f"Price: {winning_ask:.4f} vs fair: {fair_value:.4f}. "
                f"Discount: {discount:.2f}. {seconds_remaining}s left."
            ),
            metadata={
                'seconds_remaining': seconds_remaining,
                'fair_value': fair_value,
                'discount': discount,
                'binance_change_pct': binance_change_pct,
                'binance_confirms': True,
                'market_lead': market_lead,
                'type': 'time_decay',
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]
