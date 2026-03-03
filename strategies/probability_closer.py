"""
Probability Closer — Near-Expiry High-Conviction Play

FROM X POST INSIGHT: Enter the side with 80%+ probability in the
final 2 minutes of any market. When one side is clearly winning,
the price often hasn't fully converged to $1.00.

STRATEGY:
1. Wait for last 2 minutes of any market
2. If either side is priced at 80%+ ($0.80+), buy it
3. Target: ride it to $0.95-1.00 as settlement approaches
4. Risk: low — the outcome is 80%+ certain, buying at a discount

MATH:
  - Buy at $0.85 → settles at $1.00 → 17.6% return (minus fees)
  - Buy at $0.90 → settles at $1.00 → 11.1% return (minus fees)
  - But: must account for ~1.56% taker fee → net ~15% / ~9.5%
  - Win rate: ~80-85% (matches the implied probability)

KEY: Only enter when there's a clear winner and a discount to $1.00.
Uses Binance price confirmation for extra safety.
"""

from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal


class ProbabilityCloserStrategy(BaseStrategy):
    """
    Buy the near-certain winner in the final 2 minutes of a market.
    
    When the 80%+ side is trading below $0.95, that's a discount
    on a near-certain outcome.
    """

    name = "prob_closer"
    description = "Buys 80%+ probability side near expiry for safe ~10-15% returns"

    # Only trade in the final N seconds
    MAX_SECONDS = 120  # Last 2 minutes
    MIN_SECONDS = 10   # Not too close to settlement

    # Minimum probability (price) to consider a "near-certain" winner
    MIN_WINNER_PRICE = 0.78  # 78% implied probability

    # Maximum price to buy at (discount from $1.00)
    MAX_BUY_PRICE = 0.95   # Must have at least 5% discount

    # Minimum depth to ensure we can fill
    MIN_DEPTH = 1.0  # $1

    # Estimated taker fee — dynamic per price level
    BASE_FEE_RATE = 0.03125  # 3.125% at p=0.50 (reference)

    @staticmethod
    def _dynamic_fee(price: float) -> float:
        """Polymarket effective fee rate: 0.25 × p × (1-p)².
        Peak ~3.7% at p≈0.33. Settlement is FREE (0%)."""
        p = max(0.001, min(0.999, price))
        q = 1.0 - p
        return 0.25 * p * q * q

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Check if market has a near-certain winner trading at a discount.
        """
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob:
            return None

        # Only trade near expiry
        if seconds_remaining > self.MAX_SECONDS or seconds_remaining < self.MIN_SECONDS:
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

        # Find the higher-probability side
        candidates = []

        if up_ask >= self.MIN_WINNER_PRICE and up_ask <= self.MAX_BUY_PRICE:
            discount = 1.0 - up_ask
            fee_rate = self._dynamic_fee(up_ask)
            net_profit = discount - (up_ask * fee_rate * 2)
            if net_profit > 0.01:  # At least 1¢ net profit
                candidates.append({
                    'direction': 'UP',
                    'token_id': up_token,
                    'price': up_ask,
                    'discount': discount,
                    'net_profit': net_profit,
                    'depth': up_book.get('ask_depth', 0),
                })

        if down_ask >= self.MIN_WINNER_PRICE and down_ask <= self.MAX_BUY_PRICE:
            discount = 1.0 - down_ask
            fee_rate = self._dynamic_fee(down_ask)
            net_profit = discount - (down_ask * fee_rate * 2)
            if net_profit > 0.01:
                candidates.append({
                    'direction': 'DOWN',
                    'token_id': down_token,
                    'price': down_ask,
                    'discount': discount,
                    'net_profit': net_profit,
                    'depth': down_book.get('ask_depth', 0),
                })

        if not candidates:
            return None

        # Pick the best candidate (highest discount = most profit)
        best = max(candidates, key=lambda c: c['net_profit'])

        # Check depth
        if best['depth'] < self.MIN_DEPTH:
            return None

        # Optional: Binance confirmation
        binance_confirms = False
        if binance_feed:
            coin = market['coin']
            price_hist = binance_feed.price_history.get(coin)
            if price_hist and len(price_hist) >= 3:
                import time
                recent = [s for s in price_hist if s.timestamp > time.time() - 60]
                if len(recent) >= 2:
                    price_change = recent[-1].price - recent[0].price
                    if best['direction'] == 'UP' and price_change > 0:
                        binance_confirms = True
                    elif best['direction'] == 'DOWN' and price_change < 0:
                        binance_confirms = True

        # Confidence: higher price = more certain, confirmed by Binance = bonus
        base_conf = min(0.90, 0.60 + best['price'] * 0.3)
        if binance_confirms:
            base_conf = min(0.95, base_conf + 0.10)

        # More confident closer to expiry
        if seconds_remaining < 60:
            base_conf = min(0.95, base_conf + 0.05)

        pct_return = best['net_profit'] / best['price'] * 100

        rationale = (
            f"📊 PROB CLOSER: {market['coin']} {best['direction']} "
            f"@ {best['price']:.2f} ({best['price']*100:.0f}% prob)\n"
            f"  Discount: {best['discount']:.2f} | "
            f"Net profit: ${best['net_profit']:.3f}/share ({pct_return:.1f}%)\n"
            f"  Time: {seconds_remaining}s remaining | "
            f"Binance confirms: {'✅' if binance_confirms else '❌'}"
        )

        return TradeSignal(
            strategy=self.name,
            coin=market['coin'],
            timeframe=market['timeframe'],
            direction=best['direction'],
            token_id=best['token_id'],
            market_id=market['market_id'],
            entry_price=best['price'],
            confidence=base_conf,
            rationale=rationale,
            metadata={
                'discount': best['discount'],
                'net_profit': best['net_profit'],
                'pct_return': pct_return,
                'binance_confirms': binance_confirms,
                'type': 'prob_closer',
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]
