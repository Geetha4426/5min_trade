"""
WebSocket Feeds — Real-time Price Streams

Dual-feed architecture:
1. Polymarket WebSocket: real-time orderbook updates
2. Binance Feed: real-time prices (WS with REST API fallback for blocked regions)
"""

import json
import asyncio
import time
from typing import Callable, Dict, Optional, List
from collections import deque

import websockets
import requests

from config import Config


class PriceSnapshot:
    """Point-in-time price data."""
    __slots__ = ['token_id', 'price', 'best_bid', 'best_ask', 'timestamp']

    def __init__(self, token_id: str, price: float, best_bid: float = 0, best_ask: float = 0):
        self.token_id = token_id
        self.price = price
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.timestamp = time.time()


class PolymarketFeed:
    """Real-time Polymarket orderbook feed via WebSocket."""

    def __init__(self):
        self.ws_url = Config.POLYMARKET_WS_URL
        self._ws = None
        self._running = False
        self._subscribed_tokens: List[str] = []

        # Price history per token (last 60 snapshots)
        self.price_history: Dict[str, deque] = {}
        self.latest_prices: Dict[str, PriceSnapshot] = {}

        # Callbacks
        self._on_price_update: Optional[Callable] = None
        self._on_flash_crash: Optional[Callable] = None

    def on_price_update(self, callback: Callable):
        """Register callback for price updates."""
        self._on_price_update = callback

    def on_flash_crash(self, callback: Callable):
        """Register callback for flash crash detection."""
        self._on_flash_crash = callback

    async def subscribe(self, token_ids: List[str]):
        """Subscribe to token price updates."""
        self._subscribed_tokens = token_ids
        for tid in token_ids:
            if tid not in self.price_history:
                self.price_history[tid] = deque(maxlen=120)

    async def run(self):
        """Connect and stream prices."""
        self._running = True
        _logged_first_msg = False

        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=60,
                    close_timeout=10
                ) as ws:
                    self._ws = ws
                    print(f"🔌 Polymarket WS connected", flush=True)

                    # Subscribe — single message with ALL token IDs
                    if self._subscribed_tokens:
                        sub_msg = json.dumps({
                            "assets_ids": self._subscribed_tokens,
                            "type": "market",
                        })
                        await ws.send(sub_msg)
                        print(f"   Subscribed to {len(self._subscribed_tokens)} tokens", flush=True)

                    # Listen for updates
                    async for message in ws:
                        if not self._running:
                            break
                        # Log first raw message for debugging
                        if not _logged_first_msg:
                            _logged_first_msg = True
                            preview = message[:200] if len(message) > 200 else message
                            print(f"📨 First WS msg: {preview}", flush=True)
                        await self._handle_message(message)

            except websockets.ConnectionClosed:
                print("⚠️ Polymarket WS disconnected, reconnecting in 3s...", flush=True)
                await asyncio.sleep(3)
            except Exception as e:
                print(f"❌ Polymarket WS error: {e}", flush=True)
                await asyncio.sleep(5)

    async def _handle_message(self, raw: str):
        """Parse WebSocket message and update prices.
        
        Polymarket CLOB WS sends:
        1. Initial snapshot: list of orderbook objects with asset_id + asks
        2. price_change events: {"event_type": "price_change", "price_changes": [...]}
        3. Individual events with asset_id, price, best_bid, best_ask
        """
        try:
            data = json.loads(raw)

            # Format 1: Initial snapshot — list of orderbook entries
            if isinstance(data, list):
                for entry in data:
                    asset_id = entry.get('asset_id', '')
                    if not asset_id:
                        continue
                    asks = entry.get('asks', [])
                    if asks:
                        best_ask = min(float(a.get('price', a.get('p', 1.0))) for a in asks)
                        self._apply_price(asset_id, best_ask)
                return

            # Format 2: price_change event
            event_type = data.get('event_type', '')
            if event_type == 'price_change':
                for ch in data.get('price_changes', []):
                    asset_id = ch.get('asset_id', '')
                    best_ask = ch.get('best_ask')
                    if asset_id and best_ask:
                        self._apply_price(asset_id, float(best_ask))
                return

            # Format 3: Individual book/trade updates
            msg_type = data.get('type', '')
            if msg_type in ('book', 'price_change', 'last_trade_price'):
                token_id = data.get('asset_id', '')
                if not token_id:
                    return

                price = float(data.get('price', data.get('last_trade_price', 0)))
                best_bid = float(data.get('best_bid', 0))
                best_ask = float(data.get('best_ask', 0))

                if price <= 0 and best_ask > 0:
                    price = best_ask
                elif price <= 0 and best_bid > 0 and best_ask > 0:
                    price = (best_bid + best_ask) / 2

                if price > 0:
                    self._apply_price(token_id, price, best_bid, best_ask)

        except Exception as e:
            pass  # Silently ignore malformed messages

    def _apply_price(self, token_id: str, price: float,
                     best_bid: float = 0, best_ask: float = 0):
        """Store a price update and trigger callbacks."""
        if best_ask <= 0:
            best_ask = price
        snap = PriceSnapshot(token_id, price, best_bid, best_ask)

        self.latest_prices[token_id] = snap
        if token_id in self.price_history:
            self.price_history[token_id].append(snap)

        # Callback
        if self._on_price_update:
            asyncio.create_task(self._on_price_update(snap))

        # Flash crash detection
        self._detect_flash_crash(token_id, snap)

    def _detect_flash_crash(self, token_id: str, current: PriceSnapshot):
        """Detect if there was a sudden price drop."""
        history = self.price_history.get(token_id)
        if not history or len(history) < 3:
            return

        lookback = Config.FLASH_LOOKBACK_SECONDS
        threshold = Config.FLASH_DROP_THRESHOLD

        # Find price from lookback seconds ago
        cutoff = time.time() - lookback
        old_price = None
        for snap in history:
            if snap.timestamp >= cutoff:
                old_price = snap.price
                break

        if old_price is None or old_price <= 0:
            return

        drop = old_price - current.price
        if drop >= threshold and self._on_flash_crash:
            asyncio.create_task(self._on_flash_crash({
                'token_id': token_id,
                'old_price': old_price,
                'new_price': current.price,
                'drop': drop,
                'timestamp': current.timestamp,
            }))

    def get_latest(self, token_id: str) -> Optional[PriceSnapshot]:
        """Get latest price for a token."""
        return self.latest_prices.get(token_id)

    async def stop(self):
        """Stop the feed."""
        self._running = False
        if self._ws:
            await self._ws.close()


class BinancePriceSnapshot:
    """Price snapshot with attribute access (compatible with strategy expectations)."""
    __slots__ = ['price', 'timestamp']

    def __init__(self, price: float, timestamp: float = None):
        self.price = price
        self.timestamp = timestamp or time.time()


class BinanceFeed:
    """Binance price feed with WebSocket + REST API fallback.

    Railway (and other cloud providers) often block Binance WS
    from US/EU regions (HTTP 451). This class tries WS first,
    then falls back to polling the REST API every 3 seconds.
    """

    # REST API endpoints to try (in order)
    REST_ENDPOINTS = [
        'https://api.binance.com/api/v3/ticker/price',
        'https://api.binance.us/api/v3/ticker/price',
        'https://api1.binance.com/api/v3/ticker/price',
    ]

    def __init__(self):
        self._running = False
        self.latest_prices: Dict[str, float] = {}
        self._on_price: Optional[Callable] = None
        self._price_history: Dict[str, deque] = {}
        self._rest_endpoint: Optional[str] = None
        self._ws_failed = False
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': '5min-trade-bot/1.0',
        })

    @property
    def price_history(self) -> Dict[str, deque]:
        """Expose price history — strategies access this directly."""
        return self._price_history

    def on_price(self, callback: Callable):
        self._on_price = callback

    async def run(self, coins: List[str] = None):
        """Stream prices — tries WS first, falls back to REST polling."""
        coins = coins or Config.ENABLED_COINS
        self._running = True

        # Initialize price history
        for coin in coins:
            if coin not in self._price_history:
                self._price_history[coin] = deque(maxlen=120)

        # Try WebSocket first
        if not self._ws_failed:
            try:
                await asyncio.wait_for(self._run_ws(coins), timeout=10)
            except (asyncio.TimeoutError, Exception) as e:
                print(f"⚠️ Binance WS unavailable ({e}), switching to REST API", flush=True)
                self._ws_failed = True

        # Fall back to REST API polling
        if self._ws_failed and self._running:
            await self._run_rest(coins)

    async def _run_ws(self, coins: List[str]):
        """Try WebSocket connection."""
        symbols = [Config.BINANCE_SYMBOLS.get(c, f'{c.lower()}usdt') for c in coins]
        streams = '/'.join(f"{s}@trade" for s in symbols)
        url = f"{Config.BINANCE_WS_URL}/{streams}"

        async with websockets.connect(url) as ws:
            print(f"🔌 Binance WS connected ({', '.join(coins)})", flush=True)

            async for message in ws:
                if not self._running:
                    break

                data = json.loads(message)
                symbol = data.get('s', '').upper()
                price = float(data.get('p', 0))

                if price > 0:
                    for coin, sym in Config.BINANCE_SYMBOLS.items():
                        if sym.upper() == symbol:
                            self._update_price(coin, price)
                            break

    async def _run_rest(self, coins: List[str]):
        """Poll REST API for prices (fallback)."""
        # Find working endpoint
        endpoint = await self._find_rest_endpoint()
        if not endpoint:
            print("❌ All Binance REST endpoints failed. Trying CoinGecko...", flush=True)
            await self._run_coingecko(coins)
            return

        symbols_map = {Config.BINANCE_SYMBOLS.get(c, f'{c.lower()}usdt').upper(): c for c in coins}
        print(f"🔌 Binance REST API connected ({', '.join(coins)}) — polling every 3s", flush=True)

        while self._running:
            try:
                for symbol, coin in symbols_map.items():
                    resp = self._session.get(
                        f"{endpoint}?symbol={symbol}",
                        timeout=5
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        price = float(data.get('price', 0))
                        if price > 0:
                            self._update_price(coin, price)

                await asyncio.sleep(3)  # Poll every 3 seconds

            except Exception as e:
                print(f"⚠️ Binance REST error: {e}", flush=True)
                await asyncio.sleep(5)

    async def _run_coingecko(self, coins: List[str]):
        """Ultimate fallback: CoinGecko free API."""
        coin_ids = {
            'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 'XRP': 'ripple',
        }
        ids_str = ','.join(coin_ids.get(c, c.lower()) for c in coins)
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd"

        print(f"🔌 CoinGecko fallback connected ({', '.join(coins)}) — polling every 10s", flush=True)

        while self._running:
            try:
                resp = self._session.get(url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for coin in coins:
                        cg_id = coin_ids.get(coin, coin.lower())
                        if cg_id in data and 'usd' in data[cg_id]:
                            price = float(data[cg_id]['usd'])
                            self._update_price(coin, price)

                await asyncio.sleep(10)  # CoinGecko has rate limits

            except Exception as e:
                print(f"⚠️ CoinGecko error: {e}", flush=True)
                await asyncio.sleep(15)

    async def _find_rest_endpoint(self) -> Optional[str]:
        """Find a working Binance REST endpoint."""
        for endpoint in self.REST_ENDPOINTS:
            try:
                resp = self._session.get(
                    f"{endpoint}?symbol=BTCUSDT",
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if 'price' in data:
                        print(f"✅ Using Binance REST: {endpoint}", flush=True)
                        self._rest_endpoint = endpoint
                        return endpoint
            except Exception:
                continue
        return None

    def _update_price(self, coin: str, price: float):
        """Update price and trigger callback. Stores BinancePriceSnapshot objects."""
        self.latest_prices[coin] = price
        snap = BinancePriceSnapshot(price)
        if coin in self._price_history:
            self._price_history[coin].append(snap)
        if self._on_price:
            asyncio.create_task(self._on_price(coin, price))

    def get_price(self, coin: str) -> Optional[float]:
        """Get latest price for a coin."""
        return self.latest_prices.get(coin.upper())

    def get_price_history(self, coin: str) -> List:
        """Get price history for a coin (list of BinancePriceSnapshot)."""
        history = self._price_history.get(coin.upper())
        if history:
            return list(history)
        return []

    async def stop(self):
        self._running = False
