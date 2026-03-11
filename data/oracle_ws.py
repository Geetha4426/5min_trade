"""
Enhanced Binance Oracle Lead — Real-time 1s Kline WebSocket

Based on 0xLanister research: Binance leads Polymarket by 4-12 seconds
via Chainlink Data Streams oracle delay.

Uses btcusdt@kline_1s WebSocket for sub-second price detection.
This is FASTER than the existing REST-based binance_signals.py (5s cache).

Provides:
1. Real-time 1-second candle stream
2. Impulse detection (large move in 1-3 seconds)
3. Last-seconds timing signal (3-12s before candle close)
"""

import asyncio
import json
import time
import math
from typing import Dict, List, Optional, Callable
from collections import deque
from config import Config


# WebSocket URL for 1s klines
BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"

# Symbol streams for all supported coins
COIN_STREAMS = {
    'BTC': 'btcusdt@kline_1s',
    'ETH': 'ethusdt@kline_1s',
    'SOL': 'solusdt@kline_1s',
    'XRP': 'xrpusdt@kline_1s',
}


class OracleLeadSignal:
    """A real-time signal from Binance WS indicating Polymarket lag."""

    def __init__(self, coin: str, direction: str, magnitude: float,
                 confidence: float, seconds_since_move: float,
                 binance_price: float, reason: str):
        self.coin = coin
        self.direction = direction  # "UP" or "DOWN"
        self.magnitude = magnitude  # % move size
        self.confidence = confidence  # 0-1
        self.seconds_since_move = seconds_since_move
        self.binance_price = binance_price
        self.reason = reason
        self.timestamp = time.time()

    @property
    def is_actionable(self) -> bool:
        """Signal is still fresh enough to act on (within 10s)."""
        return time.time() - self.timestamp < 10

    def __repr__(self):
        return (f"OracleLeadSignal({self.coin} {self.direction} "
                f"{self.magnitude:.2f}% conf={self.confidence:.0%})")


class BinanceOracleWS:
    """
    Real-time 1-second kline WebSocket for oracle lead detection.

    Architecture:
    - Connects to Binance combined stream for all active coins
    - Buffers last 60 seconds of 1s candles per coin
    - On each candle: checks for impulse moves
    - Exports signals for the oracle_arb strategy to consume
    """

    def __init__(self):
        self._buffers: Dict[str, deque] = {}  # coin -> deque of 1s candles
        self._latest_signals: Dict[str, OracleLeadSignal] = {}
        self._callbacks: List[Callable] = []
        self._running = False
        self._ws = None

        # Impulse detection params
        self.impulse_threshold_pct = 0.05  # 0.05% move in 1s = significant
        self.strong_impulse_pct = 0.15  # 0.15% in 1-3s = very strong
        self.buffer_seconds = 60  # keep 60s of history

        # Initialize buffers
        for coin in COIN_STREAMS:
            self._buffers[coin] = deque(maxlen=self.buffer_seconds)

    def on_signal(self, callback: Callable):
        """Register a callback for new signals."""
        self._callbacks.append(callback)

    @property
    def latest_signals(self) -> Dict[str, OracleLeadSignal]:
        """Get latest signal per coin (may be stale — check is_actionable)."""
        return self._latest_signals

    def get_signal(self, coin: str) -> Optional[OracleLeadSignal]:
        """Get latest actionable signal for a coin."""
        sig = self._latest_signals.get(coin)
        if sig and sig.is_actionable:
            return sig
        return None

    async def start(self):
        """Start the WebSocket connection."""
        try:
            import websockets
        except ImportError:
            print("⚠️ websockets not installed — oracle WS disabled")
            return

        streams = "/".join(COIN_STREAMS.values())
        url = f"{BINANCE_WS_BASE}/{streams}"

        self._running = True
        print(f"🔌 Connecting Binance 1s WS: {len(COIN_STREAMS)} coins")

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._ws = ws
                    print("✅ Binance 1s WS connected")
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            self._process_message(msg)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                if self._running:
                    print(f"⚠️ Binance WS error: {e}, reconnecting in 3s...")
                    await asyncio.sleep(3)

    def stop(self):
        """Stop the WebSocket."""
        self._running = False

    def _process_message(self, msg: Dict):
        """Process a raw WebSocket kline message."""
        data = msg.get("data") or msg
        kline = data.get("k")
        if not kline:
            return

        symbol = kline.get("s", "").upper()
        coin = None
        for c, stream in COIN_STREAMS.items():
            if symbol.startswith(c) or stream.split("@")[0].upper() == symbol:
                coin = c
                break
        if not coin:
            return

        candle = {
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
            "ts": time.time(),
            "is_closed": kline.get("x", False),
        }

        self._buffers[coin].append(candle)
        self._check_impulse(coin, candle)

    def _check_impulse(self, coin: str, candle: Dict):
        """Detect impulse moves from 1s candle data."""
        buf = self._buffers[coin]
        if len(buf) < 3:
            return

        close = candle["close"]

        # ── Check 1: Single-candle impulse ──
        single_move = (candle["close"] - candle["open"]) / candle["open"] * 100
        if abs(single_move) >= self.impulse_threshold_pct:
            direction = "UP" if single_move > 0 else "DOWN"
            confidence = min(0.8, abs(single_move) / self.strong_impulse_pct)

            self._emit_signal(OracleLeadSignal(
                coin=coin,
                direction=direction,
                magnitude=abs(single_move),
                confidence=confidence,
                seconds_since_move=0,
                binance_price=close,
                reason=f"1s impulse: {single_move:+.3f}%",
            ))
            return

        # ── Check 2: 3-second rolling impulse ──
        if len(buf) >= 3:
            price_3s_ago = list(buf)[-3]["open"]
            move_3s = (close - price_3s_ago) / price_3s_ago * 100

            if abs(move_3s) >= self.strong_impulse_pct:
                direction = "UP" if move_3s > 0 else "DOWN"
                confidence = min(0.9, abs(move_3s) / (self.strong_impulse_pct * 2))

                self._emit_signal(OracleLeadSignal(
                    coin=coin,
                    direction=direction,
                    magnitude=abs(move_3s),
                    confidence=confidence,
                    seconds_since_move=3,
                    binance_price=close,
                    reason=f"3s rolling impulse: {move_3s:+.3f}%",
                ))
                return

        # ── Check 3: Acceleration detection (5s window) ──
        if len(buf) >= 5:
            prices = [c["close"] for c in list(buf)[-5:]]
            first_vel = (prices[2] - prices[0]) / prices[0] * 100
            second_vel = (prices[4] - prices[2]) / prices[2] * 100

            accel = second_vel - first_vel
            if abs(accel) > self.impulse_threshold_pct:
                direction = "UP" if accel > 0 else "DOWN"
                confidence = min(0.7, abs(accel) / 0.3)

                self._emit_signal(OracleLeadSignal(
                    coin=coin,
                    direction=direction,
                    magnitude=abs(accel),
                    confidence=confidence,
                    seconds_since_move=2,
                    binance_price=close,
                    reason=f"5s acceleration: {accel:+.3f}%/s²",
                ))

    def _emit_signal(self, signal: OracleLeadSignal):
        """Store and broadcast a new signal."""
        self._latest_signals[signal.coin] = signal
        for cb in self._callbacks:
            try:
                cb(signal)
            except Exception:
                pass

    def get_lead_analysis(self, coin: str) -> Dict:
        """
        Full oracle lead analysis for a coin.

        Returns dict compatible with the existing oracle_arb strategy.
        Designed to augment get_full_signal_analysis() from binance_signals.py.
        """
        buf = self._buffers.get(coin, deque())
        if len(buf) < 5:
            return {"has_lead": False, "direction": "NEUTRAL", "strength": 0}

        prices = [c["close"] for c in buf]
        volumes = [c["volume"] for c in buf]

        # Recent move
        recent_move = (prices[-1] - prices[-5]) / prices[-5] * 100

        # Volume trend
        vol_recent = sum(volumes[-3:])
        vol_avg = sum(volumes) / len(volumes) * 3 if volumes else 0
        vol_ratio = vol_recent / max(vol_avg, 0.01)

        # Direction and strength
        direction = "UP" if recent_move > 0.02 else ("DOWN" if recent_move < -0.02 else "NEUTRAL")
        strength = min(1.0, abs(recent_move) / 0.20 * vol_ratio)

        # Latest signal
        sig = self.get_signal(coin)

        return {
            "has_lead": direction != "NEUTRAL" and strength > 0.3,
            "direction": direction,
            "strength": strength,
            "move_pct": recent_move,
            "vol_ratio": vol_ratio,
            "current_price": prices[-1],
            "active_signal": sig is not None,
            "signal": sig,
        }
