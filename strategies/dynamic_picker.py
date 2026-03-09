"""
Dynamic Strategy Picker — ALL-IN Edition with Win Rate Learning

Runs ALL 16 strategies on every scan. Trades CONTINUOUSLY.
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
    MeanReversionScalper, SpikeFade, ExpiryRush, BinanceMomentumSniper,
    OrderbookImbalance
)
from strategies.early_mover import EarlyMoverStrategy
from strategies.quant_strategies import (
    QuantEdgeStrategy, MicroPriceSniperStrategy, InformedFlowDetector
)


class StrategyTracker:
    """Tracks win/loss record per strategy with GRADUAL deprioritization.

    Key design:
    - Penalty grows slowly: -0.02 per consecutive loss, capped at -0.12
    - Boost grows slowly: +0.03 per consecutive win, capped at +0.15
    - A single win resets the loss streak (and vice versa)
    - RESET MECHANISM: after 50 scans without being picked, the strategy
      gets a "trial run" — adjustment set to 0 for one signal. If it wins,
      it's rehabilitated. If it loses, penalty resumes from previous level.
    - DECAY: every 20 trades (global), oldest result decays by 50%,
      preventing permanent blacklisting from early bad luck.
    """

    MAX_BOOST = 0.15
    MAX_PENALTY = -0.12
    LOSS_STEP = -0.02          # Penalty per consecutive loss
    WIN_STEP = 0.03            # Boost per consecutive win
    MIN_TRADES = 3             # Start adjusting after 3 trades
    RESET_SCANS = 50           # Trial reset after 50 idle scans
    DECAY_INTERVAL = 20        # Decay old results every 20 global trades

    def __init__(self):
        self.records: Dict[str, Dict] = {}
        self._global_trades = 0
        self._idle_scans: Dict[str, int] = {}   # strategy -> scans since last picked
        self._on_trial: Dict[str, bool] = {}     # strategy -> currently on trial reset

    def record(self, strategy_name: str, won: bool, pnl: float = 0.0):
        """Record a trade result for a strategy."""
        if strategy_name not in self.records:
            self.records[strategy_name] = {
                'wins': 0, 'losses': 0, 'total_pnl': 0.0,
                'win_streak': 0, 'loss_streak': 0,
            }
        rec = self.records[strategy_name]
        if won:
            rec['wins'] += 1
            rec['win_streak'] += 1
            rec['loss_streak'] = 0
        else:
            rec['losses'] += 1
            rec['loss_streak'] += 1
            rec['win_streak'] = 0
        rec['total_pnl'] += pnl

        # Reset idle scan counter (strategy was just used)
        self._idle_scans[strategy_name] = 0

        # Handle trial outcome
        if self._on_trial.get(strategy_name):
            self._on_trial[strategy_name] = False
            if won:
                # Trial success — rehabilitate: halve old loss streak
                rec['loss_streak'] = max(0, rec['loss_streak'] // 2)

        # Global decay: every DECAY_INTERVAL trades, shrink all records
        self._global_trades += 1
        if self._global_trades % self.DECAY_INTERVAL == 0:
            self._decay_all()

    def _decay_all(self):
        """Decay old results by 50% so strategies aren't permanently killed."""
        for rec in self.records.values():
            rec['wins'] = max(0, int(rec['wins'] * 0.5))
            rec['losses'] = max(0, int(rec['losses'] * 0.5))

    def mark_scanned(self, strategy_name: str):
        """Call once per scan cycle for strategies not picked."""
        self._idle_scans[strategy_name] = self._idle_scans.get(strategy_name, 0) + 1

    def get_adjustment(self, strategy_name: str) -> float:
        """Get confidence adjustment — gradual, streak-based.

        Loss streak:  -0.02 * streak  (capped -0.12)
        Win streak:   +0.03 * streak  (capped +0.15)
        On trial:     0.0 (fresh chance)
        """
        # Trial reset: if idle too long, give a free pass
        if self._idle_scans.get(strategy_name, 0) >= self.RESET_SCANS:
            self._on_trial[strategy_name] = True
            self._idle_scans[strategy_name] = 0
            return 0.0

        if self._on_trial.get(strategy_name):
            return 0.0

        rec = self.records.get(strategy_name)
        if not rec:
            return 0.0
        total = rec['wins'] + rec['losses']
        if total < self.MIN_TRADES:
            return 0.0

        loss_streak = rec.get('loss_streak', 0)
        win_streak = rec.get('win_streak', 0)

        if loss_streak > 0:
            return max(self.MAX_PENALTY, self.LOSS_STEP * loss_streak)
        elif win_streak > 0:
            return min(self.MAX_BOOST, self.WIN_STEP * win_streak)
        else:
            # No active streak — use win rate for mild adjustment
            win_rate = rec['wins'] / total
            if win_rate >= 0.5:
                return min(0.05, 0.05 * (win_rate - 0.5) / 0.5)
            else:
                return max(-0.03, -0.03 * (0.5 - win_rate) / 0.5)

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
                'loss_streak': rec.get('loss_streak', 0),
                'win_streak': rec.get('win_streak', 0),
                'on_trial': self._on_trial.get(name, False),
                'idle_scans': self._idle_scans.get(name, 0),
            }
        return stats


class DynamicPicker(BaseStrategy):
    """
    Master strategy — evaluates ALL strategies, returns the best signal.
    Respects balance tier when filtering.
    Applies win rate learning to boost proven strategies.
    """

    name = "dynamic"
    description = "All 21 strategies — trades continuously, adapts to balance + track record"

    def __init__(self):
        self.strategies: List[BaseStrategy] = [
            CrossTimeframeArbStrategy(),  # Guaranteed cross-TF arb
            YesNoArbStrategy(),           # Guaranteed profit
            ProbabilityCloserStrategy(),  # Near-expiry high-conviction
            MeanReversionScalper(),       # Buy after crash → bounce (NEW)
            SpikeFade(),                  # Buy cheap opposite side (NEW)
            ExpiryRush(),                 # Last-60s momentum plays (NEW)
            BinanceMomentumSniper(),      # Binance→Poly price lag (NEW)
            EarlyMoverStrategy(),         # Buy cheap side on Binance reversal (NEW)
            CheapOutcomeHunter(),         # Lottery tickets
            MomentumReversal(),           # Catch dips
            OracleArbStrategy(),          # Chainlink delay exploit
            VolatilityPennySniperStrategy(),  # High-vol penny bets
            TrendFollower(),              # Ride momentum
            MidPriceSniper(),             # Underpriced outcomes
            StraddleStrategy(),           # Volatility play
            SpreadScalper(),              # Bid-ask profit
            TimeDecayStrategy(),          # Near-expiry
            OrderbookImbalance(),         # Depth-ratio directional signal (NEW)
            QuantEdgeStrategy(),          # Multi-factor quant screening (NEW v2)
            MicroPriceSniperStrategy(),   # MicroPrice divergence plays (NEW v2)
            InformedFlowDetector(),       # Kyle's Lambda whale detection (NEW v2)
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
                s.confidence = min(0.82, s.confidence + 0.05)  # Mild boost, stays under SEED 0.90
            elif s.metadata.get('is_penny_bet'):
                s.confidence = min(0.82, s.confidence + 0.05)  # Same: don't push pennies into SEED
            elif sig_type == 'prob_closer':
                s.confidence = min(0.95, s.confidence + 0.15)  # High priority for safe returns
            elif sig_type == 'mean_reversion':
                s.confidence = min(0.95, s.confidence + 0.12)  # High priority: proven bounces
            elif sig_type == 'spike_fade':
                s.confidence = min(0.93, s.confidence + 0.10)  # High priority: fade extremes
            elif sig_type == 'expiry_rush':
                s.confidence = min(0.97, s.confidence + 0.18)  # TOP priority: proven winner
            elif sig_type == 'binance_momentum':
                s.confidence = min(0.94, s.confidence + 0.12)  # High priority: real data edge
            elif sig_type == 'time_decay':
                s.confidence = min(0.95, s.confidence + 0.08)  # Binance-confirmed near-expiry
            elif sig_type == 'early_mover':
                s.confidence = min(0.95, s.confidence + 0.10)  # Reversal-confirmed cheap bet
            elif sig_type in ('trend', 'mid_sniper'):
                s.confidence = min(0.90, s.confidence + 0.05)
            elif sig_type == 'book_imbalance':
                s.confidence = min(0.93, s.confidence + 0.08)  # Orderbook signal
            elif sig_type == 'quant_edge':
                s.confidence = min(0.95, s.confidence + 0.12)  # Full quant screening
            elif sig_type == 'microprice_sniper':
                s.confidence = min(0.93, s.confidence + 0.10)  # Hidden volume pressure
            elif sig_type == 'informed_flow':
                s.confidence = min(0.93, s.confidence + 0.10)  # Whale piggyback

            # ── Apply strategy win rate adjustment (learning) ──
            track_adj = self.tracker.get_adjustment(s.strategy)
            if track_adj != 0:
                s.confidence = max(0.05, min(0.99, s.confidence + track_adj))

        # Sort by confidence
        signals.sort(key=lambda s: s.confidence, reverse=True)
        best = signals[0]
        best.metadata['alternatives'] = len(signals) - 1
        best.metadata['strategies_checked'] = len(self.strategies)

        # Track idle scans for strategies NOT picked (for reset mechanism)
        for s in signals[1:]:
            self.tracker.mark_scanned(s.strategy)

        # ── Add spread info for spread guard in execute_signal ──
        # If the strategy didn't compute spread, fetch orderbook here.
        if 'spread_pct' not in best.metadata and context.get('clob'):
            try:
                token_id = best.token_id
                if '|' not in token_id:  # Skip BOTH-side signals
                    book = context['clob'].get_orderbook(token_id)
                    if book:
                        bid = book.get('best_bid', 0)
                        ask = book.get('best_ask', 0)
                        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
                        if mid > 0:
                            best.metadata['spread_pct'] = (ask - bid) / mid * 100
            except Exception:
                pass

        return best

    def get_suitable_timeframes(self) -> List[int]:
        return [1, 5, 15, 30, 60]

    def get_all_strategies(self) -> List[BaseStrategy]:
        return self.strategies
