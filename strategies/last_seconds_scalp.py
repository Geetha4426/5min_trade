"""
Last-Seconds Scalp Strategy — Enter 3-12s Before Candle Close

From 0xLanister 5-15min-printer-bot research:
Binance settles BEFORE Polymarket's Chainlink oracle updates.
In the last 3-12 seconds before a 5min candle closes, the Binance direction
is already known with high certainty.

Strategy:
1. Wait until 3-12 seconds before market resolution
2. Check Binance 1s kline — direction is locked in
3. If Binance direction is clear, buy the corresponding side on Polymarket
4. Best odds: 1.8-4.0+ (price between 0.25-0.55 on winning side)

This strategy has the HIGHEST theoretical edge because you're buying with
near-perfect information that the oracle hasn't processed yet.
"""

from typing import Dict, Optional
from config import Config
from strategies.base_strategy import BaseStrategy, TradeSignal
from data.binance_signals import get_price_momentum


class LastSecondsScalpStrategy(BaseStrategy):
    """
    Last-seconds scalp: enter when outcome is nearly certain from Binance data.

    Timing window: 3-12 seconds before candle close.
    Only acts when Binance direction is clear and momentum is strong.

    Risk: If Binance moves in last seconds (whipsaw), can lose.
    Mitigation: Require strong momentum + acceleration confirmation.
    """

    name = "last_seconds_scalp"
    description = "Enter 3-12s before close when Binance direction is locked"

    # Timing window
    MAX_SECONDS_BEFORE_CLOSE = 12
    MIN_SECONDS_BEFORE_CLOSE = 3

    # Binance momentum requirements
    MIN_MOMENTUM_STRENGTH = 0.4
    MIN_VELOCITY_PCT = 0.02  # at least 0.02% per minute

    # Price range for good risk/reward (avoid buying at 0.90+)
    MAX_ENTRY_PRICE = 0.60  # price 0.60 → odds 1.67x
    MIN_ENTRY_PRICE = 0.20  # don't buy below 0.20 (too risky)

    # Ideal entry: 0.25-0.55 → odds 1.8-4.0x
    IDEAL_MIN_PRICE = 0.25
    IDEAL_MAX_PRICE = 0.55

    def get_suitable_timeframes(self):
        return [5, 15]  # primarily for 5min and 15min markets

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Check if we're in the last-seconds window and Binance direction is clear.
        """
        seconds_remaining = context.get('seconds_remaining', 999)

        # ── Timing filter: must be in the last-seconds window ──
        if (seconds_remaining > self.MAX_SECONDS_BEFORE_CLOSE or
                seconds_remaining < self.MIN_SECONDS_BEFORE_CLOSE):
            return None

        # Get coin from market
        coin = market.get('coin', '')
        if not coin:
            return None

        # ── Binance signal check ──
        momentum = get_price_momentum(coin, lookback_minutes=5)
        if not momentum:
            return None

        direction = momentum.get('direction', 'NEUTRAL')
        strength = momentum.get('strength', 0)
        velocity = abs(momentum.get('velocity', 0))

        # Must have clear direction
        if direction == 'NEUTRAL':
            return None

        # Must have sufficient strength
        if strength < self.MIN_MOMENTUM_STRENGTH:
            return None

        # Must have sufficient velocity
        if velocity < self.MIN_VELOCITY_PCT:
            return None

        # ── Enhanced check: use oracle WS if available ──
        oracle_ws = context.get('oracle_ws')
        ws_signal = None
        if oracle_ws:
            ws_signal = oracle_ws.get_signal(coin)
            if ws_signal and ws_signal.is_actionable:
                # WS confirms direction — boost confidence
                if ws_signal.direction == direction:
                    strength = min(1.0, strength * 1.3)
                else:
                    # WS disagrees — skip (conflicting signals)
                    return None

        # ── Get token prices ──
        tokens = market.get('tokens', [])
        if len(tokens) < 2:
            return None

        yes_token = None
        no_token = None
        for t in tokens:
            outcome = t.get('outcome', '').lower()
            if outcome == 'yes':
                yes_token = t
            elif outcome == 'no':
                no_token = t

        if not yes_token or not no_token:
            return None

        yes_price = float(yes_token.get('price', 0) or 0)
        no_price = float(no_token.get('price', 0) or 0)

        # ── Determine which side to buy ──
        if direction == "UP":
            # Binance going up → buy YES
            buy_token = yes_token
            entry_price = yes_price
            trade_direction = "UP"
        else:
            # Binance going down → buy NO
            buy_token = no_token
            entry_price = no_price
            trade_direction = "DOWN"

        # ── Price filter: need good odds ──
        if entry_price <= self.MIN_ENTRY_PRICE or entry_price >= self.MAX_ENTRY_PRICE:
            return None

        # ── Confidence calculation ──
        confidence = 0.40  # base

        # Strength bonus
        confidence += strength * 0.25

        # Velocity bonus
        if velocity > 0.05:
            confidence += 0.10
        elif velocity > 0.03:
            confidence += 0.05

        # Ideal price range bonus (best odds)
        if self.IDEAL_MIN_PRICE <= entry_price <= self.IDEAL_MAX_PRICE:
            confidence += 0.10

        # Timing bonus: closer to close = more certain
        if seconds_remaining <= 5:
            confidence += 0.10
        elif seconds_remaining <= 8:
            confidence += 0.05

        # WS confirmation bonus
        if ws_signal and ws_signal.is_actionable:
            confidence += 0.10

        # Acceleration bonus
        accel = momentum.get('acceleration', 0)
        if (direction == "UP" and accel > 0) or (direction == "DOWN" and accel < 0):
            confidence += 0.05

        confidence = min(0.85, confidence)

        odds = (1.0 - entry_price) / max(entry_price, 0.01)

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=market.get('timeframe', 5),
            direction=trade_direction,
            token_id=buy_token.get('token_id', ''),
            market_id=market.get('condition_id', ''),
            entry_price=entry_price,
            confidence=confidence,
            rationale=(
                f"Last-seconds scalp: {seconds_remaining}s to close. "
                f"Binance: {direction} (str={strength:.2f}, vel={velocity:.3f}%/m). "
                f"Entry at {entry_price:.3f} → odds {odds:.1f}x. "
                f"{'WS confirmed.' if ws_signal else 'REST only.'}"
            ),
            metadata={
                'seconds_remaining': seconds_remaining,
                'binance_strength': strength,
                'binance_velocity': velocity,
                'binance_direction': direction,
                'odds': odds,
                'ws_confirmed': ws_signal is not None,
                'type': 'last_seconds_scalp',
            },
        )
