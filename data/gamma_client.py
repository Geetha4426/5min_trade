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
        _logged_sample = False

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

                # Log first event's market structure for debugging
                if not _logged_sample and markets_in_event:
                    m0 = markets_in_event[0]
                    print(f"🔬 Sample market fields: {list(m0.keys())[:15]}", flush=True)
                    print(f"   tokens: {len(m0.get('tokens', []))} | "
                          f"clobTokenIds: {bool(m0.get('clobTokenIds'))} | "
                          f"outcomePrices: {bool(m0.get('outcomePrices'))}", flush=True)
                    _logged_sample = True

                for market in markets_in_event:
                    parsed = self._parse_event_market(market, coin, tf, title, slug)
                    if parsed:
                        # If no token IDs, fetch full market data
                        if not parsed['up_token_id'] or not parsed['down_token_id']:
                            market_id = parsed['market_id']
                            if market_id:
                                full = self._fetch_single_market(market_id)
                                if full:
                                    enriched = self._parse_event_market(
                                        full, coin, tf, title, slug
                                    )
                                    if enriched and enriched['up_token_id']:
                                        parsed = enriched
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
            with_tokens = sum(1 for m in all_parsed if m.get('up_token_id'))
            print(f"📡 Found {len(all_parsed)} crypto markets ({with_tokens} with token IDs): "
                  f"{coins_found} × {tfs_found}min", flush=True)
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
        return self._fetch_single_market(market_id)

    def _fetch_single_market(self, market_id: str) -> Optional[Dict]:
        """Fetch full market data for a single market by condition_id or id."""
        try:
            # Try by condition_id first, then by id
            for param in ['condition_id', 'id']:
                url = f"{self.base_url}/markets?{param}={market_id}&closed=false"
                resp = self.session.get(url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    elif isinstance(data, dict) and data:
                        return data

            # Direct fetch
            url = f"{self.base_url}/markets/{market_id}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            pass  # Silent — called per market, would spam
        return None

    # ═══════════════════════════════════════════════════════════════════
    # FETCH METHODS
    # ═══════════════════════════════════════════════════════════════════

    def _fetch_events(self) -> List[Dict]:
        """
        Fetch active crypto Up/Down events using fast discovery methods.
        
        Strategy 1 (fastest): Compute exact slug from current timestamp
        Strategy 2 (fallback): Use series_slug for broader discovery
        Strategy 3 (last resort): Page through all events
        """
        all_events = []
        now_ts = int(time.time())

        # ═══ Strategy 1: Direct slug lookup (st1ne method) ═══
        # Compute the exact slug for each coin/timeframe from UTC timestamp
        for coin in Config.ENABLED_COINS:
            coin_slug = Config.COIN_PM.get(coin, coin.lower())
            for tf in Config.ENABLED_TIMEFRAMES:
                # Compute current market epoch: round down to nearest {tf}min boundary
                interval_secs = tf * 60
                epoch = (now_ts // interval_secs) * interval_secs
                # Try current and previous window (in case we're between markets)
                for ts in [epoch, epoch - interval_secs]:
                    slug = f"{coin_slug}-updown-{tf}m-{ts}"
                    try:
                        url = (
                            f"{self.base_url}/events"
                            f"?slug={slug}&limit=1"
                        )
                        resp = self.session.get(url, timeout=8)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data and isinstance(data, list):
                                all_events.extend(data)
                    except Exception:
                        pass

        if all_events:
            unique_slugs = set(e.get('slug', '') for e in all_events)
            print(f"🎯 Direct slug lookup: found {len(all_events)} events ({', '.join(list(unique_slugs)[:3])})", flush=True)
            return all_events

        # ═══ Strategy 2: Series slug lookup (FrondEnt method) ═══
        for coin in Config.ENABLED_COINS:
            coin_slug = Config.COIN_PM.get(coin, coin.lower())
            for tf in Config.ENABLED_TIMEFRAMES:
                series_slug_template = Config.SERIES_SLUGS.get(tf)
                if not series_slug_template:
                    continue
                series_slug = series_slug_template.replace('{coin}', coin_slug)
                try:
                    url = (
                        f"{self.base_url}/events"
                        f"?series_slug={series_slug}"
                        f"&active=true&closed=false&limit=10"
                    )
                    resp = self.session.get(url, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and isinstance(data, list):
                            all_events.extend(data)
                except Exception:
                    pass

        if all_events:
            print(f"📡 Series slug lookup: found {len(all_events)} events", flush=True)
            return all_events

        # ═══ Strategy 3: Broad search (much slower, last resort) ═══
        print("🔍 Slug/series lookup failed, falling back to broad search...", flush=True)
        for offset in range(0, 300, 100):
            try:
                url = (
                    f"{self.base_url}/events"
                    f"?active=true&closed=false"
                    f"&limit=100&offset={offset}"
                    f"&order=startDate&ascending=false"
                )
                resp = self.session.get(url, timeout=20)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if not data:
                    break
                all_events.extend(data)

                # Count updown events — stop early if we have enough
                updown_count = sum(1 for e in all_events if 'updown' in e.get('slug', ''))
                if updown_count >= 10:
                    break
                if len(data) < 100:
                    break
            except Exception as e:
                print(f"❌ Error fetching events (offset={offset}): {e}", flush=True)
                break

        updown_events = [e for e in all_events if 'updown' in e.get('slug', '')]
        print(f"📊 Broad search: {len(all_events)} events, {len(updown_events)} crypto Up/Down", flush=True)
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

    _first_parse_logged = False

    def _parse_event_market(self, market: Dict, coin: str, timeframe: int,
                             event_title: str, event_slug: str) -> Optional[Dict]:
        """Parse a market that's nested inside an event."""
        import json as _json

        tokens = market.get('tokens', [])
        clob_ids_raw = market.get('clobTokenIds', '')
        outcomes_raw = market.get('outcomes', '')
        prices_raw = market.get('outcomePrices', '')

        # Debug: log first market's raw values
        if not GammaClient._first_parse_logged:
            GammaClient._first_parse_logged = True
            print(f"🔬 RAW PARSE DEBUG:", flush=True)
            print(f"   all keys: {list(market.keys())}", flush=True)
            print(f"   clobTokenIds type={type(clob_ids_raw).__name__} val={str(clob_ids_raw)[:120]}", flush=True)
            print(f"   outcomes type={type(outcomes_raw).__name__} val={str(outcomes_raw)[:80]}", flush=True)
            print(f"   outcomePrices type={type(prices_raw).__name__} val={str(prices_raw)[:80]}", flush=True)
            print(f"   tokens count={len(tokens)}", flush=True)

        up_token = None
        down_token = None
        up_price = 0.5
        down_price = 0.5

        # ── Parse clobTokenIds (the main source for token IDs) ──
        clob_ids = []
        if clob_ids_raw:
            try:
                if isinstance(clob_ids_raw, str):
                    clob_ids = _json.loads(clob_ids_raw)
                elif isinstance(clob_ids_raw, list):
                    clob_ids = clob_ids_raw
            except (ValueError, TypeError):
                # Try manual parsing as last resort
                try:
                    cleaned = clob_ids_raw.strip()
                    if cleaned.startswith('['):
                        cleaned = cleaned[1:-1]
                    clob_ids = [s.strip().strip('"').strip("'") for s in cleaned.split(',') if s.strip()]
                except Exception:
                    pass

        # ── Parse outcomes (e.g. ["Up", "Down"]) ──
        outcomes = []
        if outcomes_raw:
            try:
                if isinstance(outcomes_raw, str):
                    outcomes = _json.loads(outcomes_raw)
                elif isinstance(outcomes_raw, list):
                    outcomes = outcomes_raw
            except (ValueError, TypeError):
                pass

        # ── Parse outcomePrices ──
        prices = []
        if prices_raw:
            try:
                if isinstance(prices_raw, str):
                    prices = _json.loads(prices_raw)
                elif isinstance(prices_raw, list):
                    prices = prices_raw
                prices = [float(p) for p in prices]
            except (ValueError, TypeError):
                pass

        # ── Method 1: Use tokens array if available ──
        if tokens and len(tokens) >= 2:
            for token in tokens:
                outcome = token.get('outcome', '').lower()
                if 'up' in outcome or 'yes' in outcome:
                    up_token = token.get('token_id', '')
                    up_price = float(token.get('price', 0.5) or 0.5)
                elif 'down' in outcome or 'no' in outcome:
                    down_token = token.get('token_id', '')
                    down_price = float(token.get('price', 0.5) or 0.5)

        # ── Method 2: Map clobTokenIds + outcomes ──
        if not up_token and clob_ids and len(clob_ids) >= 2:
            if outcomes and len(outcomes) >= 2:
                # Map by outcome name
                for i, outcome in enumerate(outcomes):
                    outcome_lower = outcome.lower() if isinstance(outcome, str) else ''
                    if 'up' in outcome_lower or 'yes' in outcome_lower:
                        up_token = clob_ids[i]
                        if prices and len(prices) > i:
                            up_price = prices[i]
                    elif 'down' in outcome_lower or 'no' in outcome_lower:
                        down_token = clob_ids[i]
                        if prices and len(prices) > i:
                            down_price = prices[i]
            else:
                # No outcomes field — assume first=Up, second=Down
                up_token = clob_ids[0]
                down_token = clob_ids[1]

        # ── Parse prices if still default ──
        if up_price == 0.5 and prices and len(prices) >= 2:
            up_price = prices[0]
            down_price = prices[1]

        return {
            'coin': coin,
            'timeframe': timeframe,
            'question': market.get('question', event_title),
            'condition_id': market.get('conditionId', ''),
            'market_id': market.get('id', ''),
            'market_slug': market.get('market_slug', market.get('slug', event_slug)),
            'event_slug': event_slug,
            'event_start_time': market.get('eventStartTime', ''),
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
