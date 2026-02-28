"""
Volatility Penny Sniper — $1 → $50 Strategy

THE EDGE: On 5-minute BTC markets, when volatility is HIGH, tokens
priced at 2-5¢ have a MUCH higher actual probability of hitting $1
than the price implies (reported 1.5x higher by experienced traders).

STRATEGY (from someone who turned $1 into $50):
1. Check BTC volatility — only trade when vol index > threshold
2. Place limit orders at 2-5¢ on 5-min markets  
3. Most orders lose, but ONE win = 20-50x return covers all losses
4. Side doesn't matter — just need a spike in either direction

MATH:
  - Buy at 3¢ → if it goes to $1, that's 33x return
  - Need to win 1 in ~25 trades to break even on 3¢ bets
  - Actual win rate at 3¢ is ~1 in 15-20 (1.5x better than priced)
  - Expected value is POSITIVE

KEY INSIGHT: High volatility on small timeframes turns 2¢ into $1
more often than you think. The Polymarket CLOB doesn't price this
tail risk correctly.

VOLATILITY CHECK: Uses Binance kline data to calculate real-time
volatility (stddev of returns). When vol is high, penny bets fly.
"""

import math
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal
from data.binance_signals import _get_klines, _parse_klines, BINANCE_PAIRS


def get_btc_volatility(lookback_minutes: int = 30) -> Dict:
    """
    Calculate real-time BTC volatility from Binance 1m klines.
    
    Replaces TradingView's BTC volatility index with our own calculation.
    Uses standard deviation of log returns, annualized.
    
    Returns:
        dict with: vol_index (comparable to TradingView), is_high,
                   stddev, max_move, avg_move
    """
    klines = _parse_klines(_get_klines('BTC', '1m', lookback_minutes))
    if len(klines) < 10:
        return {
            'vol_index': 0, 'is_high': False,
            'stddev': 0, 'max_move': 0, 'avg_move': 0,
        }

    closes = [k['close'] for k in klines]

    # Log returns
    log_returns = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            log_returns.append(math.log(closes[i] / closes[i-1]))

    if not log_returns:
        return {
            'vol_index': 0, 'is_high': False,
            'stddev': 0, 'max_move': 0, 'avg_move': 0,
        }

    # Standard deviation of returns
    mean_ret = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
    stddev = math.sqrt(variance)

    # Annualize (for index comparison): * sqrt(525600) minutes/year
    # But for our purposes, we use a custom scale
    # TradingView's BTC vol index: ~2.0 = calm, ~3.0+ = high
    # Our mapping: stddev * 10000 gives roughly comparable numbers
    vol_index = stddev * 10000

    # Max single candle move (absolute %)
    pct_moves = [abs(closes[i] - closes[i-1]) / closes[i-1] * 100
                 for i in range(1, len(closes))]
    max_move = max(pct_moves) if pct_moves else 0
    avg_move = sum(pct_moves) / len(pct_moves) if pct_moves else 0

    # High volatility threshold (comparable to TradingView's 2.6)
    is_high = vol_index >= 1.5

    return {
        'vol_index': round(vol_index, 2),
        'is_high': is_high,
        'stddev': stddev,
        'max_move': max_move,
        'avg_move': avg_move,
    }


class VolatilityPennySniperStrategy(BaseStrategy):
    """
    Place penny bets on extreme outcomes during high-volatility periods.
    
    When BTC volatility is high, buy tokens priced at 2-5¢ as lottery
    tickets. Most lose, but winners pay 20-50x covering all losses.
    """

    name = "penny_sniper"
    description = "Penny bets during high volatility for 20-50x returns"

    # Volatility threshold (TradingView-comparable scale)
    MIN_VOL_INDEX = 1.5

    # Price range for penny bets (in dollars, 0-1 scale)
    MIN_PENNY_PRICE = 0.01   # Don't buy below 1¢ (too illiquid)
    MAX_PENNY_PRICE = 0.06   # Max 6¢ per token

    # Optimal penny price (sweet spot for risk/reward)
    OPTIMAL_PRICE = 0.03     # 3¢ = 33x potential

    # Minimum ask depth to avoid being stuck
    MIN_DEPTH = 1.0  # Need at least $1 of depth

    # Only trade short timeframes (volatility edge is strongest)
    PREFERRED_TFS = [1, 5]

    # Maximum active penny positions (don't overexpose)
    MAX_PENNY_POSITIONS = 5

    def __init__(self):
        # Track active penny bets to limit exposure (instance-level)
        self._active_pennies = []

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Check if this market has penny tokens worth sniping.
        
        Flow:
        1. Check BTC volatility — skip if too calm
        2. Find tokens priced at 2-5¢
        3. Calculate expected value
        4. Signal if EV positive
        """
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        # Enforce max penny position limit
        if len(self._active_pennies) >= self.MAX_PENNY_POSITIONS:
            return None

        if not clob:
            return None

        coin = market['coin']
        timeframe = market['timeframe']

        # Only trade short timeframes
        if timeframe not in self.PREFERRED_TFS:
            return None

        # Need enough time left for volatility to work
        if seconds_remaining < 30:  # Too close to settlement
            return None
        if seconds_remaining > timeframe * 60 * 0.7:  # Too early, prices centered
            return None

        # === VOLATILITY CHECK (sync REST call — run in executor to avoid blocking) ===
        import asyncio
        loop = asyncio.get_event_loop()
        vol = await loop.run_in_executor(None, get_btc_volatility, 30)
        if not vol['is_high']:
            return None

        # === FIND PENNY TOKENS ===
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))

        if not up_book or not down_book:
            return None

        # Check both sides for penny prices
        candidates = []

        # UP side
        up_ask = up_book['best_ask']
        if self.MIN_PENNY_PRICE <= up_ask <= self.MAX_PENNY_PRICE:
            candidates.append({
                'direction': 'UP',
                'token_id': market['up_token_id'],
                'ask_price': up_ask,
                'depth': up_book.get('ask_depth', 0),
                'potential_return': (1.0 / up_ask) - 1,  # e.g., 1/0.03 - 1 = 32x
            })

        # DOWN side
        down_ask = down_book['best_ask']
        if self.MIN_PENNY_PRICE <= down_ask <= self.MAX_PENNY_PRICE:
            candidates.append({
                'direction': 'DOWN',
                'token_id': market['down_token_id'],
                'ask_price': down_ask,
                'depth': down_book.get('ask_depth', 0),
                'potential_return': (1.0 / down_ask) - 1,
            })

        if not candidates:
            return None

        # Pick the BEST candidate (cheapest = highest upside)
        best = min(candidates, key=lambda c: c['ask_price'])

        # Check depth
        if best['depth'] < self.MIN_DEPTH:
            return None

        # === EXPECTED VALUE CALCULATION ===
        # Real probability is ~1.5x the implied probability (from trader data)
        implied_prob = best['ask_price']  # Price = implied probability
        real_prob = implied_prob * 1.5     # Edge: 1.5x higher actual probability

        # EV = (real_prob * payout) - bet_size
        # payout = $1, bet = ask_price
        ev = (real_prob * 1.0) - best['ask_price']

        # Only trade if EV is positive or very close (within the 1.5x edge)
        if ev < -0.01:  # Allow slight negative EV (the 1.5x is conservative)
            return None

        # Confidence: higher vol = more confidence, cheaper = more confidence
        vol_boost = min(1.0, vol['vol_index'] / 5.0)
        price_boost = min(1.0, (self.MAX_PENNY_PRICE - best['ask_price']) / self.MAX_PENNY_PRICE)
        confidence = 0.30 + vol_boost * 0.3 + price_boost * 0.2

        # Boost if max single candle move was significant
        if vol['max_move'] > 0.3:  # 0.3% move in a single minute
            confidence = min(0.85, confidence + 0.1)

        potential_x = best['potential_return']

        rationale = (
            f"🎰 PENNY SNIPER: {coin} {best['direction']} @ {best['ask_price']:.2f}¢ "
            f"→ potential {potential_x:.0f}x return\n"
            f"  Volatility: {vol['vol_index']:.1f} "
            f"({'🔥 HIGH' if vol['vol_index'] > 3.5 else '⚡ ELEVATED'})\n"
            f"  Max 1m move: {vol['max_move']:.2f}% | "
            f"Avg move: {vol['avg_move']:.3f}%\n"
            f"  Implied prob: {implied_prob:.1%} → "
            f"Est. real prob: {real_prob:.1%} (1.5x edge)\n"
            f"  EV per bet: {ev:+.3f} | "
            f"Time: {seconds_remaining}s remaining"
        )

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=timeframe,
            direction=best['direction'],
            token_id=best['token_id'],
            market_id=market['market_id'],
            entry_price=best['ask_price'],
            confidence=confidence,
            rationale=rationale,
            metadata={
                'vol_index': vol['vol_index'],
                'max_move': vol['max_move'],
                'implied_prob': implied_prob,
                'real_prob_estimate': real_prob,
                'ev_per_bet': ev,
                'potential_return_x': potential_x,
                'is_penny_bet': True,
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        """Best on short timeframes where volatility causes wild swings."""
        return [1, 5]
