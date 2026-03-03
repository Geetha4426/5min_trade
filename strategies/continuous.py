"""
Additional Strategies — Continuous Trading

These strategies ensure the bot ALWAYS has something to trade,
not just waiting for 1% outcomes.

Strategies:
1. Trend Follower — ride the momentum
2. Straddle — buy both sides during high volatility  
3. Spread Scalper — profit from bid-ask gaps
4. Mid-Price Sniper — buy underpriced mid-range outcomes
"""

import time
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal


class TrendFollower(BaseStrategy):
    """
    Follow the trend. If a side is gaining momentum, ride it.
    
    Logic:
    - Track price changes over last 30-60 seconds
    - If Up is trending up (price increasing), buy Up
    - If Down is trending up, buy Down
    - The trend is your friend in 5-min markets
    """

    name = "trend_follower"
    description = "Rides momentum — buys the side that's trending up"

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        poly_feed = context.get('poly_feed')
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not poly_feed or not clob:
            return None

        # Need at least 20s left to ride a trend
        if seconds_remaining < 20:
            return None

        for side, token_key in [('UP', 'up_token_id'), ('DOWN', 'down_token_id')]:
            token_id = market.get(token_key, '')
            if not token_id:
                continue

            history = poly_feed.price_history.get(token_id)
            if not history or len(history) < 4:
                continue

            # Look at last 30 seconds
            recent = [s for s in history if s.timestamp > time.time() - 30]
            if len(recent) < 3:
                continue

            prices = [s.price for s in recent]
            
            # Calculate trend: is price consistently moving up?
            gains = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i-1])
            total_moves = len(prices) - 1
            
            if total_moves < 2:
                continue
            
            trend_strength = gains / total_moves  # 0.0 = all down, 1.0 = all up
            price_change = prices[-1] - prices[0]
            
            # Strong uptrend: 70%+ of moves are up AND net gain > 3 cents
            if trend_strength >= 0.70 and price_change >= 0.03:
                # Don't buy if already overpriced (above 85 cents)
                if prices[-1] > 0.85:
                    continue

                book = clob.get_orderbook(token_id)
                if not book or book['ask_depth'] < 0.50:
                    continue

                confidence = min(0.85, 0.50 + trend_strength * 0.3 + price_change)

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
                        f"📈 TREND: {market['coin']} {side} trending up "
                        f"({price_change:+.3f} over {len(recent)} ticks, "
                        f"{trend_strength:.0%} up moves). "
                        f"Riding momentum @ ${book['best_ask']:.3f}"
                    ),
                    metadata={
                        'trend_strength': trend_strength,
                        'price_change': price_change,
                        'type': 'trend',
                    }
                )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]


class StraddleStrategy(BaseStrategy):
    """
    Buy BOTH sides during high volatility periods.
    
    When both Up and Down are mid-range (30-70 cents each),
    prices swing wildly. Buy both, sell the winner for profit.
    If one side goes to 80+ cents, sell it for quick profit.
    """

    name = "straddle"
    description = "Buys both sides during volatile mid-range pricing"

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        poly_feed = context.get('poly_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob:
            return None

        # Need at least 30s for volatility to play out
        if seconds_remaining < 30:
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

        # Both sides in the "volatile zone" (25-65 cents each)
        if not (0.25 <= up_ask <= 0.65 and 0.25 <= down_ask <= 0.65):
            return None

        combined = up_ask + down_ask

        # CRITICAL: if combined >= $0.92, guaranteed loss after fees
        # Both sides pay out exactly $1.00 at settlement (FREE)
        # Entry fee = 0.25 × p × (1-p)², at p≈0.50 ≈ 3.125% → ~1.56¢ per side
        # Two legs ≈ 3.12¢; threshold $0.92 gives ≥ $0.05 buffer for safety
        if combined >= 0.92:
            return None

        # Check for volatility: are prices swinging?
        volatility = 0
        if poly_feed:
            for token_id in [up_token, down_token]:
                history = poly_feed.price_history.get(token_id)
                if history and len(history) >= 3:
                    recent = [s for s in history if s.timestamp > time.time() - 30]
                    if len(recent) >= 2:
                        prices = [s.price for s in recent]
                        volatility += max(prices) - min(prices)

        # Only straddle if there's price movement (volatility > 5 cents combined)
        if volatility < 0.05:
            return None

        # Combined cost determines if it's worth it
        # One side WILL go to ~$1.00 at settlement
        potential = 1.0 - combined

        confidence = min(0.80, 0.50 + volatility + max(0, potential * 2))

        return TradeSignal(
            strategy=self.name,
            coin=market['coin'],
            timeframe=market['timeframe'],
            direction='BOTH',
            token_id=f"{up_token}|{down_token}",
            market_id=market['market_id'],
            entry_price=combined,
            confidence=confidence,
            rationale=(
                f"🔀 STRADDLE: {market['coin']} volatile! "
                f"Up@{up_ask:.3f} + Down@{down_ask:.3f} = {combined:.3f}. "
                f"Vol: {volatility:.3f}. "
                f"Sell the winner when it pumps!"
            ),
            metadata={
                'up_ask': up_ask, 'down_ask': down_ask,
                'combined': combined, 'volatility': volatility,
                'type': 'straddle',
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]


class SpreadScalper(BaseStrategy):
    """
    Profit from wide bid-ask spreads.
    
    When spread is wide (5+ cents), buy at the bid and sell at the ask.
    Even without market direction, the spread pays.
    """

    name = "spread_scalper"
    description = "Profits from wide bid-ask spreads"

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob:
            return None

        if seconds_remaining < 15:
            return None

        for side, token_key in [('UP', 'up_token_id'), ('DOWN', 'down_token_id')]:
            token_id = market.get(token_key, '')
            if not token_id:
                continue

            book = clob.get_orderbook(token_id)
            if not book:
                continue

            spread = book['best_ask'] - book['best_bid']

            # Wide spread = opportunity (5+ cents)
            if spread >= 0.05 and book['bid_depth'] > 0.50 and book['ask_depth'] > 0.50:
                # We buy at the ask with FOK, target exit at a mid-to-ask price
                mid_price = (book['best_ask'] + book['best_bid']) / 2
                
                # Profit target: sell at mid (half the spread minus fees)
                # Fee = 0.25 × p × (1-p)², peak ~3.7% at p≈0.33
                # Need spread/2 > ~7% of entry price for round-trip profit
                min_profit_spread = book['best_ask'] * 0.07  # ~7% for round-trip fees
                if spread / 2 < min_profit_spread:
                    continue

                # Only trade in reasonable price range (10-90 cents)
                if not (0.10 <= mid_price <= 0.90):
                    continue

                confidence = min(0.75, 0.45 + spread * 3)

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
                        f"📊 SPREAD: {market['coin']} {side} "
                        f"spread={spread:.3f} (bid:{book['best_bid']:.3f} "
                        f"ask:{book['best_ask']:.3f}). "
                        f"Buy close to bid, profit on spread."
                    ),
                    metadata={
                        'spread': spread, 'mid_price': mid_price,
                        'best_bid': book['best_bid'],
                        'best_ask': book['best_ask'],
                        'type': 'spread',
                    }
                )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]


class MidPriceSniper(BaseStrategy):
    """
    Buy mid-range outcomes (15-45 cents) that are underpriced.
    
    Uses Binance price feed to determine if the market probability
    is too low for reality. If BTC is clearly trending up but
    the Up outcome is only at 35 cents, that's a buy.
    """

    name = "mid_sniper"
    description = "Buys underpriced mid-range outcomes using price feeds"

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob or not binance_feed:
            return None

        if seconds_remaining < 15:
            return None

        coin = market['coin']
        binance_price = binance_feed.get_price(coin)
        if not binance_price:
            return None

        # Get price trend from Binance
        price_history = binance_feed.price_history.get(coin)
        if not price_history or len(price_history) < 3:
            return None

        recent_prices = [p.price for p in price_history if p.timestamp > time.time() - 60]
        if len(recent_prices) < 2:
            return None

        price_change_pct = (recent_prices[-1] - recent_prices[0]) / recent_prices[0] * 100

        # Determine which side Binance is favoring
        if price_change_pct > 0.05:  # BTC going up > 0.05%
            favored_side = 'UP'
            favored_token = market.get('up_token_id', '')
        elif price_change_pct < -0.05:
            favored_side = 'DOWN'
            favored_token = market.get('down_token_id', '')
        else:
            return None  # No clear direction

        if not favored_token:
            return None

        book = clob.get_orderbook(favored_token)
        if not book:
            return None

        ask_price = book['best_ask']

        # Mid-range and underpriced? (15-50 cents with clear Binance direction)
        if 0.15 <= ask_price <= 0.50:
            strength = abs(price_change_pct)
            confidence = min(0.80, 0.50 + strength * 5)

            return TradeSignal(
                strategy=self.name,
                coin=market['coin'],
                timeframe=market['timeframe'],
                direction=favored_side,
                token_id=favored_token,
                market_id=market['market_id'],
                entry_price=ask_price,
                confidence=confidence,
                rationale=(
                    f"🎯 SNIPER: {coin} {favored_side} underpriced @ {ask_price:.3f}! "
                    f"Binance {coin} {price_change_pct:+.3f}% → "
                    f"{favored_side} should be higher."
                ),
                metadata={
                    'price_change_pct': price_change_pct,
                    'binance_price': binance_price,
                    'type': 'mid_sniper',
                }
            )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]
