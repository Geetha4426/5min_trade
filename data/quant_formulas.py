"""
Quant Formulas Module — Market Microstructure Intelligence

Implements quantitative trading formulas from academic market microstructure
research (Algorithmic and High-Frequency Trading, Cartea/Jaimungal/Penalva)
and crypto-specific adaptations for Polymarket binary options.

Formulas implemented:
  1. MicroPrice — volume-weighted midpoint (more accurate than simple mid)
  2. Effective Spread — true trading cost (not quoted spread)
  3. Fill Probability — optimal limit order distance (κ model)
  4. Kyle's Lambda — informed trading detection
  5. Glosten-Milgrom — adverse selection probability
  6. Inventory Penalty — position-aware bias (φ model)
  7. Optimal Execution — sinh-based exit speed curve
  8. EMA Crossover — exponential moving average directional signal
  9. RSI (Relative Strength Index) — overbought/oversold detection
  10. Bollinger Band Position — volatility-relative price position
  11. VWAP Distance — volume-weighted average price divergence
  12. Composite Bayesian Score — fuses all signals into P(direction)

These are PURE functions — no side effects, no state, no API calls.
Feed them data, get numbers back.
"""

import math
from typing import Dict, Optional, List, Tuple


# ═══════════════════════════════════════════════════════════════════
# 1. MICROPRICE — Volume-weighted midpoint
# ═══════════════════════════════════════════════════════════════════

def microprice(best_bid: float, best_ask: float,
               bid_depth: float, ask_depth: float) -> float:
    """
    MicroPrice = (V_ask × bid + V_bid × ask) / (V_bid + V_ask)

    More accurate than simple midpoint because it weights by volume:
    - Lots of buyers (high bid_depth) → MicroPrice shifts toward ask → price going UP
    - Lots of sellers (high ask_depth) → MicroPrice shifts toward bid → price going DOWN

    Returns the MicroPrice, or simple midpoint if depth data unavailable.
    """
    total = bid_depth + ask_depth
    if total <= 0 or best_bid <= 0 or best_ask <= 0:
        return (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 0.5

    return (ask_depth * best_bid + bid_depth * best_ask) / total


def microprice_signal(best_bid: float, best_ask: float,
                      bid_depth: float, ask_depth: float) -> Dict:
    """
    Compute MicroPrice and derive a directional signal.

    Returns:
        microprice: the volume-weighted mid
        midpoint: simple mid
        skew: microprice - midpoint (positive = bullish)
        direction: 'UP', 'DOWN', or 'NEUTRAL'
        strength: 0-1 signal strength
    """
    mid = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 0.5
    mp = microprice(best_bid, best_ask, bid_depth, ask_depth)
    skew = mp - mid
    spread = best_ask - best_bid if best_ask > best_bid else 0.01

    # Normalize skew relative to spread
    # skew/spread > 0.3 = strong signal
    norm_skew = skew / spread if spread > 0 else 0

    if norm_skew > 0.15:
        direction = 'UP'
    elif norm_skew < -0.15:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    strength = min(1.0, abs(norm_skew) * 2)

    return {
        'microprice': mp,
        'midpoint': mid,
        'skew': skew,
        'norm_skew': norm_skew,
        'direction': direction,
        'strength': strength,
    }


# ═══════════════════════════════════════════════════════════════════
# 2. EFFECTIVE SPREAD — True trading cost
# ═══════════════════════════════════════════════════════════════════

def effective_spread(trade_price: float, mid_price: float) -> float:
    """
    ES = 2 × |P_trade − MidPrice|

    The REAL cost of trading. If ES > your edge, you're donating to market makers.
    For Polymarket: edge must exceed ES to be profitable.
    """
    if mid_price <= 0:
        return 0
    return 2 * abs(trade_price - mid_price)


def is_edge_profitable(edge: float, entry_price: float, mid_price: float) -> bool:
    """Check if your edge exceeds the effective spread (is the trade worth it?)."""
    es = effective_spread(entry_price, mid_price)
    return edge > es


# ═══════════════════════════════════════════════════════════════════
# 3. FILL PROBABILITY — Optimal limit order distance
# ═══════════════════════════════════════════════════════════════════

def fill_probability(distance_from_mid: float, kappa: float = 50.0) -> float:
    """
    P(fill | δ) = e^(-κ × δ)

    Where δ = distance from midpoint, κ = market-specific fill rate parameter.
    Crypto 5-min markets: κ ≈ 50-100 (thin books, fast expiry).

    Optimal limit order distance = 1/κ from mid.
    """
    if distance_from_mid < 0:
        distance_from_mid = abs(distance_from_mid)
    return math.exp(-kappa * distance_from_mid)


def optimal_limit_distance(kappa: float = 50.0) -> float:
    """Optimal distance from mid for limit orders: 1/κ."""
    return 1.0 / kappa if kappa > 0 else 0.02


# ═══════════════════════════════════════════════════════════════════
# 4. KYLE'S LAMBDA — Informed trading detection
# ═══════════════════════════════════════════════════════════════════

def kyle_lambda(price_volatility: float, noise_volume: float) -> float:
    """
    λ = σ_v / (2 × σ_u)

    Measures market's price impact per unit of order flow.
    High λ = informed traders dominating = dangerous to trade against.
    Low λ = noise traders dominating = safer entry.

    For Polymarket:
    - σ_v: implied price volatility (from recent price swings)
    - σ_u: noise volume (total volume minus directional flow)
    """
    if noise_volume <= 0:
        return float('inf')  # All informed trading — avoid
    return price_volatility / (2 * noise_volume)


def is_safe_to_enter(lam: float, threshold: float = 0.05) -> bool:
    """
    Low lambda = noise-dominated = safe for us to enter.
    High lambda = informed traders = they know the answer, avoid.

    threshold=0.05: conservative. In 5-min markets where spreads are
    25¢+, even 0.10 is acceptable.
    """
    return lam < threshold


# ═══════════════════════════════════════════════════════════════════
# 5. GLOSTEN-MILGROM — Adverse selection probability
# ═══════════════════════════════════════════════════════════════════

def adverse_selection_prob(spread: float) -> float:
    """
    Estimate probability that counterparty is an informed trader.

    From Glosten-Milgrom model: wider spread ≈ higher adverse selection.
    For Polymarket 5-min: 25¢ spread means ~20% informed traders.

    P(informed) ≈ spread / (2 × max_profit)
    In binary options: max_profit = $1, so P(informed) ≈ spread / 2
    """
    return min(1.0, max(0.0, spread / 2.0))


def adverse_selection_safe(spread: float, max_informed_pct: float = 0.25) -> bool:
    """Is adverse selection risk below our tolerance?"""
    return adverse_selection_prob(spread) < max_informed_pct


# ═══════════════════════════════════════════════════════════════════
# 6. INVENTORY PENALTY — Position-aware bias
# ═══════════════════════════════════════════════════════════════════

def inventory_penalty(position_size: float, phi: float = 0.01) -> float:
    """
    Penalty = φ × Q²

    Larger position → quadratically more costly to hold.
    Used to bias trading toward reducing position (risk management).

    φ = risk aversion parameter (0.01 = mild, 0.1 = aggressive)
    Q = current position size in dollars
    """
    return phi * position_size ** 2


def inventory_adjusted_threshold(base_threshold: float,
                                  current_positions: int,
                                  max_positions: int) -> float:
    """
    Raise confidence threshold when we're already heavily positioned.
    More positions → need higher confidence for next trade.
    """
    if max_positions <= 0:
        return base_threshold
    utilization = current_positions / max_positions
    # Quadratic increase: at 50% utilization → +5%, at 100% → +20%
    penalty = 0.20 * utilization ** 2
    return min(0.99, base_threshold + penalty)


# ═══════════════════════════════════════════════════════════════════
# 7. OPTIMAL EXECUTION — sinh exit speed curve
# ═══════════════════════════════════════════════════════════════════

def optimal_exit_urgency(seconds_remaining: int, total_seconds: int = 300,
                         kappa: float = 2.0) -> float:
    """
    Optimal execution speed from Almgren-Chriss model:
    v(t) = cosh(κ(T-t)) / sinh(κT)

    This tells you: go SLOW early, then EXPONENTIALLY faster near deadline.
    For 5-min markets: urgency ramps up in the last 60 seconds.

    Returns: urgency factor 0-1 (0=hold, 1=exit NOW)
    """
    if total_seconds <= 0 or seconds_remaining <= 0:
        return 1.0  # Must exit immediately

    t_remaining = seconds_remaining / total_seconds  # 0=expired, 1=full time
    try:
        urgency = math.cosh(kappa * (1 - t_remaining)) / math.sinh(kappa)
        return min(1.0, max(0.0, urgency))
    except (ValueError, OverflowError):
        return 1.0 if t_remaining < 0.2 else 0.0


# ═══════════════════════════════════════════════════════════════════
# 8. EMA CROSSOVER — Exponential Moving Average direction
# ═══════════════════════════════════════════════════════════════════

def ema(prices: List[float], period: int) -> float:
    """Calculate EMA from a price series."""
    if not prices:
        return 0
    if len(prices) < period:
        return sum(prices) / len(prices)

    multiplier = 2.0 / (period + 1)
    result = sum(prices[:period]) / period  # SMA seed
    for price in prices[period:]:
        result = (price - result) * multiplier + result
    return result


def ema_crossover_signal(prices: List[float],
                          fast_period: int = 8,
                          slow_period: int = 21) -> Dict:
    """
    EMA crossover direction signal.

    fast > slow → bullish (UP)
    fast < slow → bearish (DOWN)
    Distance between them → strength of signal

    Returns: direction, strength, fast_ema, slow_ema, cross_pct
    """
    if len(prices) < slow_period + 1:
        return {'direction': 'NEUTRAL', 'strength': 0, 'fast_ema': 0, 'slow_ema': 0, 'cross_pct': 0}

    fast = ema(prices, fast_period)
    slow = ema(prices, slow_period)

    cross_pct = (fast - slow) / slow * 100 if slow > 0 else 0

    if cross_pct > 0.01:
        direction = 'UP'
    elif cross_pct < -0.01:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    # Strength: how far apart the EMAs are (normalized)
    strength = min(1.0, abs(cross_pct) * 10)

    return {
        'direction': direction,
        'strength': strength,
        'fast_ema': fast,
        'slow_ema': slow,
        'cross_pct': cross_pct,
    }


# ═══════════════════════════════════════════════════════════════════
# 9. RSI — Relative Strength Index
# ═══════════════════════════════════════════════════════════════════

def rsi(prices: List[float], period: int = 14) -> float:
    """
    RSI = 100 - 100/(1 + RS)
    RS = avg_gain / avg_loss

    < 30: oversold (bearish exhaustion → potential UP reversal)
    > 70: overbought (bullish exhaustion → potential DOWN reversal)
    30-70: neutral zone

    For 5-min crypto: use 14 periods on 1m candles = 14 minutes lookback.
    """
    if len(prices) < 2:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    n = min(period, len(gains))
    if n == 0:
        return 50.0

    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_signal(prices: List[float], period: int = 14) -> Dict:
    """
    RSI-based directional signal.

    Returns: rsi value, direction, strength, zone
    """
    r = rsi(prices, period)

    if r > 70:
        direction = 'DOWN'  # Overbought → expect reversal down
        zone = 'overbought'
        strength = min(1.0, (r - 70) / 30)
    elif r < 30:
        direction = 'UP'  # Oversold → expect reversal up
        zone = 'oversold'
        strength = min(1.0, (30 - r) / 30)
    else:
        # In neutral zone, use RSI slope for momentum
        if r > 55:
            direction = 'UP'
            strength = (r - 50) / 50
        elif r < 45:
            direction = 'DOWN'
            strength = (50 - r) / 50
        else:
            direction = 'NEUTRAL'
            strength = 0
        zone = 'neutral'

    return {
        'rsi': r,
        'direction': direction,
        'strength': strength,
        'zone': zone,
    }


# ═══════════════════════════════════════════════════════════════════
# 10. BOLLINGER BAND POSITION
# ═══════════════════════════════════════════════════════════════════

def bollinger_position(prices: List[float], period: int = 20,
                       num_std: float = 2.0) -> Dict:
    """
    Where is the current price relative to Bollinger Bands?

    Position = (price - lower) / (upper - lower)
    0 = at lower band (oversold), 1 = at upper band (overbought)
    >1 = above upper (extreme overbought), <0 = below lower (extreme oversold)
    """
    if len(prices) < period:
        return {'position': 0.5, 'direction': 'NEUTRAL', 'strength': 0,
                'upper': 0, 'lower': 0, 'sma': 0, 'bandwidth': 0}

    recent = prices[-period:]
    sma_val = sum(recent) / period
    variance = sum((p - sma_val) ** 2 for p in recent) / period
    std = variance ** 0.5

    upper = sma_val + num_std * std
    lower = sma_val - num_std * std
    bandwidth = upper - lower if upper > lower else 0.001

    current = prices[-1]
    position = (current - lower) / bandwidth if bandwidth > 0 else 0.5

    if position > 0.85:
        direction = 'DOWN'  # Near upper band → expect mean reversion down
        strength = min(1.0, (position - 0.85) / 0.30)
    elif position < 0.15:
        direction = 'UP'  # Near lower band → expect mean reversion up
        strength = min(1.0, (0.15 - position) / 0.30)
    else:
        # In the middle — use momentum (above/below SMA)
        if current > sma_val:
            direction = 'UP'
        elif current < sma_val:
            direction = 'DOWN'
        else:
            direction = 'NEUTRAL'
        strength = abs(position - 0.5) * 2

    return {
        'position': position,
        'direction': direction,
        'strength': strength,
        'upper': upper,
        'lower': lower,
        'sma': sma_val,
        'bandwidth': bandwidth,
    }


# ═══════════════════════════════════════════════════════════════════
# 11. VWAP DISTANCE
# ═══════════════════════════════════════════════════════════════════

def vwap_signal(prices: List[float], volumes: List[float]) -> Dict:
    """
    VWAP distance signal.

    Price above VWAP → bullish (buyers willing to pay more than average)
    Price below VWAP → bearish (sellers dominating)
    """
    if not prices or not volumes or len(prices) != len(volumes):
        return {'direction': 'NEUTRAL', 'strength': 0, 'vwap': 0, 'distance_pct': 0}

    total_pv = sum(p * v for p, v in zip(prices, volumes))
    total_vol = sum(volumes)
    if total_vol <= 0:
        return {'direction': 'NEUTRAL', 'strength': 0, 'vwap': 0, 'distance_pct': 0}

    vwap_val = total_pv / total_vol
    current = prices[-1]
    distance_pct = ((current - vwap_val) / vwap_val) * 100 if vwap_val > 0 else 0

    if distance_pct > 0.03:
        direction = 'UP'
    elif distance_pct < -0.03:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    strength = min(1.0, abs(distance_pct) * 5)

    return {
        'direction': direction,
        'strength': strength,
        'vwap': vwap_val,
        'distance_pct': distance_pct,
    }


# ═══════════════════════════════════════════════════════════════════
# 12. COMPOSITE BAYESIAN SCORE — Fuses all signals
# ═══════════════════════════════════════════════════════════════════

def composite_direction_score(signals: List[Dict]) -> Dict:
    """
    Bayesian fusion of multiple directional signals.

    Each signal has: direction ('UP'/'DOWN'/'NEUTRAL'), strength (0-1), weight (0-1)

    Uses log-odds Bayesian update:
      log_odds_UP += weight × strength × sign(direction)

    Then converts back to probability.
    The more signals agree, the higher the final probability.

    Returns:
        direction: 'UP' or 'DOWN'
        probability: P(direction) (0.5-1.0)
        agreement: how many signals agree (0-1)
        total_signals: count of non-neutral signals
    """
    if not signals:
        return {'direction': 'NEUTRAL', 'probability': 0.5, 'agreement': 0, 'total_signals': 0}

    log_odds = 0.0  # Start at 50/50 (log-odds = 0)
    total_weight = 0
    up_votes = 0
    down_votes = 0

    for sig in signals:
        direction = sig.get('direction', 'NEUTRAL')
        strength = sig.get('strength', 0)
        weight = sig.get('weight', 1.0)

        if direction == 'NEUTRAL' or strength <= 0:
            continue

        # Convert signal to log-odds contribution
        # strength × weight determines how much we shift our belief
        shift = weight * strength * 1.5  # 1.5 scaling = moderate impact per signal

        if direction == 'UP':
            log_odds += shift
            up_votes += 1
        elif direction == 'DOWN':
            log_odds -= shift
            down_votes += 1

        total_weight += weight

    # Convert log-odds back to probability
    # P(UP) = 1 / (1 + exp(-log_odds))
    try:
        p_up = 1.0 / (1.0 + math.exp(-log_odds))
    except OverflowError:
        p_up = 1.0 if log_odds > 0 else 0.0

    total_votes = up_votes + down_votes
    if p_up >= 0.5:
        direction = 'UP'
        probability = p_up
        agreement = up_votes / total_votes if total_votes > 0 else 0
    else:
        direction = 'DOWN'
        probability = 1.0 - p_up
        agreement = down_votes / total_votes if total_votes > 0 else 0

    return {
        'direction': direction,
        'probability': probability,
        'agreement': agreement,
        'total_signals': total_votes,
        'up_votes': up_votes,
        'down_votes': down_votes,
        'log_odds': log_odds,
    }


# ═══════════════════════════════════════════════════════════════════
# 13. KLINE MOMENTUM — Candle-based directional trend
# ═══════════════════════════════════════════════════════════════════

def kline_momentum(klines: List[Dict], lookback: int = 5) -> Dict:
    """
    Analyze last N kline candles for directional momentum.

    Checks:
    - How many candles are green (close > open) vs red
    - Average body size (strength of moves)
    - Wick ratio (rejection signals)

    Returns: direction, strength, green_ratio, avg_body_pct
    """
    if not klines or len(klines) < 2:
        return {'direction': 'NEUTRAL', 'strength': 0, 'green_ratio': 0.5, 'avg_body_pct': 0}

    recent = klines[-lookback:]
    green_count = 0
    body_pcts = []

    for k in recent:
        o = k.get('open', 0)
        c = k.get('close', 0)
        if o <= 0:
            continue
        if c > o:
            green_count += 1
        body_pct = abs(c - o) / o * 100
        body_pcts.append(body_pct)

    green_ratio = green_count / len(recent) if recent else 0.5
    avg_body = sum(body_pcts) / len(body_pcts) if body_pcts else 0

    if green_ratio >= 0.7:
        direction = 'UP'
    elif green_ratio <= 0.3:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    strength = abs(green_ratio - 0.5) * 2  # 0 at 50%, 1 at 0% or 100%
    strength = min(1.0, strength * (1 + avg_body * 2))  # Boost by body size

    return {
        'direction': direction,
        'strength': min(1.0, strength),
        'green_ratio': green_ratio,
        'avg_body_pct': avg_body,
    }


# ═══════════════════════════════════════════════════════════════════
# 14. ORDERBOOK IMBALANCE — Depth-based directional signal
# ═══════════════════════════════════════════════════════════════════

def orderbook_imbalance_signal(bid_depth: float, ask_depth: float) -> Dict:
    """
    Orderbook imbalance as directional signal.

    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    >0: more bids = buying pressure = UP
    <0: more asks = selling pressure = DOWN

    Returns: direction, strength, imbalance ratio
    """
    total = bid_depth + ask_depth
    if total <= 0:
        return {'direction': 'NEUTRAL', 'strength': 0, 'imbalance': 0}

    imbalance = (bid_depth - ask_depth) / total

    if imbalance > 0.10:
        direction = 'UP'
    elif imbalance < -0.10:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    strength = min(1.0, abs(imbalance) * 2.5)

    return {
        'direction': direction,
        'strength': strength,
        'imbalance': imbalance,
    }


# ═══════════════════════════════════════════════════════════════════
# 15. POLYMARKET-SPECIFIC: Combined Ask Arbitrage Score
# ═══════════════════════════════════════════════════════════════════

def arb_score(up_ask: float, down_ask: float) -> Dict:
    """
    For binary markets: UP + DOWN should sum to ~$1.00.
    If combined < $1.00, guaranteed profit (buy both sides).
    If combined > $1.02, someone is overpaying (edge for the other side).

    Returns: combined cost, arb_profit, overpriced_side, underpriced_side
    """
    combined = up_ask + down_ask
    arb_profit = max(0, 1.0 - combined)

    if up_ask > down_ask + 0.04:
        overpriced = 'UP'
        underpriced = 'DOWN'
    elif down_ask > up_ask + 0.04:
        overpriced = 'DOWN'
        underpriced = 'UP'
    else:
        overpriced = 'NONE'
        underpriced = 'NONE'

    return {
        'combined_ask': combined,
        'arb_profit': arb_profit,
        'is_arb': combined < 0.98,
        'overpriced_side': overpriced,
        'underpriced_side': underpriced,
        'relative_value': up_ask / combined if combined > 0 else 0.5,
    }
