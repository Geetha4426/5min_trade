"""
Buy-Opposite Contrarian Strategy — Mean Reversion on Spikes

From Gabagool2-2 research:
When one side (YES) spikes above 0.98, the opposite (NO) becomes
extremely cheap. Markets tend to revert — the spike overshot.

Strategy:
1. Detect when YES > 0.98 (or NO > 0.98)
2. Buy the opposite side at bargain price
3. Profit from mean reversion when price normalizes

Also works with configurable thresholds for less extreme cases.
"""

from typing import Dict, Optional
from config import Config
from strategies.base_strategy import BaseStrategy, TradeSignal


class BuyOppositeStrategy(BaseStrategy):
    """
    Contrarian mean-reversion: buy the cheap side when opposite spikes.

    When YES price > SPIKE_THRESHOLD:
      → Buy NO at (1 - YES_price) — extremely cheap
      → Expect YES to revert from overextension

    When NO price > SPIKE_THRESHOLD:
      → Buy YES at (1 - NO_price)

    Risk-reward is asymmetric: small cost, potential big payoff if reversion happens.
    Even partial reversion is profitable.
    """

    name = "buy_opposite"
    description = "Buy cheap opposite when one side spikes > 0.98"

    # Primary threshold: extreme spike (high confidence)
    SPIKE_THRESHOLD = 0.98

    # Secondary threshold: strong spike (moderate confidence)
    STRONG_THRESHOLD = 0.95

    # Minimum volume to confirm the spike is real
    MIN_VOLUME = 500

    # Don't buy opposite if spread is too wide (market dead)
    MAX_SPREAD = 0.05

    def get_suitable_timeframes(self):
        return [5, 15, 30]  # works across all timeframes

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Check if one side has spiked and the opposite is a bargain.
        """
        clob = context.get('clob')
        if not clob:
            return None

        # Get market token data
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

        if yes_price <= 0 or no_price <= 0:
            return None

        # Check spread
        spread = abs(yes_price + no_price - 1.0)
        if spread > self.MAX_SPREAD:
            return None

        # Volume check
        volume = float(market.get('volume', 0) or 0)
        seconds_remaining = context.get('seconds_remaining', 300)

        # ── Check: YES spiked → buy NO ──
        if yes_price >= self.SPIKE_THRESHOLD:
            opposite_price = no_price
            buy_token = no_token
            buy_side = "DOWN"  # buying NO = bearish on YES
            spike_side = "YES"
            spike_price = yes_price

            # Higher confidence for more extreme spikes
            if yes_price >= 0.99:
                confidence = 0.55  # Very extreme — strong reversion expected
            elif yes_price >= self.SPIKE_THRESHOLD:
                confidence = 0.45
            else:
                confidence = 0.35

            return self._build_signal(
                market, buy_token, buy_side, opposite_price,
                spike_side, spike_price, confidence, seconds_remaining
            )

        # ── Check: NO spiked → buy YES ──
        if no_price >= self.SPIKE_THRESHOLD:
            opposite_price = yes_price
            buy_token = yes_token
            buy_side = "UP"  # buying YES = bullish
            spike_side = "NO"
            spike_price = no_price

            if no_price >= 0.99:
                confidence = 0.55
            elif no_price >= self.SPIKE_THRESHOLD:
                confidence = 0.45
            else:
                confidence = 0.35

            return self._build_signal(
                market, buy_token, buy_side, opposite_price,
                spike_side, spike_price, confidence, seconds_remaining
            )

        # ── Secondary: Strong but not extreme spikes ──
        if yes_price >= self.STRONG_THRESHOLD and yes_price < self.SPIKE_THRESHOLD:
            # Moderate spike — lower confidence
            if volume >= self.MIN_VOLUME * 2:  # need more volume confirmation
                return self._build_signal(
                    market, no_token, "DOWN", no_price,
                    "YES", yes_price, 0.30, seconds_remaining
                )

        if no_price >= self.STRONG_THRESHOLD and no_price < self.SPIKE_THRESHOLD:
            if volume >= self.MIN_VOLUME * 2:
                return self._build_signal(
                    market, yes_token, "UP", yes_price,
                    "NO", no_price, 0.30, seconds_remaining
                )

        return None

    def _build_signal(self, market: Dict, buy_token: Dict,
                       direction: str, entry_price: float,
                       spike_side: str, spike_price: float,
                       confidence: float, seconds_remaining: int
                       ) -> TradeSignal:
        """Build the trade signal."""
        coin = market.get('coin', market.get('question', 'UNK')[:6])
        timeframe = market.get('timeframe', 5)
        token_id = buy_token.get('token_id', '')
        market_id = market.get('condition_id', '')

        # Time bonus: closer to expiry, more certain the spike will or won't hold
        if seconds_remaining < 60:
            confidence *= 0.7  # too close, reversion may not happen
        elif seconds_remaining < 120:
            confidence *= 1.0
        elif seconds_remaining < 240:
            confidence *= 1.1  # sweet spot: enough time to revert

        risk_reward = (1.0 - entry_price) / max(entry_price, 0.01)

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=timeframe,
            direction=direction,
            token_id=token_id,
            market_id=market_id,
            entry_price=entry_price,
            confidence=min(0.75, confidence),
            rationale=(
                f"Buy-Opposite: {spike_side} spiked to {spike_price:.3f}, "
                f"buying opposite at {entry_price:.3f}. "
                f"R:R = {risk_reward:.1f}x. "
                f"Mean reversion expected from overextension."
            ),
            metadata={
                'spike_side': spike_side,
                'spike_price': spike_price,
                'risk_reward': risk_reward,
                'type': 'contrarian_reversion',
            },
        )
