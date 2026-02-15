"""
Gamma API Client — Market Discovery

Discovers active 5m/15m/30m crypto Up/Down markets on Polymarket.
Uses tag-based filtering + keyword search for efficient discovery.
"""

import re
import time
import requests
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

from config import Config


class GammaClient:
    """Discovers and tracks active crypto minute-markets on Polymarket."""

    # Market question patterns for crypto Up/Down
    MARKET_PATTERNS = [
        # "BTC Up or Down? (5 min)"
        r'(?P<coin>BTC|ETH|SOL|XRP)\s+[Uu]p\s+or\s+[Dd]own\??\s*\((?P<tf>\d+)\s*min',
        # "Will BTC go up in the next 5 minutes?"
        r'[Ww]ill\s+(?P<coin>BTC|ETH|SOL|XRP)\s+go\s+up.*?(?P<tf>\d+)\s*min',
        # "Bitcoin 5-Minute Up/Down"
        r'(?P<coin>Bitcoin|Ethereum|Solana)\s+(?P<tf>\d+)[-\s]*[Mm]inute\s+[Uu]p',
        # "BTC 5 min Up/Down"
        r'(?P<coin>BTC|ETH|SOL|XRP)\s+(?P<tf>\d+)\s*min\s+[Uu]p',
        # "BTC price up (5 min)" — broader
        r'(?P<coin>BTC|ETH|SOL|XRP)\s+price.*?(?P<tf>\d+)\s*min',
        # "Will Bitcoin go up" + timeframe embedded
        r'(?P<coin>Bitcoin|Ethereum|Solana).*?(?P<tf>\d+)\s*[-]?\s*min',
    ]

    COIN_ALIASES = {
        'Bitcoin': 'BTC', 'Ethereum': 'ETH', 'Solana': 'SOL',
        'BTC': 'BTC', 'ETH': 'ETH', 'SOL': 'SOL', 'XRP': 'XRP',
    }

    # Search keywords to find crypto minute-markets
    SEARCH_KEYWORDS = [
        'BTC up or down',
        'ETH up or down',
        'SOL up or down',
        'Bitcoin 5-minute',
        'Bitcoin 15-minute',
        'Bitcoin up',
        'crypto',
    ]

    def __init__(self):
        self.base_url = Config.GAMMA_API_URL
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': '5min-trade-bot/1.0',
            'Accept': 'application/json',
        })
        # Cache: key = (coin, timeframe) -> market data
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0
        self._cache_ttl: float = 20  # seconds

    def discover_markets(self, coins: List[str] = None, timeframes: List[int] = None) -> List[Dict]:
        """
        Find all active crypto Up/Down markets.

        Returns list of dicts with market info.
        """
        coins = coins or Config.ENABLED_COINS
        timeframes = timeframes or Config.ENABLED_TIMEFRAMES

        # Check cache
        if time.time() - self._cache_ts < self._cache_ttl and self._cache:
            return self._filter_cached(coins, timeframes)

        matched = []

        # Strategy 1: Search by tag_id (if we know it)
        tag_markets = self._fetch_by_tag()
        for m in tag_markets:
            parsed = self._parse_market(m)
            if parsed and parsed not in matched:
                matched.append(parsed)

        # Strategy 2: Keyword-based search
        for keyword in self.SEARCH_KEYWORDS:
            keyword_markets = self._search_markets(keyword)
            for m in keyword_markets:
                parsed = self._parse_market(m)
                if parsed:
                    key = f"{parsed['coin']}_{parsed['timeframe']}_{parsed['market_id']}"
                    if not any(f"{p['coin']}_{p['timeframe']}_{p['market_id']}" == key for p in matched):
                        matched.append(parsed)

        # Strategy 3: Broad fetch if nothing found yet
        if not matched:
            print("🔍 Tag/keyword search empty, trying broad fetch...", flush=True)
            raw_markets = self._fetch_all_active(limit=500)
            for m in raw_markets:
                parsed = self._parse_market(m)
                if parsed:
                    matched.append(parsed)

        # Log discovery results
        if matched:
            coins_found = set(m['coin'] for m in matched)
            tfs_found = set(m['timeframe'] for m in matched)
            print(f"📡 Found {len(matched)} crypto markets: {coins_found} × {tfs_found}min", flush=True)
        else:
            print("⚠️ No crypto Up/Down markets found on Polymarket", flush=True)

        self._cache = {f"{m['coin']}_{m['timeframe']}_{m['market_id']}": m for m in matched}
        self._cache_ts = time.time()

        return self._filter_cached(coins, timeframes)

    def get_market(self, coin: str, timeframe: int) -> Optional[Dict]:
        """Get a specific market by coin and timeframe."""
        markets = self.discover_markets()
        for m in markets:
            if m['coin'] == coin.upper() and m['timeframe'] == timeframe:
                return m
        return None

    def get_market_by_id(self, market_id: str) -> Optional[Dict]:
        """Fetch a specific market by its ID."""
        try:
            url = f"{self.base_url}/markets/{market_id}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"❌ Error fetching market {market_id}: {e}", flush=True)
        return None

    def _fetch_by_tag(self) -> List[Dict]:
        """Fetch crypto markets using tag-based filtering."""
        markets = []

        # Try several approaches to find crypto tag
        tag_urls = [
            f"{self.base_url}/markets?tag=crypto&closed=false&active=true&limit=100",
            f"{self.base_url}/markets?tag=Crypto&closed=false&active=true&limit=100",
            f"{self.base_url}/events?tag=crypto&closed=false&active=true&limit=50",
            f"{self.base_url}/events?tag=Crypto&closed=false&active=true&limit=50",
        ]

        for url in tag_urls:
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        # Events endpoint returns events with nested markets
                        for item in data:
                            if 'markets' in item:
                                markets.extend(item['markets'])
                            else:
                                markets.append(item)
                    if markets:
                        break
            except Exception as e:
                print(f"⚠️ Tag fetch error ({url[:60]}...): {e}", flush=True)

        return markets

    def _search_markets(self, keyword: str) -> List[Dict]:
        """Search for markets by keyword."""
        try:
            url = (
                f"{self.base_url}/markets"
                f"?closed=false&active=true&limit=50"
                f"&order=volume&ascending=false"
            )
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                # Filter client-side by keyword in question
                keyword_lower = keyword.lower()
                return [m for m in data if keyword_lower in m.get('question', '').lower()]
        except Exception as e:
            print(f"⚠️ Search error: {e}", flush=True)
        return []

    def _fetch_all_active(self, limit: int = 500) -> List[Dict]:
        """Fetch all active markets from Gamma API (fallback)."""
        markets = []
        offset = 0
        batch = 100

        while len(markets) < limit:
            try:
                url = f"{self.base_url}/markets?limit={batch}&offset={offset}&closed=false&active=true"
                resp = self.session.get(url, timeout=30)
                if resp.status_code != 200:
                    print(f"⚠️ Gamma API returned {resp.status_code}", flush=True)
                    break
                data = resp.json()
                if not data:
                    break
                markets.extend(data)
                if len(data) < batch:
                    break
                offset += batch
            except Exception as e:
                print(f"❌ Error fetching markets: {e}", flush=True)
                break

        # Log what we got for debugging
        crypto_related = [m for m in markets if any(
            kw in m.get('question', '').lower()
            for kw in ['btc', 'eth', 'sol', 'bitcoin', 'ethereum', 'crypto', 'up or down']
        )]
        print(f"📊 Fetched {len(markets)} total markets, {len(crypto_related)} crypto-related", flush=True)

        # Print sample titles of crypto markets for debugging
        for m in crypto_related[:5]:
            print(f"   → {m.get('question', '?')[:80]}", flush=True)

        return markets

    def _parse_market(self, market: Dict) -> Optional[Dict]:
        """Parse a raw market into our standard format if it's a crypto Up/Down market."""
        question = market.get('question', '')

        for pattern in self.MARKET_PATTERNS:
            match = re.search(pattern, question)
            if match:
                coin_raw = match.group('coin')
                coin = self.COIN_ALIASES.get(coin_raw, coin_raw.upper())
                timeframe = int(match.group('tf'))

                # Extract token IDs
                tokens = market.get('tokens', [])
                clob_ids_raw = market.get('clobTokenIds', '')

                up_token = None
                down_token = None
                up_price = 0.5
                down_price = 0.5

                # Parse tokens
                if tokens and len(tokens) >= 2:
                    for token in tokens:
                        outcome = token.get('outcome', '').lower()
                        if 'up' in outcome or 'yes' in outcome:
                            up_token = token.get('token_id', '')
                            up_price = float(token.get('price', 0.5))
                        elif 'down' in outcome or 'no' in outcome:
                            down_token = token.get('token_id', '')
                            down_price = float(token.get('price', 0.5))

                # Fallback: parse from clobTokenIds string
                if not up_token and clob_ids_raw:
                    try:
                        if isinstance(clob_ids_raw, str):
                            ids = clob_ids_raw.strip('[]"').split('","')
                        else:
                            ids = clob_ids_raw
                        if len(ids) >= 2:
                            up_token = ids[0]
                            down_token = ids[1]
                    except Exception:
                        pass

                # Parse prices from outcomePrices
                if up_price == 0.5:
                    prices_raw = market.get('outcomePrices', '')
                    if prices_raw:
                        try:
                            if isinstance(prices_raw, str):
                                prices = prices_raw.strip('[]"').split('","')
                            else:
                                prices = prices_raw
                            if len(prices) >= 2:
                                up_price = float(prices[0])
                                down_price = float(prices[1])
                        except Exception:
                            pass

                return {
                    'coin': coin,
                    'timeframe': timeframe,
                    'question': question,
                    'condition_id': market.get('conditionId', ''),
                    'market_id': market.get('id', ''),
                    'market_slug': market.get('market_slug', market.get('slug', '')),
                    'up_token_id': up_token or '',
                    'down_token_id': down_token or '',
                    'up_price': up_price,
                    'down_price': down_price,
                    'end_date': market.get('endDate', market.get('end_date_iso', '')),
                    'volume': float(market.get('volume', 0) or 0),
                    'liquidity': float(market.get('liquidity', 0) or 0),
                }

        return None

    def _filter_cached(self, coins: List[str], timeframes: List[int]) -> List[Dict]:
        """Filter cached markets by coins and timeframes."""
        results = []
        for key, market in self._cache.items():
            if market['coin'] in coins and market['timeframe'] in timeframes:
                results.append(market)
        return results

    def get_seconds_remaining(self, market: Dict) -> int:
        """Calculate seconds remaining until market settlement."""
        end_date = market.get('end_date', '')
        if not end_date:
            return 0
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            remaining = (end_dt - now).total_seconds()
            return max(0, int(remaining))
        except Exception:
            return 0
