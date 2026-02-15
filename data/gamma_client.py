"""
Gamma API Client — Market Discovery

Discovers active 5m/15m/30m crypto Up/Down markets on Polymarket.

Key insight: These markets are EVENTS (not individual markets).
- Event slug pattern: {coin}-updown-{tf}m-{epoch_timestamp}
- Example: btc-updown-5m-1771173900
- Each event contains 1 market with "Up" and "Down" outcomes.
- Use the /events endpoint, NOT /markets.
"""

import re
import time
import requests
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

from config import Config


class GammaClient:
    """Discovers and tracks active crypto minute-markets on Polymarket."""

    # Slug pattern: btc-updown-5m-1771173900
    SLUG_PATTERN = re.compile(
        r'(?P<coin>btc|eth|sol|xrp)-updown-(?P<tf>\d+)m-(?P<epoch>\d+)'
    )

    # Market question patterns (fallback for non-slug matching)
    QUESTION_PATTERNS = [
        r'(?P<coin>BTC|ETH|SOL|XRP)\s+[Uu]p\s+or\s+[Dd]own\??\s*\((?P<tf>\d+)\s*min',
        r'[Ww]ill\s+(?P<coin>BTC|ETH|SOL|XRP)\s+go\s+up.*?(?P<tf>\d+)\s*min',
        r'(?P<coin>Bitcoin|Ethereum|Solana)\s+(?P<tf>\d+)[-\s]*[Mm]inute\s+[Uu]p',
        r'(?P<coin>BTC|ETH|SOL|XRP)\s+(?P<tf>\d+)\s*min\s+[Uu]p',
        r'(?P<coin>BTC|ETH|SOL|XRP).*?[Uu]p.*?[Dd]own.*?(?P<tf>\d+)\s*min',
    ]

    COIN_ALIASES = {
        'bitcoin': 'BTC', 'ethereum': 'ETH', 'solana': 'SOL',
        'btc': 'BTC', 'eth': 'ETH', 'sol': 'SOL', 'xrp': 'XRP',
        'Bitcoin': 'BTC', 'Ethereum': 'ETH', 'Solana': 'SOL',
        'BTC': 'BTC', 'ETH': 'ETH', 'SOL': 'SOL', 'XRP': 'XRP',
    }

    def __init__(self):
        self.base_url = Config.GAMMA_API_URL
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': '5min-trade-bot/1.0',
            'Accept': 'application/json',
        })
        # Cache: list of parsed markets
        self._cache: List[Dict] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 15  # seconds — 5min markets rotate fast

    def discover_markets(self, coins: List[str] = None, timeframes: List[int] = None) -> List[Dict]:
        """
        Find all active crypto Up/Down markets by searching EVENTS.

        Returns list of parsed market dicts.
        """
        coins = coins or Config.ENABLED_COINS
        timeframes = timeframes or Config.ENABLED_TIMEFRAMES

        # Check cache
        if time.time() - self._cache_ts < self._cache_ttl and self._cache:
            return self._filter(self._cache, coins, timeframes)

        all_parsed = []

        # ── Strategy 1: Fetch events and filter by slug pattern ──
        events = self._fetch_events()
        for event in events:
            slug = event.get('slug', '')
            title = event.get('title', '')

            # Check slug against pattern
            slug_match = self.SLUG_PATTERN.match(slug)
            if slug_match:
                coin = slug_match.group('coin').upper()
                tf = int(slug_match.group('tf'))
                markets_in_event = event.get('markets', [])

                for market in markets_in_event:
                    parsed = self._parse_event_market(market, coin, tf, title, slug)
                    if parsed:
                        all_parsed.append(parsed)
                continue

            # Fallback: check title/question against patterns
            for pattern in self.QUESTION_PATTERNS:
                match = re.search(pattern, title)
                if match:
                    coin_raw = match.group('coin')
                    coin = self.COIN_ALIASES.get(coin_raw, coin_raw.upper())
                    tf = int(match.group('tf'))
                    markets_in_event = event.get('markets', [])

                    for market in markets_in_event:
                        parsed = self._parse_event_market(market, coin, tf, title, slug)
                        if parsed:
                            all_parsed.append(parsed)
                    break

        # ── Strategy 2: Also try /markets endpoint for any that slip through ──
        if not all_parsed:
            print("🔍 No events matched, trying /markets endpoint...", flush=True)
            raw_markets = self._fetch_markets_direct()
            for market in raw_markets:
                parsed = self._parse_standalone_market(market)
                if parsed:
                    all_parsed.append(parsed)

        # Deduplicate by market_id
        seen = set()
        unique = []
        for m in all_parsed:
            if m['market_id'] not in seen:
                seen.add(m['market_id'])
                unique.append(m)
        all_parsed = unique

        # Log what we found
        if all_parsed:
            coins_found = set(m['coin'] for m in all_parsed)
            tfs_found = set(m['timeframe'] for m in all_parsed)
            print(f"📡 Found {len(all_parsed)} crypto markets: {coins_found} × {tfs_found}min", flush=True)
        else:
            print("⚠️ No crypto Up/Down markets found on Polymarket right now", flush=True)

        self._cache = all_parsed
        self._cache_ts = time.time()

        return self._filter(all_parsed, coins, timeframes)

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

    # ═══════════════════════════════════════════════════════════════════
    # FETCH METHODS
    # ═══════════════════════════════════════════════════════════════════

    def _fetch_events(self) -> List[Dict]:
        """
        Fetch active events from the /events endpoint.
        This is where 5m/15m/30m crypto Up/Down markets live.
        """
        all_events = []

        # Fetch multiple pages of active events (newest first)
        for offset in range(0, 600, 100):
            try:
                url = (
                    f"{self.base_url}/events"
                    f"?active=true&closed=false"
                    f"&limit=100&offset={offset}"
                    f"&order=startDate&ascending=false"
                )
                resp = self.session.get(url, timeout=20)
                if resp.status_code != 200:
                    print(f"⚠️ Events API returned {resp.status_code}", flush=True)
                    break
                data = resp.json()
                if not data:
                    break
                all_events.extend(data)

                # Count how many have 'updown' in slug — stop early if we have enough
                updown_count = sum(1 for e in all_events if 'updown' in e.get('slug', ''))
                if updown_count >= 20:  # We have plenty
                    break

                if len(data) < 100:
                    break

            except Exception as e:
                print(f"❌ Error fetching events (offset={offset}): {e}", flush=True)
                break

        # Log discovery info
        updown_events = [e for e in all_events if 'updown' in e.get('slug', '')]
        print(
            f"📊 Fetched {len(all_events)} events, "
            f"{len(updown_events)} are crypto Up/Down",
            flush=True
        )
        for e in updown_events[:5]:
            print(f"   → {e.get('slug', '?')} | {e.get('title', '?')[:60]}", flush=True)

        return all_events

    def _fetch_markets_direct(self) -> List[Dict]:
        """Fallback: fetch from /markets endpoint directly."""
        try:
            url = f"{self.base_url}/markets?active=true&closed=false&limit=200"
            resp = self.session.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"❌ Error fetching markets: {e}", flush=True)
        return []

    # ═══════════════════════════════════════════════════════════════════
    # PARSE METHODS
    # ═══════════════════════════════════════════════════════════════════

    def _parse_event_market(self, market: Dict, coin: str, timeframe: int,
                             event_title: str, event_slug: str) -> Optional[Dict]:
        """Parse a market that's nested inside an event."""
        tokens = market.get('tokens', [])
        clob_ids_raw = market.get('clobTokenIds', '')

        up_token = None
        down_token = None
        up_price = 0.5
        down_price = 0.5

        # Parse tokens (outcomes: Up/Down or Yes/No)
        if tokens and len(tokens) >= 2:
            for token in tokens:
                outcome = token.get('outcome', '').lower()
                if 'up' in outcome or 'yes' in outcome:
                    up_token = token.get('token_id', '')
                    up_price = float(token.get('price', 0.5) or 0.5)
                elif 'down' in outcome or 'no' in outcome:
                    down_token = token.get('token_id', '')
                    down_price = float(token.get('price', 0.5) or 0.5)

        # Fallback: clobTokenIds
        if not up_token and clob_ids_raw:
            try:
                if isinstance(clob_ids_raw, str):
                    ids = clob_ids_raw.strip('[]"').split('","')
                else:
                    ids = list(clob_ids_raw)
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
                        prices = list(prices_raw)
                    if len(prices) >= 2:
                        up_price = float(prices[0])
                        down_price = float(prices[1])
                except Exception:
                    pass

        return {
            'coin': coin,
            'timeframe': timeframe,
            'question': market.get('question', event_title),
            'condition_id': market.get('conditionId', ''),
            'market_id': market.get('id', ''),
            'market_slug': market.get('market_slug', market.get('slug', event_slug)),
            'event_slug': event_slug,
            'up_token_id': up_token or '',
            'down_token_id': down_token or '',
            'up_price': up_price,
            'down_price': down_price,
            'end_date': market.get('endDate', market.get('end_date_iso', '')),
            'volume': float(market.get('volume', 0) or 0),
            'liquidity': float(market.get('liquidity', 0) or 0),
        }

    def _parse_standalone_market(self, market: Dict) -> Optional[Dict]:
        """Parse a standalone market from /markets endpoint (fallback)."""
        question = market.get('question', '')

        for pattern in self.QUESTION_PATTERNS:
            match = re.search(pattern, question)
            if match:
                coin_raw = match.group('coin')
                coin = self.COIN_ALIASES.get(coin_raw, coin_raw.upper())
                timeframe = int(match.group('tf'))
                return self._parse_event_market(market, coin, timeframe, question, '')

        return None

    # ═══════════════════════════════════════════════════════════════════
    # FILTER & UTILITY
    # ═══════════════════════════════════════════════════════════════════

    def _filter(self, markets: List[Dict], coins: List[str], timeframes: List[int]) -> List[Dict]:
        """Filter markets by coins and timeframes."""
        return [
            m for m in markets
            if m['coin'] in coins and m['timeframe'] in timeframes
        ]

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
