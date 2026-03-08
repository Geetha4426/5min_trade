"""
Reference Price Engine — Captures price-to-beat for each market.

Each Polymarket 5-min UP/DOWN market has a "price-to-beat" — the Chainlink
oracle price at the moment the market opened. The UP token pays $1 if BTC
finishes ABOVE this price; DOWN pays $1 if BELOW.

We reconstruct this reference price using:
  1. Binance WebSocket snapshot at market discovery time (most accurate)
  2. Binance 1m kline_open at the market's epoch (fallback, ~$4 avg error)

Once captured, we can calculate the TRUE probability of UP winning using:
  - Distance from reference price (how far above/below BTC currently is)
  - Time remaining (less time = more certainty)
  - Recent volatility (higher vol = more uncertain)

This transforms strategies from "directional guesses" into
"calibrated probability edge detection."
"""

import math
import time
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

_SLUG_RE = re.compile(
    r'(?:btc|eth|sol|xrp)-updown-(?:\d+)m-(?P<epoch>\d+)', re.I
)


class ReferencePriceEngine:
    """Captures and caches price-to-beat per market. Calculates P(UP)."""

    def __init__(self):
        # market_id -> {'ref_price': float, 'epoch': int, 'coin': str, 'source': str}
        self._cache: Dict[str, Dict] = {}
        # coin -> deque of recent 1m returns for volatility estimation
        self._vol_cache: Dict[str, Dict] = {}

    def capture(self, market: Dict, binance_feed=None, binance_signals_mod=None):
        """Capture the reference price for a market (call once per market).

        Priority:
          1. Binance WS latest price (if market just opened, this IS the reference)
          2. Binance REST kline_open at the market's epoch (fallback)
        """
        market_id = market.get('market_id', '')
        if not market_id or market_id in self._cache:
            return  # Already captured

        coin = market.get('coin', '')
        epoch = self._get_epoch(market)
        if not epoch or not coin:
            return

        ref_price = None
        source = 'none'

        # How old is this market? If freshly opened (<60s), WS price is ideal
        now_ts = int(time.time())
        market_age_secs = now_ts - epoch

        # Method 1: Binance WS price (best for fresh markets)
        if binance_feed and market_age_secs < 90:
            ws_price = binance_feed.get_price(coin) if hasattr(binance_feed, 'get_price') else None
            if ws_price and ws_price > 0:
                ref_price = ws_price
                source = 'binance_ws'

        # Method 2: Binance kline_open at the epoch (best for older markets)
        if not ref_price and binance_signals_mod:
            ref_price = self._kline_open_at_epoch(coin, epoch, binance_signals_mod)
            if ref_price:
                source = 'kline_open'

        # Method 3: WS price history — find snapshot closest to epoch
        if not ref_price and binance_feed:
            ref_price = self._ws_history_at_epoch(coin, epoch, binance_feed)
            if ref_price:
                source = 'ws_history'

        if ref_price:
            self._cache[market_id] = {
                'ref_price': ref_price,
                'epoch': epoch,
                'coin': coin,
                'source': source,
                'captured_at': now_ts,
            }

    def get_reference_price(self, market_id: str) -> Optional[float]:
        """Get the cached reference price for a market."""
        entry = self._cache.get(market_id)
        return entry['ref_price'] if entry else None

    def get_info(self, market_id: str) -> Optional[Dict]:
        """Get full reference price info for a market."""
        return self._cache.get(market_id)

    def calc_p_up(self, market: Dict, binance_feed, seconds_remaining: int) -> Optional[Dict]:
        """Calculate probability that UP wins, given reference price and current Binance.

        Model: P(UP) = Phi(d / sigma_remaining)
        Where:
          d = (current_price - reference_price) / reference_price
          sigma_remaining = vol_1m * sqrt(minutes_remaining)
          Phi = standard normal CDF

        Returns dict with: p_up, p_down, distance, distance_pct, vol, edge info
        Or None if reference price not available.
        """
        market_id = market.get('market_id', '')
        entry = self._cache.get(market_id)
        if not entry:
            return None

        coin = market.get('coin', '')
        ref_price = entry['ref_price']

        # Get current Binance price
        current_price = None
        if binance_feed:
            current_price = binance_feed.get_price(coin) if hasattr(binance_feed, 'get_price') else None
        if not current_price:
            return None

        # Distance from reference
        distance = current_price - ref_price
        distance_pct = distance / ref_price if ref_price > 0 else 0

        # Estimate remaining volatility
        minutes_remaining = max(0.1, seconds_remaining / 60.0)
        vol_1m = self._estimate_vol_1m(coin, binance_feed)
        sigma_remaining = vol_1m * math.sqrt(minutes_remaining)

        # P(UP wins) = P(final_price > ref_price)
        # = P(Z > -d/sigma) = Phi(d/sigma)
        if sigma_remaining > 0:
            z = distance_pct / sigma_remaining
            p_up = _norm_cdf(z)
        else:
            # No volatility data — use distance sign
            p_up = 0.75 if distance > 0 else 0.25

        # Clamp to avoid extremes
        p_up = max(0.02, min(0.98, p_up))
        p_down = 1.0 - p_up

        return {
            'p_up': p_up,
            'p_down': p_down,
            'ref_price': ref_price,
            'current_price': current_price,
            'distance': distance,
            'distance_pct': distance_pct * 100,  # As percentage
            'vol_1m': vol_1m,
            'sigma_remaining': sigma_remaining,
            'minutes_remaining': minutes_remaining,
            'source': entry['source'],
        }

    def calc_edge(self, market: Dict, binance_feed, seconds_remaining: int,
                  poly_up_price: float, poly_down_price: float) -> Optional[Dict]:
        """Calculate the TRUE edge: model probability vs Polymarket price.

        Edge = P(model) - P(market). Positive = market underprices this side.

        Returns dict with: up_edge, down_edge, best_side, best_edge, and full p_up info.
        """
        prob = self.calc_p_up(market, binance_feed, seconds_remaining)
        if not prob:
            return None

        # Polymarket prices ARE implied probabilities
        up_edge = prob['p_up'] - poly_up_price
        down_edge = prob['p_down'] - poly_down_price

        if up_edge > down_edge:
            best_side = 'UP'
            best_edge = up_edge
        else:
            best_side = 'DOWN'
            best_edge = down_edge

        result = dict(prob)
        result.update({
            'poly_up_price': poly_up_price,
            'poly_down_price': poly_down_price,
            'up_edge': up_edge,
            'down_edge': down_edge,
            'best_side': best_side,
            'best_edge': best_edge,
        })
        return result

    def cleanup_expired(self, max_age_secs: int = 1800):
        """Remove old entries (markets that have long expired)."""
        now = int(time.time())
        stale = [k for k, v in self._cache.items()
                 if now - v.get('captured_at', 0) > max_age_secs]
        for k in stale:
            del self._cache[k]

    # ── Internal helpers ──

    def _get_epoch(self, market: Dict) -> Optional[int]:
        """Extract market open epoch from event_start_time or slug."""
        est = market.get('event_start_time', '')
        if est:
            try:
                dt = datetime.fromisoformat(est.replace('Z', '+00:00'))
                return int(dt.timestamp())
            except (ValueError, TypeError):
                pass
        slug = market.get('event_slug', '') or market.get('market_slug', '')
        m = _SLUG_RE.search(slug)
        if m:
            return int(m.group('epoch'))
        return None

    def _kline_open_at_epoch(self, coin: str, epoch: int, binance_signals_mod) -> Optional[float]:
        """Fetch Binance kline that contains the epoch, return its OPEN price."""
        try:
            klines = binance_signals_mod._parse_klines(
                binance_signals_mod._get_klines(coin, '1m', 20)
            )
            epoch_ms = epoch * 1000
            for k in klines:
                if k['open_time'] <= epoch_ms <= k['close_time']:
                    return k['open']  # kline_open is most accurate (~$4 avg error)
        except Exception:
            pass
        return None

    def _ws_history_at_epoch(self, coin: str, epoch: int, binance_feed) -> Optional[float]:
        """Find the WS price snapshot closest to the epoch."""
        try:
            history = binance_feed.get_price_history(coin)
            if not history:
                return None
            best_snap = None
            best_dt = float('inf')
            for snap in history:
                dt = abs(snap.timestamp - epoch)
                if dt < best_dt:
                    best_dt = dt
                    best_snap = snap
            if best_snap and best_dt < 120:  # Within 2 minutes
                return best_snap.price
        except Exception:
            pass
        return None

    def _estimate_vol_1m(self, coin: str, binance_feed) -> float:
        """Estimate 1-minute volatility (as fraction) from recent WS history."""
        # Check cache (refreshes every 30s)
        now = time.time()
        cached = self._vol_cache.get(coin)
        if cached and now - cached.get('ts', 0) < 30:
            return cached['vol']

        default_vols = {'BTC': 0.0015, 'ETH': 0.0025, 'SOL': 0.004, 'XRP': 0.003}
        default = default_vols.get(coin, 0.002)

        try:
            history = binance_feed.get_price_history(coin) if binance_feed else []
            if len(history) < 10:
                return default

            # Resample to ~1-minute intervals
            minute_prices = []
            last_t = 0
            for snap in history:
                if snap.timestamp - last_t >= 55:  # ~1 min apart
                    minute_prices.append(snap.price)
                    last_t = snap.timestamp

            if len(minute_prices) < 3:
                return default

            returns = []
            for i in range(1, len(minute_prices)):
                r = (minute_prices[i] - minute_prices[i-1]) / minute_prices[i-1]
                returns.append(r)

            vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
            vol = max(0.0005, min(0.01, vol))  # Clamp to reasonable range

            self._vol_cache[coin] = {'vol': vol, 'ts': now}
            return vol
        except Exception:
            return default


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
