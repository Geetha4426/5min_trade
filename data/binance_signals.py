"""
Binance Signal Engine — Advanced Market Intelligence

Provides real-time signals from Binance for cross-exchange arbitrage:
  1. Price Momentum — velocity, acceleration, RSI from multiple timeframes
  2. Order Flow — taker buy/sell pressure from recent trades
  3. Cross-Exchange Divergence — Binance implied prob vs Polymarket prob
  4. Volume-Weighted Momentum (VWM) — VWAP distance + volume quality

KEY INSIGHT: Polymarket uses Chainlink oracle data which lags Binance
by ~60 seconds. This module detects when Binance has already moved
but Polymarket hasn't caught up yet — a guaranteed edge window.
"""

import time
import math
import asyncio
import requests
from typing import Dict, Optional, Tuple, List as TList
from config import Config


# Binance symbol mapping
BINANCE_PAIRS = {
    'BTC': 'BTCUSDT',
    'ETH': 'ETHUSDT',
    'SOL': 'SOLUSDT',
    'XRP': 'XRPUSDT',
    'DOGE': 'DOGEUSDT',
    'AVAX': 'AVAXUSDT',
    'LINK': 'LINKUSDT',
    'ADA': 'ADAUSDT',
    'MATIC': 'MATICUSDT',
    'DOT': 'DOTUSDT',
    'SUI': 'SUIUSDT',
}

# Cache to avoid hitting Binance rate limits
_cache: Dict[str, Dict] = {}
_CACHE_TTL = 5  # seconds — warm_cache prevents blocking, keep data fresh


def _get_klines(symbol: str, interval: str = '1m', limit: int = 15) -> list:
    """Fetch klines from Binance REST API with caching."""
    cache_key = f"{symbol}_{interval}_{limit}"
    now = time.time()

    if cache_key in _cache:
        cached = _cache[cache_key]
        if now - cached['ts'] < _CACHE_TTL:
            return cached['data']

    pair = BINANCE_PAIRS.get(symbol.upper())
    if not pair:
        return []

    try:
        url = "https://api.binance.com/api/v3/klines"
        resp = requests.get(url, params={
            'symbol': pair,
            'interval': interval,
            'limit': limit,
        }, timeout=5)
        data = resp.json()
        _cache[cache_key] = {'data': data, 'ts': now}
        return data
    except Exception:
        return []


def _fetch_klines_sync(pair: str, interval: str, limit: int) -> list:
    """Blocking HTTP fetch — called via asyncio.to_thread()."""
    try:
        url = "https://api.binance.com/api/v3/klines"
        resp = requests.get(url, params={
            'symbol': pair, 'interval': interval, 'limit': limit,
        }, timeout=5)
        return resp.json()
    except Exception:
        return []


async def warm_cache(coins: TList[str], interval: str = '1m', limit: int = 25):
    """
    Pre-fetch klines for all active coins in parallel threads.

    Call this ONCE at the start of each scan cycle so that all subsequent
    _get_klines() calls hit the warm cache and never block the event loop.
    """
    now = time.time()
    tasks = []
    keys = []

    for coin in coins:
        cache_key = f"{coin}_{interval}_{limit}"
        # Skip if cache is still fresh
        if cache_key in _cache and now - _cache[cache_key]['ts'] < _CACHE_TTL:
            continue
        pair = BINANCE_PAIRS.get(coin.upper())
        if not pair:
            continue
        keys.append((coin, cache_key))
        tasks.append(asyncio.to_thread(_fetch_klines_sync, pair, interval, limit))

    if not tasks:
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    now = time.time()
    for (coin, cache_key), result in zip(keys, results):
        if isinstance(result, list) and result:
            _cache[cache_key] = {'data': result, 'ts': now}
            # Also warm the 15-candle cache since most strategies use that
            cache_key_15 = f"{coin}_{interval}_15"
            if limit >= 15:
                _cache[cache_key_15] = {'data': result[-15:], 'ts': now}


def _parse_klines(raw: list) -> list:
    """Parse raw klines into dicts."""
    result = []
    for k in raw:
        result.append({
            'open_time': k[0],
            'open': float(k[1]),
            'high': float(k[2]),
            'low': float(k[3]),
            'close': float(k[4]),
            'volume': float(k[5]),
            'close_time': k[6],
            'quote_vol': float(k[7]),
            'trades': int(k[8]),
            'taker_buy_base': float(k[9]),
            'taker_buy_quote': float(k[10]),
        })
    return result


def get_price_momentum(symbol: str, lookback_minutes: int = 15) -> Dict:
    """
    Calculate price momentum from Binance 1m klines.

    Returns:
        dict with: direction (UP/DOWN/NEUTRAL), strength (0-1),
                   velocity (% per minute), acceleration, rsi
    """
    klines = _parse_klines(_get_klines(symbol, '1m', lookback_minutes))
    if len(klines) < 5:
        return {
            'direction': 'NEUTRAL', 'strength': 0.0,
            'velocity': 0.0, 'acceleration': 0.0, 'rsi': 50.0,
        }

    closes = [k['close'] for k in klines]

    # Velocity: price change over last N candles (% per minute)
    total_change = (closes[-1] - closes[0]) / closes[0] * 100
    velocity = total_change / len(closes)

    # Acceleration: velocity change (last 5 vs first 5)
    mid = len(closes) // 2
    first_half_vel = (closes[mid] - closes[0]) / closes[0] * 100 / max(mid, 1)
    second_half_vel = (closes[-1] - closes[mid]) / closes[mid] * 100 / max(len(closes) - mid, 1)
    acceleration = second_half_vel - first_half_vel

    # RSI (14-period, adapted to available data)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else sum(gains) / max(len(gains), 1)
    avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else sum(losses) / max(len(losses), 1)
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = 100 - (100 / (1 + rs))

    # Strength: normalized from velocity + acceleration
    raw_strength = abs(velocity) * 10 + abs(acceleration) * 5
    strength = min(1.0, raw_strength)

    # Direction
    if velocity > 0.01:
        direction = 'UP'
    elif velocity < -0.01:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    # Boost strength if acceleration confirms direction
    if (direction == 'UP' and acceleration > 0) or (direction == 'DOWN' and acceleration < 0):
        strength = min(1.0, strength * 1.2)

    return {
        'direction': direction,
        'strength': strength,
        'velocity': velocity,
        'acceleration': acceleration,
        'rsi': rsi,
        'price_change_pct': total_change,
        'current_price': closes[-1],
        'start_price': closes[0],
    }


def get_order_flow(symbol: str) -> Dict:
    """
    Analyze Binance taker buy/sell pressure from recent 1m klines.

    Returns:
        dict with: buy_pressure (0-1), direction (UP/DOWN/NEUTRAL),
                   intensity, large_trade_bias
    """
    klines = _parse_klines(_get_klines(symbol, '1m', 5))
    if not klines:
        return {
            'buy_pressure': 0.5, 'direction': 'NEUTRAL',
            'intensity': 0.0, 'large_trade_bias': 'NEUTRAL',
        }

    total_vol = sum(k['volume'] for k in klines)
    total_taker_buy = sum(k['taker_buy_base'] for k in klines)
    avg_trades = sum(k['trades'] for k in klines) / len(klines)

    ratio = total_taker_buy / total_vol if total_vol > 0 else 0.5

    # Direction: needs meaningful skew (>55% or <45%)
    if ratio > 0.55:
        direction = 'UP'
    elif ratio < 0.45:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    # Intensity: normalized trade count
    intensity = min(1.0, avg_trades / 1000)  # 1000 trades/min = max intensity

    # Large trade bias from most recent candle
    recent = klines[-1]
    recent_ratio = recent['taker_buy_base'] / recent['volume'] if recent['volume'] > 0 else 0.5
    if recent_ratio > 0.65:
        large_trade_bias = 'BUY'
    elif recent_ratio < 0.35:
        large_trade_bias = 'SELL'
    else:
        large_trade_bias = 'NEUTRAL'

    return {
        'buy_pressure': ratio,
        'direction': direction,
        'intensity': intensity,
        'large_trade_bias': large_trade_bias,
    }


def get_cross_exchange_divergence(
    symbol: str,
    polymarket_up_price: float,
    seconds_remaining: int = 300,
) -> Dict:
    """
    THE KEY ORACLE DELAY EXPLOIT.

    Compares Binance real-time price movement against Polymarket's
    current probability pricing. Since Chainlink lags Binance by ~60s,
    large divergences = high-confidence signal.

    Args:
        symbol: coin symbol (BTC, ETH, etc.)
        polymarket_up_price: current Polymarket UP token mid price (0-1)
        seconds_remaining: seconds until market resolution

    Returns:
        dict with divergence, opportunity, binance_direction, implied probability
    """
    # Get Binance klines (match market timeframe)
    klines = _parse_klines(_get_klines(symbol, '1m', 15))
    if len(klines) < 5:
        return {
            'divergence': 0.0, 'opportunity': 'NEUTRAL',
            'binance_direction': 'NEUTRAL', 'binance_implied_prob': 0.5,
            'binance_price': 0, 'price_change_pct': 0,
        }

    # Calculate Binance price change over the window
    first_open = klines[0]['open']
    last_close = klines[-1]['close']
    pct_change = ((last_close - first_open) / first_open) * 100

    # Map price change to implied probability
    # Key calibration: 1% move in spot = ~20% move in probability
    # This is the PolyFlup-validated mapping
    binance_implied_prob = 0.5 + (pct_change / 5.0)
    binance_implied_prob = max(0.02, min(0.98, binance_implied_prob))

    # Calculate divergence: how much Polymarket disagrees with Binance
    # Positive = Polymarket thinks UP more than Binance suggests
    # Negative = Polymarket thinks DOWN more than Binance suggests  
    divergence = polymarket_up_price - binance_implied_prob

    # Binance direction
    if pct_change > 0.05:
        binance_direction = 'UP'
    elif pct_change < -0.05:
        binance_direction = 'DOWN'
    else:
        binance_direction = 'NEUTRAL'

    # Opportunity detection
    # If divergence < -0.05: Polymarket is pricing UP too low → BUY UP
    # If divergence >  0.05: Polymarket is pricing DOWN too low → BUY DOWN
    if divergence < -0.05:
        opportunity = 'BUY_UP'
    elif divergence > 0.05:
        opportunity = 'BUY_DOWN'
    else:
        opportunity = 'NEUTRAL'

    # Enhanced: check if divergence is growing (momentum of divergence)
    # Compare last 3 candles vs previous 3 candles
    if len(klines) >= 6:
        recent_change = ((klines[-1]['close'] - klines[-3]['open']) / klines[-3]['open']) * 100
        earlier_change = ((klines[-4]['close'] - klines[-6]['open']) / klines[-6]['open']) * 100
        divergence_momentum = recent_change - earlier_change
    else:
        divergence_momentum = 0

    return {
        'divergence': divergence,
        'opportunity': opportunity,
        'binance_direction': binance_direction,
        'binance_implied_prob': binance_implied_prob,
        'binance_price': last_close,
        'price_change_pct': pct_change,
        'divergence_momentum': divergence_momentum,
    }


def get_volume_weighted_momentum(symbol: str) -> Dict:
    """
    VWAP-based momentum signal.

    Returns:
        dict with: direction, strength, vwap_distance, volume_quality
    """
    klines = _parse_klines(_get_klines(symbol, '1m', 15))
    if len(klines) < 5:
        return {
            'direction': 'NEUTRAL', 'strength': 0.0,
            'vwap_distance': 0.0, 'volume_quality': 0.0,
        }

    # Calculate VWAP
    total_pv = sum(k['close'] * k['volume'] for k in klines)
    total_vol = sum(k['volume'] for k in klines)
    vwap = total_pv / total_vol if total_vol > 0 else klines[-1]['close']

    # Current price distance from VWAP (as percentage)
    current_price = klines[-1]['close']
    vwap_distance = ((current_price - vwap) / vwap) * 100 if vwap > 0 else 0

    # Volume quality: is recent volume higher than average?
    avg_vol = total_vol / len(klines)
    recent_vol = sum(k['volume'] for k in klines[-3:]) / 3
    volume_quality = min(1.0, recent_vol / avg_vol) if avg_vol > 0 else 0.5

    # Direction from VWAP distance
    if vwap_distance > 0.03:
        direction = 'UP'
    elif vwap_distance < -0.03:
        direction = 'DOWN'
    else:
        direction = 'NEUTRAL'

    # Strength: combines distance and volume
    strength = min(1.0, abs(vwap_distance) * 5 * volume_quality)

    return {
        'direction': direction,
        'strength': strength,
        'vwap_distance': vwap_distance,
        'volume_quality': volume_quality,
    }


def get_full_signal_analysis(
    symbol: str,
    polymarket_up_price: float,
    seconds_remaining: int = 300,
) -> Dict:
    """
    MASTER SIGNAL — Combines all 4 signals into a single weighted verdict.

    Inspired by PolyFlup's 6-signal weighted system with quality factors.

    Weights:
      - Price Momentum: 30%
      - Cross-Exchange Divergence: 25%
      - Order Flow: 20%
      - Volume-Weighted Momentum: 15%
      - Time Pressure: 10% (bonus)

    Returns:
        dict with: direction, confidence (0-1), signals breakdown,
                   entry_recommended, edge
    """
    # Gather all signals
    momentum = get_price_momentum(symbol)
    divergence = get_cross_exchange_divergence(symbol, polymarket_up_price, seconds_remaining)
    flow = get_order_flow(symbol)
    vwm = get_volume_weighted_momentum(symbol)

    # Weights
    W_MOM = 0.30
    W_DIV = 0.25
    W_FLOW = 0.20
    W_VWM = 0.15
    W_TIME = 0.10

    # Score for each direction
    up_score = 0.0
    down_score = 0.0

    # 1. MOMENTUM (30%)
    if momentum['direction'] == 'UP':
        up_score += momentum['strength'] * W_MOM
    elif momentum['direction'] == 'DOWN':
        down_score += momentum['strength'] * W_MOM

    # Quality boost: RSI confirms
    if momentum['rsi'] < 35 and momentum['direction'] == 'DOWN':
        # Oversold — potential bounce → reduce DOWN score
        down_score *= 0.7
    elif momentum['rsi'] > 65 and momentum['direction'] == 'UP':
        # Overbought — potential pullback → reduce UP score
        up_score *= 0.7

    # 2. CROSS-EXCHANGE DIVERGENCE (25%) — THE CHAINLINK DELAY
    if divergence['opportunity'] == 'BUY_UP':
        # Polymarket underprices UP relative to Binance
        div_strength = min(1.0, abs(divergence['divergence']) * 10)
        up_score += div_strength * W_DIV
    elif divergence['opportunity'] == 'BUY_DOWN':
        div_strength = min(1.0, abs(divergence['divergence']) * 10)
        down_score += div_strength * W_DIV

    # Divergence momentum bonus
    if divergence.get('divergence_momentum', 0) > 0.05:
        up_score *= 1.1
    elif divergence.get('divergence_momentum', 0) < -0.05:
        down_score *= 1.1

    # 3. ORDER FLOW (20%)
    if flow['direction'] == 'UP':
        flow_strength = min(1.0, abs(flow['buy_pressure'] - 0.5) * 10)
        up_score += flow_strength * W_FLOW
    elif flow['direction'] == 'DOWN':
        flow_strength = min(1.0, abs(flow['buy_pressure'] - 0.5) * 10)
        down_score += flow_strength * W_FLOW

    # Large trade confirmation
    if flow['large_trade_bias'] == 'BUY':
        up_score *= 1.05
    elif flow['large_trade_bias'] == 'SELL':
        down_score *= 1.05

    # 4. VOLUME-WEIGHTED MOMENTUM (15%)
    if vwm['direction'] == 'UP':
        up_score += vwm['strength'] * W_VWM
    elif vwm['direction'] == 'DOWN':
        down_score += vwm['strength'] * W_VWM

    # 5. TIME PRESSURE (10%)
    # Closer to expiry = our edge is stronger (less time for CLOB to adjust)
    # But not too close (under 30s — settle uncertainty)
    if 30 <= seconds_remaining <= 120:
        time_bonus = 0.8 + (120 - seconds_remaining) / 120 * 0.2
        if up_score > down_score:
            up_score += time_bonus * W_TIME
        elif down_score > up_score:
            down_score += time_bonus * W_TIME
    elif seconds_remaining > 120:
        # Plenty of time — moderate bonus
        time_bonus = 0.3
        if up_score > down_score:
            up_score += time_bonus * W_TIME
        elif down_score > up_score:
            down_score += time_bonus * W_TIME

    # Determine direction and confidence
    if up_score > down_score:
        direction = 'UP'
        confidence = up_score
        edge = up_score - down_score
    elif down_score > up_score:
        direction = 'DOWN'
        confidence = down_score
        edge = down_score - up_score
    else:
        direction = 'NEUTRAL'
        confidence = 0.0
        edge = 0.0

    # Multi-confirmation check: how many signals agree?
    signals_direction = [momentum['direction'], divergence['binance_direction'],
                        flow['direction'], vwm['direction']]
    aligned_count = sum(1 for d in signals_direction if d == direction)

    # Penalty if fewer than 2 signals agree
    if aligned_count < 2 and confidence > 0.3:
        confidence *= 0.6  # Heavy penalty for no confirmation
    elif aligned_count == 2:
        confidence *= 0.85  # Mild penalty
    elif aligned_count >= 3:
        confidence *= 1.1  # Bonus for strong agreement

    # Cap confidence
    confidence = max(0.0, min(0.95, confidence))

    # Entry recommendation
    entry_recommended = (
        confidence >= 0.35 and
        edge >= 0.05 and
        aligned_count >= 2 and
        seconds_remaining >= 30
    )

    return {
        'direction': direction,
        'confidence': confidence,
        'edge': edge,
        'aligned_signals': aligned_count,
        'entry_recommended': entry_recommended,
        'signals': {
            'momentum': momentum,
            'divergence': divergence,
            'order_flow': flow,
            'vwm': vwm,
        },
        'scores': {
            'up': up_score,
            'down': down_score,
        },
    }
