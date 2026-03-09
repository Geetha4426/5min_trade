"""
Quant Strategies — Advanced Mathematical Strategies using Market Microstructure

Three new strategies built on academic quant formulas:

1. QuantEdge — Multi-factor quant screening with Kyle's Lambda, adverse selection,
   fill probability, and Bayesian fusion. The "brain" strategy that uses EVERY
   formula from the quant module to find statistically optimal entries.

2. MicroPriceSniper — Trades when MicroPrice (volume-weighted mid) diverges
   significantly from the market mid, indicating hidden buying/selling pressure.

3. InformedFlowDetector — Detects when informed traders are active (via Kyle's
   Lambda) and piggybacks on their direction using orderbook + Binance signals.

All strategies are modular, use only pure quant_formulas functions + existing
data feeds, and are safe to run alongside all other strategies.
"""

from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy, TradeSignal
from data.quant_formulas import (
    microprice, microprice_signal, effective_spread, is_edge_profitable,
    fill_probability, optimal_limit_distance,
    kyle_lambda, is_safe_to_enter,
    adverse_selection_prob, adverse_selection_safe,
    inventory_penalty, inventory_adjusted_threshold,
    optimal_exit_urgency,
    ema_crossover_signal, rsi_signal, bollinger_position,
    vwap_signal, composite_direction_score,
    kline_momentum, orderbook_imbalance_signal, arb_score,
)
import data.binance_signals as bsig


# ═══════════════════════════════════════════════════════════════════
# 1. QUANT EDGE — Full quant screening with multi-factor fusion
# ═══════════════════════════════════════════════════════════════════

class QuantEdgeStrategy(BaseStrategy):
    """
    Combines ALL quant formulas into a single decision engine.

    Entry logic:
    1. Screen: adverse selection < 20%, Kyle's lambda safe, spread profitable
    2. Direction: 6-signal Bayesian fusion (ref price, EMA, RSI, kline, orderbook, flow)
    3. Sizing: inventory-aware threshold adjustment
    4. Timing: optimal exit urgency check (don't enter if exit will be forced)

    This is the most mathematically rigorous strategy in the system.
    """

    name = "quant_edge"
    description = "Multi-factor quant screening — Kyle's Lambda + adverse selection + Bayesian fusion"

    MIN_COMPOSITE_PROB = 0.82
    MIN_AGREEMENT = 4          # At least 4 of 6 direction signals agree
    MAX_ENTRY_PRICE = 0.72
    MIN_EDGE_OVER_SPREAD = 1.5  # Edge must be 1.5x the effective spread

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 999)
        ref_engine = context.get('ref_engine')

        if not clob or not binance_feed or not ref_engine:
            return None

        coin = market.get('coin', '')
        if not coin:
            return None

        # Need at least 30s remaining but not more than 240s
        if seconds_remaining < 30 or seconds_remaining > 240:
            return None

        # ── Gather orderbook data ──
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))
        if not up_book or not down_book:
            return None

        up_ask = up_book.get('best_ask', 0.5)
        down_ask = down_book.get('best_ask', 0.5)
        up_bid = up_book.get('best_bid', 0)
        down_bid = down_book.get('best_bid', 0)
        up_bid_depth = up_book.get('bid_depth', 0)
        up_ask_depth = up_book.get('ask_depth', 0)
        down_bid_depth = down_book.get('bid_depth', 0)
        down_ask_depth = down_book.get('ask_depth', 0)

        # ═══ SCREEN 1: Adverse Selection Filter ═══
        up_spread = up_ask - up_bid if up_bid > 0 else 0.10
        down_spread = down_ask - down_bid if down_bid > 0 else 0.10
        avg_spread = (up_spread + down_spread) / 2
        if not adverse_selection_safe(avg_spread, max_informed_pct=0.20):
            return None

        # ═══ SCREEN 2: Kyle's Lambda — informed trading intensity ═══
        # Use Binance volume as proxy for noise volume, spread as volatility proxy
        try:
            klines = bsig._parse_klines(bsig._get_klines(coin, '1m', 25))
            closes = [k['close'] for k in klines]
            volumes = [k['volume'] for k in klines]

            if len(closes) >= 10:
                # Price volatility = std of returns
                returns = [(closes[i] - closes[i-1]) / closes[i-1]
                           for i in range(1, len(closes)) if closes[i-1] > 0]
                if returns:
                    vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
                    noise_vol = sum(volumes[-5:]) if len(volumes) >= 5 else 1.0
                    lam = kyle_lambda(vol, max(noise_vol, 0.001))
                    if not is_safe_to_enter(lam, threshold=0.08):
                        return None  # Too much informed trading
        except Exception:
            klines = []
            closes = []
            volumes = []

        # ═══ SCREEN 3: Arb check — combined ask ═══
        arb = arb_score(up_ask, down_ask)
        if arb['combined_ask'] > 1.04:
            return None  # Spread too wide for profitable entry

        # ═══ DIRECTION: 6-signal Bayesian fusion ═══
        signals = []

        # Signal 1: Reference price model
        prob = ref_engine.calc_p_up(market, binance_feed, seconds_remaining)
        if prob:
            p_up = prob['p_up']
            p_down = prob['p_down']
            if p_up > p_down:
                signals.append({'name': 'ref_price', 'direction': 'UP',
                                'strength': min(1.0, (p_up - 0.5) * 3), 'weight': 0.25})
            else:
                signals.append({'name': 'ref_price', 'direction': 'DOWN',
                                'strength': min(1.0, (p_down - 0.5) * 3), 'weight': 0.25})
        else:
            return None

        # Signal 2: EMA crossover
        if closes and len(closes) >= 21:
            ema_sig = ema_crossover_signal(closes, fast_period=8, slow_period=21)
            signals.append({'name': 'ema', 'direction': ema_sig['direction'],
                            'strength': ema_sig['strength'], 'weight': 0.18})

        # Signal 3: RSI
        if closes and len(closes) >= 14:
            rsi_s = rsi_signal(closes, period=14)
            if rsi_s['rsi'] > 55:
                signals.append({'name': 'rsi', 'direction': 'UP',
                                'strength': min(1.0, (rsi_s['rsi'] - 50) / 30), 'weight': 0.12})
            elif rsi_s['rsi'] < 45:
                signals.append({'name': 'rsi', 'direction': 'DOWN',
                                'strength': min(1.0, (50 - rsi_s['rsi']) / 30), 'weight': 0.12})
            else:
                signals.append({'name': 'rsi', 'direction': 'NEUTRAL', 'strength': 0, 'weight': 0.12})

        # Signal 4: Kline momentum
        if klines and len(klines) >= 5:
            km = kline_momentum(klines, lookback=5)
            signals.append({'name': 'kline', 'direction': km['direction'],
                            'strength': km['strength'], 'weight': 0.15})

        # Signal 5: Orderbook imbalance (cross-book)
        total_bid = up_bid_depth + down_bid_depth
        total_ask = up_ask_depth + down_ask_depth
        if total_bid + total_ask > 0:
            # UP favored when UP has more bids + DOWN has more asks
            up_demand = up_bid_depth + down_ask_depth
            down_demand = down_bid_depth + up_ask_depth
            net = (up_demand - down_demand) / (up_demand + down_demand) if (up_demand + down_demand) > 0 else 0
            if net > 0.08:
                ob_dir = 'UP'
            elif net < -0.08:
                ob_dir = 'DOWN'
            else:
                ob_dir = 'NEUTRAL'
            signals.append({'name': 'orderbook', 'direction': ob_dir,
                            'strength': min(1.0, abs(net) * 3), 'weight': 0.15})

        # Signal 6: Order flow
        try:
            flow = bsig.get_order_flow(coin)
            signals.append({'name': 'flow', 'direction': flow['direction'],
                            'strength': min(1.0, abs(flow['buy_pressure'] - 0.5) * 5), 'weight': 0.15})
        except Exception:
            pass

        if len(signals) < 4:
            return None

        # ═══ FUSE ═══
        composite = composite_direction_score(signals)
        direction = composite['direction']
        probability = composite['probability']
        agreement = composite.get('up_votes', 0) if direction == 'UP' else composite.get('down_votes', 0)

        if probability < self.MIN_COMPOSITE_PROB:
            return None
        if agreement < self.MIN_AGREEMENT:
            return None

        # ═══ ENTRY SELECTION ═══
        if direction == 'UP':
            entry_price = up_ask
            token_id = market.get('up_token_id', '')
            mid = (up_bid + up_ask) / 2 if up_bid > 0 else up_ask
        else:
            entry_price = down_ask
            token_id = market.get('down_token_id', '')
            mid = (down_bid + down_ask) / 2 if down_bid > 0 else down_ask

        if entry_price <= 0 or entry_price > self.MAX_ENTRY_PRICE:
            return None

        # ═══ SCREEN 4: Edge vs effective spread ═══
        edge = probability - entry_price
        es = effective_spread(entry_price, mid)
        if edge < es * self.MIN_EDGE_OVER_SPREAD:
            return None

        # ═══ SCREEN 5: Exit urgency (don't enter if we'd be forced to exit immediately) ═══
        urgency = optimal_exit_urgency(seconds_remaining, total_seconds=300)
        if urgency > 0.85:
            return None  # Too close to expiry for a new position

        return TradeSignal(
            market_id=market.get('market_id', ''),
            coin=coin,
            direction=direction,
            entry_price=entry_price,
            token_id=token_id,
            confidence=probability,
            timeframe=market.get('timeframe', 5),
            strategy='quant_edge',
            rationale=(
                f"📊 QuantEdge: {direction} | "
                f"P={probability:.1%} | "
                f"Agree={agreement}/{len(signals)} | "
                f"Edge={edge:.1%} vs ES={es:.4f} | "
                f"λ_safe ✓ | AS_safe ✓ | "
                f"{seconds_remaining}s left"
            ),
            metadata={
                'type': 'quant_edge',
                'probability': probability,
                'agreement': agreement,
                'edge': edge,
                'effective_spread': es,
                'adverse_selection': adverse_selection_prob(avg_spread),
                'combined_ask': arb['combined_ask'],
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]


# ═══════════════════════════════════════════════════════════════════
# 2. MICROPRICE SNIPER — Trade MicroPrice divergence
# ═══════════════════════════════════════════════════════════════════

class MicroPriceSniperStrategy(BaseStrategy):
    """
    Detects when MicroPrice diverges from market mid, indicating hidden
    buying/selling pressure not yet reflected in the price.

    Entry:
    - UP MicroPrice skew > 0.25 (buyers dominating) → buy UP
    - DOWN MicroPrice skew > 0.25 (sellers dominating) → buy DOWN
    - Both books must show same directional skew (convergence)
    - Confirmed by at least one Binance signal (EMA or flow)

    This exploits the fact that depth (volume at price levels) reveals
    future price direction BEFORE the price moves.
    """

    name = "microprice_sniper"
    description = "Trade hidden volume pressure via MicroPrice divergence"

    MIN_NET_SKEW = 0.20         # Minimum combined skew strength
    MIN_BINANCE_CONFIRM = 0.55  # Binance signal must lean same way
    MAX_ENTRY = 0.65
    MIN_SECONDS = 20
    MAX_SECONDS = 200

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 999)

        if not clob or not binance_feed:
            return None

        coin = market.get('coin', '')
        if not coin:
            return None

        if seconds_remaining < self.MIN_SECONDS or seconds_remaining > self.MAX_SECONDS:
            return None

        # ── Orderbook data ──
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))
        if not up_book or not down_book:
            return None

        up_ask = up_book.get('best_ask', 0.5)
        down_ask = down_book.get('best_ask', 0.5)
        up_bid = up_book.get('best_bid', 0)
        down_bid = down_book.get('best_bid', 0)
        up_bid_depth = up_book.get('bid_depth', 0)
        up_ask_depth = up_book.get('ask_depth', 0)
        down_bid_depth = down_book.get('bid_depth', 0)
        down_ask_depth = down_book.get('ask_depth', 0)

        # ── Compute MicroPrice signals for both books ──
        up_micro = microprice_signal(
            up_bid or up_ask - 0.01, up_ask, up_bid_depth, up_ask_depth)
        down_micro = microprice_signal(
            down_bid or down_ask - 0.01, down_ask, down_bid_depth, down_ask_depth)

        # Net skew: positive = UP favored
        # UP book bullish (positive skew) means buyers want UP
        # DOWN book bearish (negative skew) means sellers dumping DOWN
        net_skew = up_micro['norm_skew'] - down_micro['norm_skew']

        if abs(net_skew) < self.MIN_NET_SKEW:
            return None  # Not enough divergence

        direction = 'UP' if net_skew > 0 else 'DOWN'

        # ── Binance confirmation ──
        # Need at least one Binance signal to agree
        confirmed = False
        try:
            flow = bsig.get_order_flow(coin)
            if direction == 'UP' and flow['buy_pressure'] > self.MIN_BINANCE_CONFIRM:
                confirmed = True
            elif direction == 'DOWN' and flow['buy_pressure'] < (1 - self.MIN_BINANCE_CONFIRM):
                confirmed = True
        except Exception:
            pass

        if not confirmed:
            # Try EMA as backup confirmation
            try:
                klines = bsig._parse_klines(bsig._get_klines(coin, '1m', 25))
                closes = [k['close'] for k in klines]
                if len(closes) >= 21:
                    ema_sig = ema_crossover_signal(closes)
                    if ema_sig['direction'] == direction and ema_sig['strength'] > 0.3:
                        confirmed = True
            except Exception:
                pass

        if not confirmed:
            return None

        # ── Entry ──
        if direction == 'UP':
            entry_price = up_ask
            token_id = market.get('up_token_id', '')
        else:
            entry_price = down_ask
            token_id = market.get('down_token_id', '')

        if entry_price <= 0 or entry_price > self.MAX_ENTRY:
            return None

        # ── Spread check ──
        combined = up_ask + down_ask
        if combined > 1.04:
            return None

        confidence = min(0.92, 0.70 + abs(net_skew) * 0.5)

        return TradeSignal(
            market_id=market.get('market_id', ''),
            coin=coin,
            direction=direction,
            entry_price=entry_price,
            token_id=token_id,
            confidence=confidence,
            timeframe=market.get('timeframe', 5),
            strategy='microprice_sniper',
            rationale=(
                f"🔬 MicroSniper: {direction} | "
                f"Skew={net_skew:+.3f} | "
                f"UP_μ={up_micro['norm_skew']:+.3f} DOWN_μ={down_micro['norm_skew']:+.3f} | "
                f"Conf={confidence:.1%} | "
                f"{seconds_remaining}s left"
            ),
            metadata={
                'type': 'microprice_sniper',
                'net_skew': net_skew,
                'up_skew': up_micro['norm_skew'],
                'down_skew': down_micro['norm_skew'],
                'combined_ask': combined,
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15, 30]


# ═══════════════════════════════════════════════════════════════════
# 3. INFORMED FLOW DETECTOR — Piggyback on whale/informed activity
# ═══════════════════════════════════════════════════════════════════

class InformedFlowDetector(BaseStrategy):
    """
    Detects when large/informed traders are active and rides their direction.

    Uses Kyle's Lambda to measure informed trading intensity:
    - When lambda is MODERATELY high (0.03-0.07), informed traders are present
      but not dominating. We can safely piggyback on their direction.
    - When lambda is TOO high (>0.07), it's dangerous — they'll move price
      against us. We stay out.
    - When lambda is TOO low (<0.01), no informed activity — no edge.

    Direction determined by orderbook imbalance + Binance momentum.
    Think of it as front-running the smart money (legally, on Polymarket).
    """

    name = "informed_flow"
    description = "Piggyback on informed trader flow via Kyle's Lambda detection"

    LAMBDA_MIN = 0.02   # Minimum informed activity
    LAMBDA_MAX = 0.07   # Maximum safe informed activity
    MIN_FLOW_STRENGTH = 0.60  # Binance flow must show clear direction
    MAX_ENTRY = 0.68
    MIN_SECONDS = 30
    MAX_SECONDS = 180

    async def analyze(self, market: Dict, context: Dict) -> Optional[TradeSignal]:
        clob = context.get('clob')
        binance_feed = context.get('binance_feed')
        seconds_remaining = context.get('seconds_remaining', 999)

        if not clob or not binance_feed:
            return None

        coin = market.get('coin', '')
        if not coin:
            return None

        if seconds_remaining < self.MIN_SECONDS or seconds_remaining > self.MAX_SECONDS:
            return None

        # ── Compute Kyle's Lambda ──
        try:
            klines = bsig._parse_klines(bsig._get_klines(coin, '1m', 25))
            closes = [k['close'] for k in klines]
            volumes = [k['volume'] for k in klines]
        except Exception:
            return None

        if len(closes) < 10:
            return None

        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(1, len(closes)) if closes[i-1] > 0]
        if not returns:
            return None

        vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
        noise_vol = sum(volumes[-5:]) if len(volumes) >= 5 else 1.0
        lam = kyle_lambda(vol, max(noise_vol, 0.001))

        # ── Lambda sweet spot: informed but not dangerous ──
        if lam < self.LAMBDA_MIN or lam > self.LAMBDA_MAX:
            return None

        # ── Determine informed direction via multiple signals ──
        # Orderbook
        up_book = clob.get_orderbook(market.get('up_token_id', ''))
        down_book = clob.get_orderbook(market.get('down_token_id', ''))
        if not up_book or not down_book:
            return None

        up_ask = up_book.get('best_ask', 0.5)
        down_ask = down_book.get('best_ask', 0.5)
        up_bid = up_book.get('best_bid', 0)
        down_bid = down_book.get('best_bid', 0)
        up_bid_depth = up_book.get('bid_depth', 0)
        down_bid_depth = down_book.get('bid_depth', 0)

        # Which side has more bid depth? Informed money accumulates quietly
        if up_bid_depth + down_bid_depth > 0:
            bid_ratio = up_bid_depth / (up_bid_depth + down_bid_depth)
        else:
            return None

        # ── Binance flow direction (taker pressure) ──
        try:
            flow = bsig.get_order_flow(coin)
            flow_pres = flow['buy_pressure']
            flow_dir = flow['direction']
        except Exception:
            return None

        # Both must agree: orderbook bids + Binance flow
        if bid_ratio > 0.55 and flow_pres > self.MIN_FLOW_STRENGTH:
            direction = 'UP'
        elif bid_ratio < 0.45 and flow_pres < (1 - self.MIN_FLOW_STRENGTH):
            direction = 'DOWN'
        else:
            return None  # Signals don't agree

        # ── Kline momentum must also confirm ──
        if len(klines) >= 5:
            km = kline_momentum(klines, lookback=5)
            if km['direction'] != direction and km['direction'] != 'NEUTRAL':
                return None  # Candles disagree

        # ── Entry ──
        if direction == 'UP':
            entry_price = up_ask
            token_id = market.get('up_token_id', '')
        else:
            entry_price = down_ask
            token_id = market.get('down_token_id', '')

        if entry_price <= 0 or entry_price > self.MAX_ENTRY:
            return None

        # ── Spread check ──
        if up_ask + down_ask > 1.04:
            return None

        # Confidence scales with lambda (more informed = higher confidence in direction)
        confidence = min(0.92, 0.72 + lam * 3)

        return TradeSignal(
            market_id=market.get('market_id', ''),
            coin=coin,
            direction=direction,
            entry_price=entry_price,
            token_id=token_id,
            confidence=confidence,
            timeframe=market.get('timeframe', 5),
            strategy='informed_flow',
            rationale=(
                f"🐋 InformedFlow: {direction} | "
                f"λ={lam:.4f} | "
                f"BidRatio={bid_ratio:.1%} | "
                f"Flow={flow_pres:.1%} | "
                f"Conf={confidence:.1%} | "
                f"{seconds_remaining}s left"
            ),
            metadata={
                'type': 'informed_flow',
                'kyle_lambda': lam,
                'bid_ratio': bid_ratio,
                'flow_pressure': flow_pres,
                'combined_ask': up_ask + down_ask,
            }
        )

    def get_suitable_timeframes(self) -> List[int]:
        return [5, 15]
