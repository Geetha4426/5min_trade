"""
Oracle Arbitrage Strategy v2 — Chainlink Delay Exploit

THE CORE EDGE: Polymarket settles crypto markets using Chainlink oracle
data which lags Binance/spot by ~60 seconds. This strategy:

1. Gets real-time Binance price momentum (velocity + acceleration + RSI)
2. Gets order flow (taker buy/sell pressure)
3. Calculates cross-exchange divergence (Binance implied prob vs Polymarket)
4. Combines with VWAP momentum
5. Multi-confirmation: requires 2+ signals agreeing before entry
6. Timing-aware: stronger signals closer to expiry (more certainty)

This is the #1 profit strategy when tuned correctly.
"""

from typing import Dict, List, Optional
from config import Config
from strategies.base_strategy import BaseStrategy, TradeSignal
from data.binance_signals import get_full_signal_analysis


class OracleArbStrategy(BaseStrategy):
    """
    Advanced oracle arbitrage using Binance signals + Chainlink delay.
    
    Uses 4 weighted signals:
      - Price Momentum (30%) — velocity, acceleration, RSI
      - Cross-Exchange Divergence (25%) — THE oracle delay exploit
      - Order Flow (20%) — taker buy/sell pressure
      - Volume-Weighted Momentum (15%) — VWAP distance    
      - Time Pressure (10%) — edge grows near expiry
    """

    name = "oracle_arb"
    description = "Exploits Chainlink oracle delay with 4 Binance signals"

    # Minimum confidence to generate a signal
    MIN_CONFIDENCE = 0.35

    # Minimum edge (score difference between UP and DOWN)
    MIN_EDGE = 0.10

    # Don't trade in final 20 seconds (settlement uncertainty)
    MIN_SECONDS = 20

    # Don't trade too early (need momentum to develop)
    MAX_SECONDS_FOR_STRONG_SIGNAL = 240  # 4 minutes

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Analyze market using Binance signals + Chainlink delay.
        
        Returns TradeSignal if high-confidence divergence found.
        """
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob:
            return None

        coin = market['coin']

        # Don't trade too close to settlement
        if seconds_remaining < self.MIN_SECONDS:
            return None

        # Get current Polymarket prices
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))
        if not up_book or not down_book:
            return None

        poly_up_mid = (up_book['best_bid'] + up_book['best_ask']) / 2
        poly_down_mid = (down_book['best_bid'] + down_book['best_ask']) / 2

        # Sanity: skip if prices are extreme or sum is > 1
        if poly_up_mid <= 0.02 or poly_up_mid >= 0.98:
            return None
        if poly_up_mid + poly_down_mid > 1.05:
            return None  # Market already fully priced

        # === THE MAIN ENGINE ===
        # Get comprehensive Binance signal analysis
        analysis = get_full_signal_analysis(
            symbol=coin,
            polymarket_up_price=poly_up_mid,
            seconds_remaining=seconds_remaining,
        )

        direction = analysis['direction']
        confidence = analysis['confidence']
        edge = analysis['edge']
        aligned = analysis['aligned_signals']
        signals = analysis['signals']

        # Filter: skip weak signals
        if direction == 'NEUTRAL':
            return None
        if confidence < self.MIN_CONFIDENCE:
            return None
        if edge < self.MIN_EDGE:
            return None
        if not analysis.get('entry_recommended', False):
            return None

        # Determine which side to buy
        if direction == 'UP':
            token_id = market['up_token_id']
            entry_price = up_book['best_ask']
            entry_depth = up_book.get('ask_depth', 0)
            market_mid = poly_up_mid
            true_prob = signals['divergence']['binance_implied_prob']
            actual_edge = true_prob - poly_up_mid
        else:
            token_id = market['down_token_id']
            entry_price = down_book['best_ask']
            entry_depth = down_book.get('ask_depth', 0)
            market_mid = poly_down_mid
            true_prob = 1 - signals['divergence']['binance_implied_prob']
            actual_edge = true_prob - poly_down_mid

        # Skip if entry price is too high (diminishing returns)
        if entry_price >= 0.90:
            return None

        # Check ask depth — need at least $1 liquidity to fill
        if entry_depth < 1.0:
            return None

        # Build detailed rationale
        momentum = signals['momentum']
        divergence = signals['divergence']
        flow = signals['order_flow']

        rationale = (
            f"🎯 ORACLE ARB v2: {coin} {direction} "
            f"[{aligned}/4 signals aligned]\n"
            f"  Binance: ${divergence.get('binance_price', 0):,.2f} "
            f"({divergence.get('price_change_pct', 0):+.2f}%)\n"
            f"  Momentum: {momentum['direction']} "
            f"(vel={momentum['velocity']:+.3f}%/min, RSI={momentum['rsi']:.0f})\n"
            f"  Divergence: {divergence['divergence']:+.3f} "
            f"({divergence['opportunity']})\n"
            f"  Flow: {flow['direction']} "
            f"(buy_pressure={flow['buy_pressure']:.1%})\n"
            f"  Edge: {actual_edge:+.1%} | "
            f"Conf: {confidence:.0%} | "
            f"Time: {seconds_remaining}s"
        )

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=market['timeframe'],
            direction=direction,
            token_id=token_id,
            market_id=market['market_id'],
            entry_price=entry_price,
            confidence=confidence,
            rationale=rationale,
            metadata={
                'binance_price': divergence.get('binance_price', 0),
                'price_change_pct': divergence.get('price_change_pct', 0),
                'divergence': divergence.get('divergence', 0),
                'true_prob': true_prob,
                'market_mid': market_mid,
                'actual_edge': actual_edge,
                'momentum_velocity': momentum['velocity'],
                'momentum_rsi': momentum['rsi'],
                'buy_pressure': flow['buy_pressure'],
                'aligned_signals': aligned,
                'scores': analysis['scores'],
                'spread_pct': 2 * (entry_price - market_mid) / market_mid * 100 if market_mid > 0 else 0,
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        """Works on ALL timeframes — the Chainlink delay is universal."""
        return [1, 5, 15, 30, 60]
