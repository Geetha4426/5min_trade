"""
Dynamic Strategy Picker — ALL-IN Edition with Win Rate Learning

Runs ALL 15 strategies on every scan. Trades CONTINUOUSLY.
Uses the BalanceManager to adjust aggression based on balance tier.

NEW: Strategy Win Rate Tracking (inspired by ThinkEnigmatic/bot-arena
and PolyFlup's Bayesian A/B testing). Tracks which strategies actually
win/lose over time and applies a performance multiplier to confidence scores.

Strategies (15 total):
1. Cross-Timeframe Arb — GUARANTEED profit on overlapping markets
2. YES+NO Arb — guaranteed profit arbitrage
3. Cheap Outcome Hunter — buy at 1-8 cents for 100x
4. Momentum Reversal — catch big dips
5. Oracle Arb — Binance Chainlink delay exploit
6. Volatility Penny Sniper — 2-5¢ bets for 20-50x during high vol
7. Time Decay — near-expiry farming
8. Trend Follower — ride momentum
9. Straddle — buy both sides during volatility
10. Spread Scalper — profit from wide spreads
11. Mid-Price Sniper — buy underpriced mid-range outcomes
12. Mean Reversion Scalper — buy after crash, catch the bounce (NEW)
13. Spike Fade — buy cheap opposite side when one spikes (NEW)
14. Expiry Rush — aggressive last-60s momentum plays (NEW)
15. Binance Momentum Sniper — catch Binance→Polymarket price lag (NEW)
"""

from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal
from strategies.flash_crash import CheapOutcomeHunter, MomentumReversal
from strategies.oracle_arb import OracleArbStrategy
from strategies.yes_no_arb import YesNoArbStrategy
from strategies.time_decay import TimeDecayStrategy
from strategies.continuous import (
    TrendFollower, StraddleStrategy, SpreadScalper, MidPriceSniper
)
from strategies.cross_timeframe_arb import CrossTimeframeArbStrategy
from strategies.volatility_penny_sniper import VolatilityPennySniperStrategy
from strategies.probability_closer import ProbabilityCloserStrategy
from strategies.swing_scalpers import (
    MeanReversionScalper, SpikeFade, ExpiryRush, BinanceMomentumSniper
)


class StrategyTracker:
    """Tracks win/loss record per strategy for performance-based selection.
    
    Inspired by ThinkEnigmatic/bot-arena's Bayesian learning and PolyFlup's
    A/B confidence testing. Simple but effective: strategies with better
    track records get a confidence boost, underperformers get penalized.
    """

    MIN_TRADES_FOR_ADJUSTMENT = 5  # Need at least 5 trades before adjusting
    MAX_BOOST = 0.15               # Max confidence boost for best strategy
    MAX_PENALTY = -0.10            # Max penalty for worst strategy

    def __init__(self):
        # {strategy_name: {'wins': int, 'losses': int, 'total_pnl': float}}
        self.records: Dict[str, Dict] = {}

    def record(self, strategy_name: str, won: bool, pnl: float = 0.0):
        """Record a trade result for a strategy."""
        if strategy_name not in self.records:
            self.records[strategy_name] = {'wins': 0, 'losses': 0, 'total_pnl': 0.0}
        rec = self.records[strategy_name]
        if won:
            rec['wins'] += 1
        else:
            rec['losses'] += 1
        rec['total_pnl'] += pnl

    def get_adjustment(self, strategy_name: str) -> float:
        """Get confidence adjustment for a strategy based on track record.
        
        Returns a value between MAX_PENALTY and MAX_BOOST.
        Strategies with too few trades get 0 (no adjustment).
        """
        rec = self.records.get(strategy_name)
        if not rec:
            return 0.0
        total = rec['wins'] + rec['losses']
        if total < self.MIN_TRADES_FOR_ADJUSTMENT:
            return 0.0

        win_rate = rec['wins'] / total
        # Center at 50% — above gets boost, below gets penalty
        # Scale: 80% win rate → +0.15, 20% win rate → -0.10
        if win_rate >= 0.5:
            return self.MAX_BOOST * (win_rate - 0.5) / 0.5
        else:
            return self.MAX_PENALTY * (0.5 - win_rate) / 0.5

    def get_stats(self) -> Dict[str, Dict]:
        """Get all strategy stats for display."""
        stats = {}
        for name, rec in self.records.items():
            total = rec['wins'] + rec['losses']
            stats[name] = {
                'wins': rec['wins'],
                'losses': rec['losses'],
                'total': total,
                'win_rate': (rec['wins'] / total * 100) if total > 0 else 0,
                'pnl': rec['total_pnl'],
                'adjustment': self.get_adjustment(name),
            }
        return stats


class DynamicPicker(BaseStrategy):
    """
    Master strategy — evaluates ALL strategies, returns the best signal.
    Respects balance tier when filtering.
    Applies win rate learning to boost proven strategies.
    """

    name = "dynamic"
    description = "All 15 strategies — trades continuously, adapts to balance + track record"

    def __init__(self):
        self.strategies: List[BaseStrategy] = [
            CrossTimeframeArbStrategy(),  # Guaranteed cross-TF arb
            YesNoArbStrategy(),           # Guaranteed profit
            ProbabilityCloserStrategy(),  # Near-expiry high-conviction
            MeanReversionScalper(),       # Buy after crash → bounce (NEW)
            SpikeFade(),                  # Buy cheap opposite side (NEW)
            ExpiryRush(),                 # Last-60s momentum plays (NEW)
            BinanceMomentumSniper(),      # Binance→Poly price lag (NEW)
            CheapOutcomeHunter(),         # Lottery tickets
            MomentumReversal(),           # Catch dips
            OracleArbStrategy(),          # Chainlink delay exploit
            VolatilityPennySniperStrategy(),  # High-vol penny bets
            TrendFollower(),              # Ride momentum
            MidPriceSniper(),             # Underpriced outcomes
            StraddleStrategy(),           # Volatility play
            SpreadScalper(),              # Bid-ask profit
            TimeDecayStrategy(),          # Near-expiry
        ]
        # Strategy win rate tracker (learns from results)
        self.tracker = StrategyTracker()

    async def analyze(self, market: Dict, context: Dict,
                       balance_prefs: Dict = None) -> Optional[TradeSignal]:
        """
        Run ALL strategies. Return the best signal.
        Optionally filter by balance tier preferences.
        """
        signals: List[TradeSignal] = []
        min_confidence = 0.25  # Low bar — trade more often

        # Apply balance tier preferences if available
        enabled = None
        disabled = []
        if balance_prefs:
            enabled_cfg = balance_prefs.get('enabled', 'all')
            if enabled_cfg != 'all':
                enabled = enabled_cfg
            disabled = balance_prefs.get('disabled', [])
            min_confidence = balance_prefs.get('min_confidence', 0.25)

        for strategy in self.strategies:
            # Skip disabled strategies
            if strategy.name in disabled:
                continue
            # Only run enabled strategies (if specified)
            if enabled and strategy.name not in enabled:
                continue

            try:
                signal = await strategy.analyze(market, context)
                if signal and signal.confidence >= min_confidence:
                    signals.append(signal)
            except Exception as e:
                continue

        if not signals:
            return None

        # Priority boost for guaranteed-profit strategies
        for s in signals:
            sig_type = s.metadata.get('type', '')
            if sig_type == 'cross_timeframe_arb':
                s.confidence = min(0.99, s.confidence + 0.25)  # HIGHEST priority
            elif sig_type == 'both_sides':
                s.confidence = min(0.99, s.confidence + 0.20)
            elif sig_type == 'cheap_single':
                s.confidence = min(0.95, s.confidence + 0.10)
            elif s.metadata.get('is_penny_bet'):
                s.confidence = min(0.85, s.confidence + 0.05)  # Don't over-prioritize
            elif sig_type == 'prob_closer':
                s.confidence = min(0.95, s.confidence + 0.15)  # High priority for safe returns
            elif sig_type == 'mean_reversion':
                s.confidence = min(0.95, s.confidence + 0.12)  # High priority: proven bounces
            elif sig_type == 'spike_fade':
                s.confidence = min(0.93, s.confidence + 0.10)  # High priority: fade extremes
            elif sig_type == 'expiry_rush':
                s.confidence = min(0.95, s.confidence + 0.12)  # Urgent: time-sensitive
            elif sig_type == 'binance_momentum':
                s.confidence = min(0.94, s.confidence + 0.12)  # High priority: real data edge
            elif sig_type in ('trend', 'mid_sniper'):
                s.confidence = min(0.90, s.confidence + 0.05)

            # ── Apply strategy win rate adjustment (learning) ──
            track_adj = self.tracker.get_adjustment(s.strategy)
            if track_adj != 0:
                s.confidence = max(0.05, min(0.99, s.confidence + track_adj))

        # Sort by confidence
        signals.sort(key=lambda s: s.confidence, reverse=True)
        best = signals[0]
        best.metadata['alternatives'] = len(signals) - 1
        best.metadata['strategies_checked'] = len(self.strategies)
        return best

    def get_suitable_timeframes(self) -> List[int]:
        return [1, 5, 15, 30, 60]

    def get_all_strategies(self) -> List[BaseStrategy]:
        return self.strategies
