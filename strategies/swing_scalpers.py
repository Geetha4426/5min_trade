"""
Swing Scalpers — Aggressive Strategies for 5min Market Volatility

These strategies exploit the WILD price swings in 5-minute crypto markets
where prices routinely go 10¢→90¢→10¢ within seconds.

Strategies (4 new):
1. Mean Reversion Scalper — buy after crash, sell into spike
2. Spike Fade — sell/short the overextended side when it pumps to 85¢+
3. Expiry Rush — last 60s aggressive plays on clear momentum
4. Binance Momentum Sniper — align with real BTC direction for max conviction

These are AGGRESSIVE strategies designed for the $3→$10k journey.
"""

import time
import math
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal


class MeanReversionScalper(BaseStrategy):
    """
    Buy after a sharp crash, expecting a bounce.
    
    THE EDGE: In 5min markets, when a side crashes from 50¢+ to under 20¢
    in seconds, it often bounces back 10-30¢ as:
    - Panic sellers finish dumping
    - Bargain hunters buy the dip
    - Market makers re-enter
    
    We buy AFTER the crash (not during), ensuring the bottom is forming.
    
    TRIGGERS:
    - Price dropped 25¢+ in the last 20 seconds
    - Current price is under 30¢ (cheap entry)
    - Price has STOPPED falling (last 2 ticks stable or up)
    - At least 20s left before expiry
    
    EXIT: Sell at 2x+ entry or cut at -15%
    """

    name = "mean_reversion"
    description = "Buys after sharp crashes — catches the bounce for 2-5x returns"

    # How much drop triggers a trade (in cents)
    MIN_DROP = 0.20          # At least 20¢ drop
    MAX_ENTRY_PRICE = 0.35   # Only buy if price is under 35¢ (cheap)
    MIN_ENTRY_PRICE = 0.01   # Must have some value
    LOOKBACK_SECS = 20       # Check last 20 seconds for the drop
    MIN_TICKS_STABLE = 2     # Last 2 ticks must be stable/rising (bottom forming)
    MIN_SECONDS_LEFT = 45    # Need enough time for the bounce (was 20 — too risky)

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        poly_feed = context.get('poly_feed')
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not poly_feed or not clob:
            return None

        if seconds_remaining < self.MIN_SECONDS_LEFT:
            return None

        # ── Binance confirmation: only buy crash if Binance is NOT
        #    confirming the crash direction (flat or reversing) ──
        # If Binance confirms the crash → it's a REAL move, no bounce.
        # If Binance is flat/reversing → crash was overreaction → bounce likely.
        binance_direction = None
        if binance_feed:
            coin = market.get('coin', '')
            price_hist = binance_feed.price_history.get(coin)
            if price_hist and len(price_hist) >= 2:
                now_b = time.time()
                recent_b = [s for s in price_hist if s.timestamp > now_b - 30]
                if len(recent_b) >= 2:
                    btc_change = (recent_b[-1].price - recent_b[0].price) / recent_b[0].price * 100
                    if btc_change > 0.03:
                        binance_direction = 'UP'
                    elif btc_change < -0.03:
                        binance_direction = 'DOWN'
                    # else: flat → no filter applied

        for side, token_key in [('UP', 'up_token_id'), ('DOWN', 'down_token_id')]:
            token_id = market.get(token_key, '')
            if not token_id:
                continue

            history = poly_feed.price_history.get(token_id)
            if not history or len(history) < 5:
                continue

            now = time.time()
            recent = [s for s in history if s.timestamp > now - self.LOOKBACK_SECS]
            if len(recent) < 4:
                continue

            prices = [s.price for s in recent]
            peak = max(prices)
            current = prices[-1]
            drop = peak - current

            # TRIGGER: big drop + cheap price
            if drop < self.MIN_DROP:
                continue
            if not (self.MIN_ENTRY_PRICE < current <= self.MAX_ENTRY_PRICE):
                continue

            # ── Binance filter: skip if Binance confirms the crash ──
            # UP crashed → Binance is DOWN (confirms) → skip
            # DOWN crashed → Binance is UP (confirms) → skip
            if binance_direction:
                crash_confirmed = (
                    (side == 'UP' and binance_direction == 'DOWN') or
                    (side == 'DOWN' and binance_direction == 'UP')
                )
                if crash_confirmed:
                    continue  # Crash is real, no bounce expected

            # BOTTOM CHECK: last 2 ticks should be stable or rising
            if len(prices) >= 3:
                last3 = prices[-3:]
                if last3[-1] < last3[-2] and last3[-2] < last3[-3]:
                    continue  # Still falling — don't catch a falling knife

            book = clob.get_orderbook(token_id)
            if not book or book['ask_depth'] < 0.50:
                continue

            ask = book['best_ask']
            if ask > self.MAX_ENTRY_PRICE:
                continue

            # Potential return: if it bounces to 50% of the drop
            bounce_target = current + (drop * 0.5)
            potential_return = bounce_target / ask if ask > 0 else 0

            # Confidence: bigger drop + more stable bottom = higher
            confidence = min(0.85, 0.55 + drop * 1.5)

            # Boost if Binance is actively REVERSING toward our side
            binance_supports = (
                (side == 'UP' and binance_direction == 'UP') or
                (side == 'DOWN' and binance_direction == 'DOWN')
            )
            if binance_supports:
                confidence = min(0.92, confidence + 0.06)

            # Boost if we have very cheap entry + big drop (but cap at 0.88
            # to stay below SEED's 0.90 confidence floor — ultra-cheap mean
            # reversion is still a lottery bet, not a high-conviction arb)
            if current < 0.10 and drop > 0.40:
                confidence = min(0.88, confidence + 0.08)

            return TradeSignal(
                strategy=self.name,
                coin=market['coin'],
                timeframe=market['timeframe'],
                direction=side,
                token_id=token_id,
                market_id=market['market_id'],
                entry_price=ask,
                confidence=confidence,
                rationale=(
                    f"📉🔄 MEAN REVERSION: {market['coin']} {side} "
                    f"crashed {drop:.2f} ({peak:.2f}→{current:.2f}) in {self.LOOKBACK_SECS}s. "
                    f"Bottom forming. Buy @ {ask:.3f}, target {bounce_target:.2f} "
                    f"({potential_return:.1f}x)"
                ),
                metadata={
                    'drop': drop, 'peak': peak, 'current': current,
                    'bounce_target': bounce_target,
                    'potential_return': potential_return,
                    'type': 'mean_reversion',
                }
            )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]


class SpikeFade(BaseStrategy):
    """
    Buy the OPPOSITE side when one side spikes to extremes.
    
    THE EDGE: When UP pumps to 85¢+, DOWN is dirt cheap (15¢ or less).
    In volatile 5min markets, the spike often fades back — DOWN bounces.
    Instead of "shorting" the spike (can't short on Polymarket), we
    buy the cheap opposite side.
    
    MATH:
    - UP spikes to 90¢ → DOWN drops to ~10¢
    - If DOWN bounces to 30¢, that's a 3x return
    - Works in reverse too: DOWN spikes → buy cheap UP
    
    TRIGGERS:
    - One side is at 82¢+ (overextended)
    - The opposite side is at 18¢ or less
    - Recent rapid price movement (spike, not gradual)
    """

    name = "spike_fade"
    description = "Buys cheap opposite side when one side spikes to extremes"

    SPIKE_THRESHOLD = 0.82     # One side must be 82¢+
    CHEAP_THRESHOLD = 0.18     # Other side must be 18¢ or less
    MIN_SPIKE_SPEED = 0.10     # Must have moved 10¢+ in last 20s (genuine spike)
    MIN_SECONDS_LEFT = 60      # Need enough time for the fade (raised from 45)
    MIN_ENTRY_PRICE = 0.03     # No penny bets — 1¢-2¢ entries have zero fade time

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        poly_feed = context.get('poly_feed')
        clob = context.get('clob')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not poly_feed or not clob:
            return None

        if seconds_remaining < self.MIN_SECONDS_LEFT:
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

        # Check for spike + cheap opposite
        candidates = []

        if up_ask >= self.SPIKE_THRESHOLD and down_ask <= self.CHEAP_THRESHOLD:
            # UP spiked → buy cheap DOWN
            candidates.append(('DOWN', down_token, down_ask, down_book, 'UP', up_token, up_ask))
        if down_ask >= self.SPIKE_THRESHOLD and up_ask <= self.CHEAP_THRESHOLD:
            # DOWN spiked → buy cheap UP
            candidates.append(('UP', up_token, up_ask, up_book, 'DOWN', down_token, down_ask))

        if not candidates:
            return None

        # ── Binance confirmation: only fade the spike if Binance is NOT
        #    confirming the spike direction (flat or reversing) ──
        # If Binance confirms the spike → it's a REAL move, won't fade.
        # If Binance is flat/reversing → overreaction → fade likely.
        binance_direction = None
        if poly_feed:
            binance_feed = context.get('binance_feed')
            if binance_feed:
                coin = market.get('coin', '')
                price_hist = binance_feed.price_history.get(coin)
                if price_hist and len(price_hist) >= 2:
                    now_b = time.time()
                    recent_b = [s for s in price_hist if s.timestamp > now_b - 30]
                    if len(recent_b) >= 2:
                        btc_change = (recent_b[-1].price - recent_b[0].price) / recent_b[0].price * 100
                        if btc_change > 0.03:
                            binance_direction = 'UP'
                        elif btc_change < -0.03:
                            binance_direction = 'DOWN'

        for buy_side, buy_token, buy_price, buy_book, spike_side, spike_token, spike_price in candidates:
            # Verify it's a genuine SPIKE (not gradual drift)
            history = poly_feed.price_history.get(spike_token)
            if history and len(history) >= 3:
                now = time.time()
                recent = [s for s in history if s.timestamp > now - 20]
                if len(recent) >= 2:
                    move = recent[-1].price - recent[0].price
                    if abs(move) < self.MIN_SPIKE_SPEED:
                        continue  # Gradual — not a spike

            # ── Binance filter: skip if Binance confirms the spike ──
            # UP spiked + Binance UP → spike is real → don't fade
            # DOWN spiked + Binance DOWN → spike is real → don't fade
            if binance_direction == spike_side:
                continue  # Spike confirmed by Binance — won't fade

            if buy_book['ask_depth'] < 0.30:
                continue

            # Reject ultra-cheap entries — no time for a fade at pennies
            if buy_price < self.MIN_ENTRY_PRICE:
                continue

            # Scale time requirement by price: cheaper = need MORE time
            # At 3¢ need 60s, at 10¢ need 45s, at 18¢ need 30s
            min_time_needed = max(30, int(60 - (buy_price - 0.03) * 200))
            if seconds_remaining < min_time_needed:
                continue

            # Calculate potential
            potential_return = 0.30 / buy_price if buy_price > 0 else 0 # Target 30¢ bounce

            confidence = min(0.82, 0.50 + (spike_price - 0.80) * 2 + (0.20 - buy_price) * 1.5)

            # REQUIRE Binance opposition — if Binance is flat, skip entirely.
            # Spike fades at low prices ($0.10-0.14) are coin flips without
            # Binance confirmation that the spike is overextended.
            binance_opposes_spike = (
                binance_direction is not None and
                binance_direction != spike_side
            )
            if not binance_opposes_spike:
                continue  # No Binance reversal signal → skip this candidate
            # Binance confirms reversal → boost
            confidence = min(0.90, confidence + 0.08)

            return TradeSignal(
                strategy=self.name,
                coin=market['coin'],
                timeframe=market['timeframe'],
                direction=buy_side,
                token_id=buy_token,
                market_id=market['market_id'],
                entry_price=buy_price,
                confidence=confidence,
                rationale=(
                    f"🔥🧊 SPIKE FADE: {market['coin']} {spike_side} spiked to "
                    f"{spike_price:.2f}! Buying cheap {buy_side} @ {buy_price:.3f} "
                    f"for fade. Target: {potential_return:.1f}x return"
                ),
                metadata={
                    'spike_side': spike_side, 'spike_price': spike_price,
                    'buy_price': buy_price,
                    'potential_return': potential_return,
                    'type': 'spike_fade',
                }
            )

        return None

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]


class ExpiryRush(BaseStrategy):
    """
    Aggressive last-60-seconds plays.
    
    THE EDGE: In the final minute of 5min markets, prices become
    extremely volatile as traders rush to exit or take positions.
    We look for ONE clear signal and go all-in:
    
    TRIGGERS:
    - 10-60 seconds remaining
    - One side has strong momentum in last 15 seconds
    - Binance price confirms the direction
    - Buy the side with momentum — it often accelerates into expiry
    
    This is different from prob_closer: we don't need 80%+ price,
    we look for MOMENTUM. A side going from 30¢→50¢ with 30s left
    and Binance confirming is a strong buy.
    """

    name = "expiry_rush"
    description = "Aggressive last-minute plays on clear momentum"

    MAX_SECONDS = 60           # Last 60 seconds
    MIN_SECONDS = 15           # Not too close to settlement (need time to exit)
    MIN_MOMENTUM = 0.08        # At least 8¢ move in last 15s
    MAX_ENTRY = 0.75           # Don't overpay (under 75¢)
    MIN_ENTRY = 0.10           # Need reasonable price

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        poly_feed = context.get('poly_feed')
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not poly_feed or not clob:
            return None

        if seconds_remaining > self.MAX_SECONDS or seconds_remaining < self.MIN_SECONDS:
            return None

        coin = market['coin']

        # Get Binance direction for confirmation
        binance_direction = None
        if binance_feed:
            price_hist = binance_feed.price_history.get(coin)
            if price_hist and len(price_hist) >= 2:
                recent_px = [s for s in price_hist if s.timestamp > time.time() - 30]
                if len(recent_px) >= 2:
                    btc_change = recent_px[-1].price - recent_px[0].price
                    if btc_change > 0:
                        binance_direction = 'UP'
                    elif btc_change < 0:
                        binance_direction = 'DOWN'

        best_signal = None
        best_conf = 0

        for side, token_key in [('UP', 'up_token_id'), ('DOWN', 'down_token_id')]:
            token_id = market.get(token_key, '')
            if not token_id:
                continue

            history = poly_feed.price_history.get(token_id)
            if not history or len(history) < 3:
                continue

            now = time.time()
            recent = [s for s in history if s.timestamp > now - 15]
            if len(recent) < 2:
                continue

            prices = [s.price for s in recent]
            momentum = prices[-1] - prices[0]  # Positive = price going up

            # Need clear upward momentum for this side
            if momentum < self.MIN_MOMENTUM:
                continue

            current = prices[-1]
            if not (self.MIN_ENTRY <= current <= self.MAX_ENTRY):
                continue

            book = clob.get_orderbook(token_id)
            if not book or book['ask_depth'] < 0.30:
                continue

            ask = book['best_ask']
            if ask > self.MAX_ENTRY:
                continue

            # Confidence: base + momentum strength + Binance confirmation
            confidence = min(0.92, 0.50 + momentum * 2)
            
            # Binance confirmation is a BIG boost
            if binance_direction == side:
                confidence = min(0.95, confidence + 0.15)
            elif binance_direction and binance_direction != side:
                confidence -= 0.10  # Counter-signal — reduce confidence

            # Time pressure boost: closer to expiry = stronger signal
            if seconds_remaining < 30:
                confidence = min(0.95, confidence + 0.05)

            # Reference price edge boost
            edge_info = ''
            ref_engine = context.get('ref_engine')
            if ref_engine:
                edge = ref_engine.calc_edge(
                    market, binance_feed, seconds_remaining,
                    market.get('up_price', 0.5), market.get('down_price', 0.5)
                )
                if edge:
                    side_edge = edge['up_edge'] if side == 'UP' else edge['down_edge']
                    if side_edge > 0.10:  # >10% edge — strong model agreement
                        confidence = min(0.97, confidence + 0.05)
                        edge_info = f" Edge:{side_edge:.0%}"
                    elif side_edge < -0.10:  # Model disagrees — demote
                        confidence -= 0.08

            if confidence > best_conf:
                best_conf = confidence
                best_signal = TradeSignal(
                    strategy=self.name,
                    coin=market['coin'],
                    timeframe=market['timeframe'],
                    direction=side,
                    token_id=token_id,
                    market_id=market['market_id'],
                    entry_price=ask,
                    confidence=confidence,
                    rationale=(
                        f"⏰🚀 EXPIRY RUSH: {market['coin']} {side} "
                        f"momentum +{momentum:.2f} in 15s! "
                        f"{seconds_remaining}s left. "
                        f"Binance: {'✅ confirms' if binance_direction == side else '❌ no confirm'}."
                        f"{edge_info} Buy @ {ask:.3f}"
                    ),
                    metadata={
                        'momentum': momentum,
                        'seconds_remaining': seconds_remaining,
                        'binance_confirms': binance_direction == side,
                        'type': 'expiry_rush',
                    }
                )

        return best_signal

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]


class BinanceMomentumSniper(BaseStrategy):
    """
    High-conviction directional play aligned with real BTC movement.
    
    THE EDGE: Polymarket prices lag behind Binance by 1-5 seconds.
    When BTC makes a strong directional move on Binance, the Polymarket
    5min market hasn't fully repriced yet. We buy the direction
    Binance is showing BEFORE Polymarket catches up.
    
    DIFFERENT from Oracle Arb: 
    - Oracle Arb uses a fixed threshold (1% delta)
    - This uses RATE OF CHANGE (acceleration) + volume confirmation
    - This catches sudden moves, not just static mispricings
    
    TRIGGERS:
    - BTC moved 0.15%+ in last 30 seconds on Binance (strong move)
    - Polymarket price hasn't caught up (the corresponding side is still cheap)
    - At least 30s remaining
    """

    name = "binance_momentum"
    description = "Catches Binance→Polymarket price lag for directional alpha"

    MIN_BTC_MOVE_PCT = 0.12     # BTC must move 0.12%+ in 30s
    MAX_POLY_PRICE = 0.60       # Polymarket side should still be under 60¢
    MIN_POLY_PRICE = 0.08       # Above 8¢
    LOOKBACK_SECS = 30          # Watch last 30 seconds of BTC
    MIN_SECONDS_LEFT = 30       # Need time for Polymarket to reprice

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob or not binance_feed:
            return None

        if seconds_remaining < self.MIN_SECONDS_LEFT:
            return None

        coin = market['coin']
        price_hist = binance_feed.price_history.get(coin)
        if not price_hist or len(price_hist) < 3:
            return None

        now = time.time()
        recent = [s for s in price_hist if s.timestamp > now - self.LOOKBACK_SECS]
        if len(recent) < 2:
            return None

        # Calculate BTC move percentage
        start_price = recent[0].price
        end_price = recent[-1].price
        if start_price <= 0:
            return None
        
        btc_change_pct = (end_price - start_price) / start_price * 100

        if abs(btc_change_pct) < self.MIN_BTC_MOVE_PCT:
            return None

        # Determine direction
        if btc_change_pct > 0:
            poly_side = 'UP'
            poly_token = market.get('up_token_id', '')
        else:
            poly_side = 'DOWN'
            poly_token = market.get('down_token_id', '')

        if not poly_token:
            return None

        book = clob.get_orderbook(poly_token)
        if not book:
            return None

        ask = book['best_ask']

        # Check Polymarket hasn't already caught up
        if not (self.MIN_POLY_PRICE <= ask <= self.MAX_POLY_PRICE):
            return None

        if book['ask_depth'] < 0.50:
            return None

        # Confidence: stronger BTC move + cheaper Poly price = higher
        move_strength = abs(btc_change_pct)
        confidence = min(0.92, 0.55 + move_strength * 3 + (0.50 - ask) * 0.5)

        # Reference price edge boost
        ref_engine = context.get('ref_engine')
        if ref_engine:
            edge = ref_engine.calc_edge(
                market, binance_feed, seconds_remaining,
                market.get('up_price', 0.5), market.get('down_price', 0.5)
            )
            if edge:
                side_edge = edge['up_edge'] if poly_side == 'UP' else edge['down_edge']
                if side_edge > 0.10:
                    confidence = min(0.95, confidence + 0.05)
                elif side_edge < -0.10:
                    confidence -= 0.08

        # Extra boost for very strong moves
        if move_strength > 0.30:
            confidence = min(0.95, confidence + 0.08)

        return TradeSignal(
            strategy=self.name,
            coin=market['coin'],
            timeframe=market['timeframe'],
            direction=poly_side,
            token_id=poly_token,
            market_id=market['market_id'],
            entry_price=ask,
            confidence=confidence,
            rationale=(
                f"⚡📊 BINANCE MOMENTUM: {coin} {btc_change_pct:+.3f}% in "
                f"{self.LOOKBACK_SECS}s on Binance! Polymarket {poly_side} "
                f"still at {ask:.3f} — lagging. Buy before reprice!"
            ),
            metadata={
                'btc_change_pct': btc_change_pct,
                'poly_price': ask,
                'move_strength': move_strength,
                'type': 'binance_momentum',
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]


class OrderbookImbalance(BaseStrategy):
    """Orderbook Imbalance — uses bid/ask depth ratio as directional signal.

    If 75%+ of depth is on bid side → buyers dominating → likely UP.
    If 75%+ of depth is on ask side → sellers dominating → likely DOWN.
    Combined with Binance direction for confirmation.
    """

    name = "book_imbalance"
    description = "Trade direction of orderbook depth imbalance"

    MIN_IMBALANCE = 0.50       # |imbalance| > 0.50  (75/25 depth split)
    MIN_TOTAL_DEPTH = 3.0      # $3 total depth minimum (meaningful book)
    MAX_ENTRY = 0.65           # Don't overpay
    MIN_ENTRY = 0.08           # Avoid dust
    MIN_SECONDS = 45           # Need time for move to play out
    MAX_SECONDS = 240          # Not too early (noise)

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 0)

        if not clob:
            return None

        if seconds_remaining < self.MIN_SECONDS or seconds_remaining > self.MAX_SECONDS:
            return None

        coin = market['coin']
        best_signal = None
        best_conf = 0

        for side, token_key in [('UP', 'up_token_id'), ('DOWN', 'down_token_id')]:
            token_id = market.get(token_key, '')
            if not token_id:
                continue

            book = clob.get_orderbook(token_id)
            if not book:
                continue

            imbalance = book.get('imbalance', 0)
            bid_depth = book.get('bid_depth', 0)
            ask_depth = book.get('ask_depth', 0)
            total_depth = bid_depth + ask_depth

            if total_depth < self.MIN_TOTAL_DEPTH:
                continue

            # For UP side: positive imbalance = more bids = bullish
            # For DOWN side: negative imbalance = more asks = bearish for UP = bullish for DOWN
            if side == 'UP' and imbalance < self.MIN_IMBALANCE:
                continue
            if side == 'DOWN' and imbalance > -self.MIN_IMBALANCE:
                continue

            ask = book.get('best_ask', 1.0)
            if not (self.MIN_ENTRY <= ask <= self.MAX_ENTRY):
                continue

            abs_imbalance = abs(imbalance)
            # Base confidence from imbalance strength
            confidence = min(0.88, 0.55 + abs_imbalance * 0.30)

            # Binance confirmation boost
            binance_confirms = False
            if binance_feed:
                price_hist = binance_feed.price_history.get(coin)
                if price_hist and len(price_hist) >= 2:
                    recent_px = [s for s in price_hist if s.timestamp > time.time() - 30]
                    if len(recent_px) >= 2:
                        btc_dir = 'UP' if recent_px[-1].price > recent_px[0].price else 'DOWN'
                        if btc_dir == side:
                            confidence = min(0.93, confidence + 0.10)
                            binance_confirms = True
                        else:
                            confidence -= 0.05  # Counter-signal

            # Cheaper price = more upside = higher confidence
            if ask < 0.30:
                confidence = min(0.93, confidence + 0.03)

            # Reference price edge boost
            ref_engine = context.get('ref_engine')
            if ref_engine:
                edge = ref_engine.calc_edge(
                    market, binance_feed, seconds_remaining,
                    market.get('up_price', 0.5), market.get('down_price', 0.5)
                )
                if edge:
                    side_edge = edge['up_edge'] if side == 'UP' else edge['down_edge']
                    if side_edge > 0.10:
                        confidence = min(0.95, confidence + 0.05)
                    elif side_edge < -0.10:
                        confidence -= 0.08

            if confidence > best_conf:
                best_conf = confidence
                depth_ratio = bid_depth / total_depth if side == 'UP' else ask_depth / total_depth
                best_signal = TradeSignal(
                    strategy=self.name,
                    coin=coin,
                    timeframe=market['timeframe'],
                    direction=side,
                    token_id=token_id,
                    market_id=market['market_id'],
                    entry_price=ask,
                    confidence=confidence,
                    rationale=(
                        f"📊 BOOK IMBALANCE: {coin} {side} — "
                        f"depth ratio {depth_ratio:.0%} "
                        f"(bid=${bid_depth:.1f} ask=${ask_depth:.1f}). "
                        f"Binance: {'✅' if binance_confirms else '❌'}. "
                        f"{seconds_remaining}s left. Buy @ {ask:.3f}"
                    ),
                    metadata={
                        'imbalance': imbalance,
                        'bid_depth': bid_depth,
                        'ask_depth': ask_depth,
                        'binance_confirms': binance_confirms,
                        'type': 'book_imbalance',
                    }
                )

        return best_signal

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]
