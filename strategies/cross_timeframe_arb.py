"""
Cross-Timeframe Arbitrage Strategy — RISK-AWARE (Fee + Dead-Zone)

THE EDGE: When a 15-minute market has ~5 minutes left, a NEW 5-minute
market opens for the same coin. Because the 15-min market has already
"traveled" 10 minutes of price action, its UP/DOWN pricing diverges
from the fresh 5-min market.

RISK (Dead Zone): Each market has a DIFFERENT price-to-beat (Chainlink
oracle price at its own open time). If the final price lands between
the two thresholds, BOTH legs lose. This is NOT a guaranteed arb.

Example dead zone (BTC):
  15m opened at 4:30 → threshold $67,959  (Chainlink at 4:30)
  5m  opened at 4:40 → threshold $68,003  (Chainlink at 4:40)
  Gap = $44 — if final price is 67,959–68,003, BOTH legs lose.

STRATEGY:
1. Scan for overlapping markets: same coin, different timeframes
2. Estimate the dead zone using Binance klines (threshold gap)
3. Calculate expected value factoring in dead-zone probability
4. Collect ALL candidates per scan, rank by EV, pick the best one
5. Only signal when EV > minimum threshold
"""

import re
import time
import math
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal

# Parse start epoch from market slugs like "btc-updown-15m-1772875200"
_SLUG_RE = re.compile(
    r'(?:btc|eth|sol|xrp)-updown-(?:\d+)m-(?P<epoch>\d+)', re.I
)


class CrossTimeframeArbStrategy(BaseStrategy):
    """
    Cross-timeframe arbitrage with dead-zone risk awareness.

    Buys opposite sides on overlapping markets (e.g., 15-min UP + 5-min DOWN).
    Uses Binance price data to estimate the gap between each market's
    price-to-beat threshold and calculates expected value before signaling.
    Collects all candidates per scan and returns only the best one.
    """

    name = "cross_tf_arb"
    description = "Cross-timeframe arbitrage on overlapping markets"

    # Maximum combined cost to enter (lower = more profit)
    MAX_COMBINED_COST = 0.95

    # Minimum expected value per share (after dead-zone risk + fees)
    MIN_EV = 0.02  # 2¢ minimum EV

    # Legacy min profit (used as floor before EV calculation)
    MIN_PROFIT = 0.03  # 3¢ = ~3% net of fees

    # Maximum dead-zone width as % of asset price — skip if wider
    MAX_DEAD_ZONE_PCT = 0.12  # 0.12% (≈$82 for BTC, ≈$3 for ETH)

    # Estimated taker fee rate per leg — dynamic per price
    @staticmethod
    def _dynamic_fee(price: float) -> float:
        """Polymarket effective fee rate: 0.25 × p × (1-p)².
        Peak ~3.7% at p≈0.33. Settlement is FREE (0%)."""
        p = max(0.001, min(0.999, price))
        q = 1.0 - p
        return 0.25 * p * q * q

    # Minimum depth on both sides (avoid thin markets)
    MIN_DEPTH = 2.0  # $2

    # Expected order size per leg — used for fillable price calculation
    EXPECTED_LEG_SIZE = 1.0  # $1 per leg in SEED/PLANT

    @staticmethod
    def _fillable_price(book: dict, size_usd: float) -> tuple:
        """Calculate the price needed to fill `size_usd` from ask side.
        
        Walks the orderbook asks to find the worst price we'd pay
        to fully fill our order. Returns (fillable_price, fillable).
        If not enough depth, returns (worst_ask_seen, False).
        """
        asks = book.get('asks', [])  # [(price, size), ...] sorted ascending
        if not asks:
            return (book.get('best_ask', 1.0), False)
        remaining = size_usd
        worst_price = asks[0][0]
        for price, shares in asks:
            level_value = price * shares
            worst_price = price
            remaining -= level_value
            if remaining <= 0:
                return (price, True)
        return (worst_price, False)

    # The 15-min market should have 3-7 minutes remaining (overlap window)
    OVERLAP_MIN_SECS = 120   # 2 min — need enough execution time
    OVERLAP_MAX_SECS = 420   # 7 min — the 5-min market just opened

    # Track discovered pairs to avoid duplicate signals (instance-level)
    def __init__(self):
        self._active_pairs = {}
        self._depth_skip_log = {}  # coin -> last_log_time for throttling

    def _log_depth_skip(self, coin: str, reason: str):
        """Log depth skip once per 30s per coin to avoid spam."""
        import time
        now = time.time()
        last = self._depth_skip_log.get(coin, 0)
        if now - last > 30:
            self._depth_skip_log[coin] = now
            print(f"💧 Depth skip: {coin} cross_tf_arb — {reason}", flush=True)

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        """
        Check for cross-timeframe arbitrage.

        This strategy needs access to ALL markets, not just one.
        It stores market data and looks for overlapping pairs.

        We are called once per market. We accumulate markets and
        check for pairs each time.
        """
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob or seconds_remaining <= 0:
            return None

        # In SEED/PLANT mode, require higher profit margin for arb
        balance_mgr = context.get('balance_mgr')
        self._seed_mode = balance_mgr and getattr(balance_mgr, 'mode_name', '') in ('seed', 'plant')

        coin = market['coin']
        timeframe = market['timeframe']
        market_id = market['market_id']

        # Store this market for pair matching
        key = f"{coin}_{timeframe}_{market_id}"
        self._active_pairs[key] = {
            'market': market,
            'seconds_remaining': seconds_remaining,
            'context': context,
        }

        # Clean stale entries (expired markets)
        stale_keys = [
            k for k, v in self._active_pairs.items()
            if v['seconds_remaining'] <= 0
        ]
        for k in stale_keys:
            del self._active_pairs[k]

        # Now scan for pairs: find overlapping markets for the SAME coin
        # We want: longer_tf market with ~5 min left + shorter_tf fresh market
        signal = await self._find_arb_pair(coin, clob, context)
        return signal

    async def _find_arb_pair(self, coin: str, clob, context: Dict) -> Optional[TradeSignal]:
        """
        Find the BEST arb pair for the given coin across timeframes.

        Collects all valid candidates, scores by expected value,
        and returns only the single best one.
        """
        # Gather all active entries for this coin
        coin_entries = {
            k: v for k, v in self._active_pairs.items()
            if k.startswith(f"{coin}_")
        }

        if len(coin_entries) < 2:
            return None

        # Sort by timeframe
        sorted_entries = sorted(
            coin_entries.values(),
            key=lambda e: e['market']['timeframe']
        )

        # Collect ALL valid candidates
        candidates = []
        for i, longer in enumerate(sorted_entries):
            for shorter in sorted_entries[:i]:
                signal = await self._check_pair(longer, shorter, clob, context)
                if signal:
                    candidates.append(signal)

        if not candidates:
            return None

        # Rank by expected value (stored in metadata)
        candidates.sort(key=lambda s: s.metadata.get('expected_value', 0), reverse=True)

        best = candidates[0]
        if len(candidates) > 1:
            ev = best.metadata.get('expected_value', 0)
            print(f"🏆 {coin} cross_tf_arb: picked best of {len(candidates)} "
                  f"candidates (EV={ev:.3f})", flush=True)

        return best

    async def _check_pair(self, longer_entry, shorter_entry, clob, context: Dict) -> Optional[TradeSignal]:
        """
        Check if a longer/shorter market pair has an arb opportunity.

        Now risk-aware: estimates the dead zone between the two markets'
        price-to-beat thresholds and calculates expected value.
        """
        longer_mkt = longer_entry['market']
        shorter_mkt = shorter_entry['market']
        longer_secs = longer_entry['seconds_remaining']
        shorter_secs = shorter_entry['seconds_remaining']

        # Check overlap window: longer market should have 2-7 min left
        if longer_secs < self.OVERLAP_MIN_SECS or longer_secs > self.OVERLAP_MAX_SECS:
            return None

        # Shorter market should be fresh (most of its time remaining)
        shorter_tf_secs = shorter_mkt['timeframe'] * 60
        freshness = shorter_secs / shorter_tf_secs if shorter_tf_secs > 0 else 0
        if freshness < 0.60:  # Should have at least 60% time remaining
            return None

        # === DEAD ZONE ESTIMATION ===
        binance_feed = context.get('binance_feed')
        dz = self._estimate_dead_zone(longer_mkt, shorter_mkt, binance_feed)

        # If dead zone is too wide, skip entirely
        if dz and dz['pct'] > self.MAX_DEAD_ZONE_PCT:
            coin = longer_mkt['coin']
            self._log_depth_skip(
                coin,
                f"dead zone {dz['width']:.2f} ({dz['pct']:.3f}%) > "
                f"{self.MAX_DEAD_ZONE_PCT:.2f}% max"
            )
            return None

        # Get orderbooks for both markets
        longer_up_book = clob.get_orderbook(longer_mkt.get('up_token_id', ''))
        longer_dn_book = clob.get_orderbook(longer_mkt.get('down_token_id', ''))
        shorter_up_book = clob.get_orderbook(shorter_mkt.get('up_token_id', ''))
        shorter_dn_book = clob.get_orderbook(shorter_mkt.get('down_token_id', ''))

        if not all([longer_up_book, longer_dn_book, shorter_up_book, shorter_dn_book]):
            return None

        # === FIND THE BEST COMBINATION (FEE-AWARE + DEPTH-AWARE) ===
        ctx = longer_entry.get('context', {})
        balance_mgr = ctx.get('balance_mgr')
        leg_size = self.EXPECTED_LEG_SIZE
        if balance_mgr:
            leg_size = max(leg_size, balance_mgr.get_position_size(0.95) / 2)

        # Fillable prices: realistic prices we'd actually get filled at
        a_p1_fill, a_p1_ok = self._fillable_price(longer_up_book, leg_size)
        a_p2_fill, a_p2_ok = self._fillable_price(shorter_dn_book, leg_size)
        b_p1_fill, b_p1_ok = self._fillable_price(longer_dn_book, leg_size)
        b_p2_fill, b_p2_ok = self._fillable_price(shorter_up_book, leg_size)

        # Option A: Buy UP on longer + DOWN on shorter
        combo_a_cost = a_p1_fill + a_p2_fill
        combo_a_fee_1 = self._dynamic_fee(a_p1_fill)
        combo_a_fee_2 = self._dynamic_fee(a_p2_fill)
        combo_a_fees = (a_p1_fill * combo_a_fee_1 + a_p2_fill * combo_a_fee_2)
        combo_a_profit = 1.0 - combo_a_cost - combo_a_fees
        combo_a_fillable = a_p1_ok and a_p2_ok

        # Option B: Buy DOWN on longer + UP on shorter
        combo_b_cost = b_p1_fill + b_p2_fill
        combo_b_fee_1 = self._dynamic_fee(b_p1_fill)
        combo_b_fee_2 = self._dynamic_fee(b_p2_fill)
        combo_b_fees = (b_p1_fill * combo_b_fee_1 + b_p2_fill * combo_b_fee_2)
        combo_b_profit = 1.0 - combo_b_cost - combo_b_fees
        combo_b_fillable = b_p1_ok and b_p2_ok

        # === CALCULATE EXPECTED VALUE FOR EACH COMBO ===
        p_dead = dz['p_dead'] if dz else 0.05  # Conservative 5% default if no data
        dz_width = dz['width'] if dz else 0
        dz_pct = dz['pct'] if dz else 0

        # EV = (1-p_dead) * profit_if_win - p_dead * cost_if_lose
        # Simplifies to: EV = 1 - p_dead - combined_cost - fees
        combo_a_ev = (1 - p_dead) * combo_a_profit - p_dead * (combo_a_cost + combo_a_fees)
        combo_b_ev = (1 - p_dead) * combo_b_profit - p_dead * (combo_b_cost + combo_b_fees)

        # Pick the most profitable combination by EV
        if combo_a_fillable and not combo_b_fillable:
            pick_a = True
        elif combo_b_fillable and not combo_a_fillable:
            pick_a = False
        else:
            pick_a = combo_a_ev >= combo_b_ev

        if pick_a:
            best_combo = 'A'
            combined_cost = combo_a_cost
            profit = combo_a_profit
            ev = combo_a_ev
            total_fees = combo_a_fees
            both_fillable = combo_a_fillable
            primary_side = 'UP'
            primary_token = longer_mkt['up_token_id']
            primary_price = a_p1_fill
            hedge_side = 'DOWN'
            hedge_token = shorter_mkt['down_token_id']
            hedge_price = a_p2_fill
            primary_depth = longer_up_book.get('ask_depth', 0)
            hedge_depth = shorter_dn_book.get('ask_depth', 0)
        else:
            best_combo = 'B'
            combined_cost = combo_b_cost
            profit = combo_b_profit
            ev = combo_b_ev
            total_fees = combo_b_fees
            both_fillable = combo_b_fillable
            primary_side = 'DOWN'
            primary_token = longer_mkt['down_token_id']
            primary_price = b_p1_fill
            hedge_side = 'UP'
            hedge_token = shorter_mkt['up_token_id']
            hedge_price = b_p2_fill
            primary_depth = longer_dn_book.get('ask_depth', 0)
            hedge_depth = shorter_up_book.get('ask_depth', 0)

        # === FILTERS ===
        if combined_cost > self.MAX_COMBINED_COST:
            return None

        # SEED/PLANT: higher edge bar since capital is precious
        min_profit = 0.05 if getattr(self, '_seed_mode', False) else self.MIN_PROFIT
        if profit < min_profit:
            return None

        # EV must exceed minimum (accounts for dead zone risk)
        if ev < self.MIN_EV:
            coin = longer_mkt['coin']
            self._log_depth_skip(
                coin,
                f"EV too low: {ev:.3f} (profit={profit:.3f}, "
                f"p_dead={p_dead:.1%}, gap={dz_width:.1f})"
            )
            return None

        # Check liquidity — both total depth and fillability
        min_depth = min(primary_depth, hedge_depth)
        coin = longer_mkt['coin']
        if min_depth < self.MIN_DEPTH:
            self._log_depth_skip(coin, f"depth ${min_depth:.2f} < ${self.MIN_DEPTH:.2f} min")
            return None

        if not both_fillable:
            self._log_depth_skip(coin, f"not fillable at ${leg_size:.2f}/leg "
                                 f"(combo {best_combo}: primary={primary_depth:.2f} hedge={hedge_depth:.2f})")
            return None

        # Confidence scales with EV (not raw profit)
        confidence = min(0.99, 0.70 + ev * 8)

        pct_return = profit / combined_cost * 100
        ev_pct = ev / combined_cost * 100

        # Build rationale with dead-zone info
        dz_info = ""
        if dz and dz['width'] > 0:
            dz_info = (
                f"\n  ⚠️ Dead zone: ${dz['width']:.2f} gap ({dz['pct']:.3f}%), "
                f"P(both-lose)={p_dead:.1%}"
            )

        rationale = (
            f"🔗 CROSS-TF ARB: {coin} — EV {ev_pct:.1f}% "
            f"(raw {pct_return:.1f}%, risk-adj)\n"
            f"  Leg 1: {primary_side} on {longer_mkt['timeframe']}m @ "
            f"{primary_price:.2f}¢ ({longer_secs}s left)\n"
            f"  Leg 2: {hedge_side} on {shorter_mkt['timeframe']}m @ "
            f"{hedge_price:.2f}¢ (fresh)\n"
            f"  Combined: {combined_cost:.2f}¢ → Payout: $1.00 → "
            f"Profit: {profit:.2f}¢ | EV: {ev:.3f}¢{dz_info}\n"
            f"  Depth: ${min_depth:.0f}"
        )

        return TradeSignal(
            strategy=self.name,
            coin=coin,
            timeframe=longer_mkt['timeframe'],
            direction='BOTH',
            token_id=f"{primary_token}|{hedge_token}",
            market_id=f"{longer_mkt['market_id']}|{shorter_mkt['market_id']}",
            entry_price=combined_cost,
            confidence=confidence,
            rationale=rationale,
            metadata={
                'type': 'cross_timeframe_arb',
                'primary_side': primary_side,
                'primary_token': primary_token,
                'primary_price': primary_price,
                'primary_timeframe': longer_mkt['timeframe'],
                'hedge_side': hedge_side,
                'hedge_token': hedge_token,
                'hedge_price': hedge_price,
                'hedge_timeframe': shorter_mkt['timeframe'],
                'combined_cost': combined_cost,
                'profit_per_share': profit,
                'expected_value': ev,
                'pct_return': pct_return,
                'ev_pct': ev_pct,
                'p_dead_zone': p_dead,
                'dead_zone_width': dz_width,
                'dead_zone_pct': dz_pct,
                'min_depth': min_depth,
                'longer_secs_remaining': longer_secs,
                'shorter_secs_remaining': shorter_secs,
            }
        )

    def _estimate_dead_zone(self, longer_mkt: Dict, shorter_mkt: Dict,
                            binance_feed) -> Optional[Dict]:
        """
        Estimate the dead zone between two markets' price-to-beat thresholds.

        Each market's threshold = Chainlink price at its start epoch.
        Uses Binance 1m klines to approximate the Chainlink prices,
        then calculates gap width and probability of the final price
        landing in the dead zone.

        Returns dict with: width, pct, p_dead, lo, hi, current_price
        Or None if estimation fails (treat as moderate risk).
        """
        # Parse start epochs from event slugs
        longer_slug = longer_mkt.get('event_slug', '')
        shorter_slug = shorter_mkt.get('event_slug', '')

        m_longer = _SLUG_RE.search(longer_slug)
        m_shorter = _SLUG_RE.search(shorter_slug)

        if not m_longer or not m_shorter:
            return None

        epoch_longer = int(m_longer.group('epoch'))
        epoch_shorter = int(m_shorter.group('epoch'))
        time_gap_secs = abs(epoch_shorter - epoch_longer)

        if time_gap_secs == 0:
            # Same start time → same threshold → no dead zone
            return {'width': 0, 'pct': 0, 'p_dead': 0.0, 'lo': 0, 'hi': 0,
                    'current_price': 0}

        coin = longer_mkt['coin']

        # Get Binance 1m klines covering the gap period
        from data.binance_signals import _get_klines, _parse_klines
        klines = _parse_klines(_get_klines(coin, '1m', 20))

        if len(klines) < 3:
            return None

        # Find the close price at each market's start epoch
        epoch_longer_ms = epoch_longer * 1000
        epoch_shorter_ms = epoch_shorter * 1000

        price_at_longer = None
        price_at_shorter = None

        for k in klines:
            ot = k['open_time']
            ct = k['close_time']
            # Kline covers [open_time, close_time] — match epoch within candle
            if ot <= epoch_longer_ms <= ct:
                price_at_longer = k['close']
            if ot <= epoch_shorter_ms <= ct:
                price_at_shorter = k['close']

        # Fallback: use BinanceFeed price_history snapshots
        if (not price_at_longer or not price_at_shorter) and binance_feed:
            history = binance_feed.get_price_history(coin)
            for snap in history:
                snap_ms = snap.timestamp * 1000
                if not price_at_longer and abs(snap_ms - epoch_longer_ms) < 45000:
                    price_at_longer = snap.price
                if not price_at_shorter and abs(snap_ms - epoch_shorter_ms) < 45000:
                    price_at_shorter = snap.price

        if not price_at_longer or not price_at_shorter:
            return None

        # Dead zone = gap between the two thresholds
        gap = price_at_shorter - price_at_longer
        width = abs(gap)
        lo = min(price_at_longer, price_at_shorter)
        hi = max(price_at_longer, price_at_shorter)

        # Current Binance price
        current_price = None
        if binance_feed:
            current_price = binance_feed.get_price(coin)
        if not current_price:
            current_price = klines[-1]['close']

        pct = width / current_price * 100 if current_price else 0

        # Estimate recent 1m volatility (stddev of 1m returns)
        closes = [k['close'] for k in klines[-10:]]
        if len(closes) >= 3:
            returns = [(closes[i] - closes[i-1]) / closes[i-1]
                       for i in range(1, len(closes))]
            vol_1m_pct = (sum(r**2 for r in returns) / len(returns)) ** 0.5
        else:
            vol_1m_pct = 0.001  # Conservative fallback

        # Project volatility to remaining ~5 minutes
        remaining_mins = 5.0
        vol_remaining = vol_1m_pct * remaining_mins ** 0.5 * current_price

        # Dead zone probability using normal approximation
        # P(lo < P_final < hi) where P_final ~ N(current_price, vol²)
        if vol_remaining > 0:
            mid = (lo + hi) / 2
            z_dist = abs(current_price - mid) / vol_remaining
            # Base probability from gap width relative to volatility
            p_base = width / (vol_remaining * 2.507)  # sqrt(2*pi) ≈ 2.507
            # Modulate by distance from dead zone center
            p_dead = p_base * math.exp(-0.5 * z_dist * z_dist)
            p_dead = min(0.50, max(0.01, p_dead))
        else:
            p_dead = 0.15  # Conservative default

        return {
            'width': width,
            'pct': pct,
            'lo': lo,
            'hi': hi,
            'p_dead': p_dead,
            'current_price': current_price,
            'vol_remaining': vol_remaining,
            'price_at_longer': price_at_longer,
            'price_at_shorter': price_at_shorter,
        }

    def get_suitable_timeframes(self) -> List[int]:
        """Needs multiple timeframes to find overlapping pairs."""
        return [1, 5, 15, 30, 60]
