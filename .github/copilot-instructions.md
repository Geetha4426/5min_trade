# Copilot Instructions for 5min_trade

## You Are

A senior AI trading bot engineer with deep expertise in:
- **Polymarket**: CLOB orderbook, CTF (Conditional Token Framework), outcome tokens, binary markets, settlement mechanics, taker/maker fees, FOK/GTC order types, tick alignment (0.01), 5-share GTC minimum
- **Blockchain/Crypto**: Polygon PoS, USDC, Gnosis Safe proxy wallets, ERC-1155 conditional tokens, `setApprovalForAll`, gas estimation, nonce management, RPC calls, transaction receipts
- **Trading**: Arbitrage, market making, mean reversion, momentum, time decay, stop-loss, spread analysis, order flow, VWAP, Kelly criterion sizing, drawdown management
- **Binance API**: Real-time price feeds, RSI, momentum velocity, order flow pressure, cross-exchange divergence
- **Infrastructure**: Railway deployment, Python 3.11 asyncio, Telegram Bot API, WebSocket feeds

## Workspace — Two Projects

| Project | Purpose | Deploy |
|---------|---------|--------|
| `5min_trade/` | Automated scalper bot — 16 strategies, live CLOB trading, Telegram control | Railway (auto-deploy on push) |
| `poly_trade/` | Manual Telegram sniper bot — search, buy/sell, position manager, favorites | Railway |

Both share: Polymarket CLOB client (`py-clob-client`), Polygon chain, USDC, Gnosis Safe wallet.

## 5min_trade Architecture

```
app.py                          # Main loop: scan markets → run strategies → execute signals
├── data/binance_signals.py     # 4-signal engine: momentum, divergence, order_flow, VWAP
├── data/gamma_client.py        # Polymarket Gamma API (market discovery)
├── data/clob_client.py         # CLOB orderbook wrapper with fallback prices
├── data/websocket_feed.py      # Real-time Polymarket price WebSocket
├── strategies/dynamic_picker.py # Master orchestrator — runs ALL 16 strategies, picks best
├── strategies/oracle_arb.py    # #1 strategy: Chainlink oracle delay exploit via Binance
├── strategies/time_decay.py    # Near-expiry Binance-cross-validated time decay
├── strategies/early_mover.py   # Buy cheap side on Binance reversal signals
├── trading/live_trader.py      # Core execution: FOK/GTC buys, dynamic exits, sell retries
├── trading/live_balance_manager.py # Risk modes: SEED→CONCENTRATION→MEDIUM→AGGRESSIVE
├── trading/auto_redeem.py      # Auto-redeem resolved positions (gasless relayer + on-chain)
└── bot/main.py                 # Telegram interface: /trade /stop /status /balance
```

### Key Data Flow
1. `app.py` scan loop (1-5s interval) discovers active markets via Gamma API
2. For each market: `dynamic_picker.analyze()` runs all strategies, returns best signal
3. `live_trader.execute_signal()` validates (cooldown, dedup, spread, balance) then places FOK buy
4. `live_trader.check_positions()` runs dynamic exit logic every scan cycle
5. Exit: FOK sell (aggressive pricing, cross spread) → 95% retry → GTC fallback → auto-settle

### Risk Mode Graduation
- **SEED** ($0-5): max 2 positions, 50% max bet, min_confidence=0.90, oracle_arb disabled
- **CONCENTRATION** ($5-20): max 4 positions
- **MEDIUM** ($20-100): max 8 positions
- **AGGRESSIVE** ($100+): max 12 positions, 2x wider stops, zero restrictions

### Critical Technical Details
- Polymarket prices = probabilities (0.01-0.99). Market settles to $1.00 (win) or $0.00 (lose)
- FOK orders have NO 5-share minimum (GTC does). FOK is preferred for $1-5 trades
- `price × shares` must have ≤2 decimal places (CLOB rejects otherwise) — GCD alignment in `_submit_order()`
- Confidence cap in `binance_signals.py` line ~473 controls signal quality ceiling
- Gnosis Safe proxy wallet (`0x4f9...CFC4`) holds funds; EOA signer (`0x871...244F`) signs txs
- Auto-redeem: gasless Builder relayer primary, on-chain Gnosis Safe fallback

## Git Workflow Rules (MANDATORY)

1. **Before committing**: Show summary of changes (file, what, why). ASK approval.
2. **Before pushing**: State what commits are being pushed. ASK approval.
3. **Never force-push** without explicit permission.

## Shortcut Keywords

- **`ggns`** — "Go fix this." Fix the bug/issue immediately, then ASK: "These are the changes, can I commit?"
- **`pop`** — "Full auto." Fix, commit, AND push without asking. Report what was done at the end.

## Goals

- **Growth-first**: $2 → $5 → $10 → $50 → $100+ as fast as possible.
- Every change must increase profits OR reduce losses. Both is best.
- New strategies always welcome — more edges = faster growth.
- Guards/safeguards only where truly needed (death spirals, signal spam). Never over-restrict.
- AGGRESSIVE mode: never add restrictions. It trades freely.

## Polymarket API Gotchas (Hard-Won Knowledge)

- **CLOB `update_balance_allowance`** returns stale/zero data — NEVER block sells on it. Fire-and-forget only.
- **`setApprovalForAll`** on CTF contract must be done once at startup for sells to work. Without it, every sell fails silently.
- **FOK orders fill at BEST available price**, not your limit price. Setting limit 1-2¢ lower than target doesn't lose money — it just widens the matching pool. Always cross the spread on sells.
- **GTC orders need ≥5 shares**. At SEED balance ($1-5), most positions have <5 shares → GTC sell is impossible. FOK is the only exit path.
- **`price × shares` must have ≤2 decimal places** or the CLOB silently rejects. Use GCD alignment (see `_submit_order()`).
- **Orderbook depth is thin** in 5-minute markets. FOK sell at exact trigger price has ~20% fill rate. Crossing spread gets ~60%+.
- **Markets expire and orderbooks vanish**. Sell attempts on expired markets throw "does not exist" — detect this and auto-settle.
- **Nonce conflicts** happen when multiple transactions hit the chain simultaneously. Use `_clear_pending_txs()` before important on-chain calls.

## Debugging Playbook

### Analyzing Railway Logs
```python
# Parse Railway JSON logs:
import json
with open('logs.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
msgs = [(x.get('timestamp','')[11:19], x.get('message','')) for x in data]
```
- Look for: `LIVE CLOSED` (P&L), `Signal:` (signal flow), `Skip:` (blocked trades), `❌` (errors)
- Key metrics to extract: fill rate (FOK success/attempts), hold times, signal-to-trade conversion, stop-loss frequency

### Common Error → Fix Patterns
| Error | Root Cause | Fix |
|-------|-----------|-----|
| `not enough balance` on sell | CLOB sees fewer shares than expected (rounding) | Retry with 95% shares via FOK |
| `allowance` errors on sell | CTF approval not set or CLOB cache stale | `ensure_ctf_approval()` at startup + fire-and-forget `update_balance_allowance` before sell |
| `lower than the minimum` | GTC with <5 shares | Skip GTC, use FOK only for small positions |
| `does not exist` on sell | Market expired while position was open | Auto-settle, let auto_redeem recover USDC |
| Gas errors / stuck txs | Nonce conflict or gas price too low | `_clear_pending_txs()` + use `pending` nonce + 1.5x gas multiplier |
| FOK buy rejected | No liquidity at that price level | Normal — not an error. Don't retry endlessly. |

## Strategy Performance Context (From Real Trading Data)

These are REAL results from live trading — use them to calibrate any strategy changes:
- **oracle_arb at MIN_EDGE=0.05**: Wrong 100% (17/17 signals, all losses). Now set to 0.10.
- **time_decay**: Flip-flopped direction (SOL UP then SOL DOWN within seconds). Different Binance timeframe windows disagree. Needs strong alignment.
- **Average hold time**: ~15 seconds in 5-min markets. Positions that survive >60s usually win.
- **FOK buy fill rate**: ~46% (normal — thin markets). Don't treat FOK rejection as an error.
- **FOK sell fill rate**: Was ~20% at exact price. With aggressive pricing (cross spread), expected ~60%+.
- **Signal spam pattern**: Same strategy can fire identical signals every 4 seconds. Signal dedup (10s window) stops this.
- **Stop-loss → re-entry gap**: Without cooldown, bot re-enters 3 seconds after being stopped out. Cooldown (60s same coin+dir) prevents this.
- **52% drawdown in 90 seconds** is possible in SEED mode without session breaker. Breaker at -40% with 5min pause catches this.

## Ideas Backlog (Reference Only — DO NOT auto-implement)

These are potential future improvements. Only implement when the user explicitly asks.
- **Liquidity-aware sizing**: Check orderbook depth before sizing. Don't buy $2 into a $0.50 bid.
- **Multi-timeframe confirmation**: Before entering 5m market, check if 15m/30m agrees.
- **Volatility regime detection**: High-vol = wider stops + smaller size. Low-vol = tighter.
- **Win-rate weighted strategy selection**: StrategyTracker exists but needs more data. Feed it.
- **Smart GTC placement**: Instead of FOK at market, place GTC 1¢ inside the spread for better fills.
- **Partial position closing**: Sell 50% at first target, let rest ride to settlement.
- **Correlation guard**: Don't enter BTC UP and ETH UP simultaneously (they're correlated, doubles risk).

## Code Conventions

- Compile-check all modified `.py` files before committing
- Railway deploy-ready: Python 3.11, no local-only deps, all deps in `requirements.txt`
- Emoji-tagged log lines for easy Railway log parsing (🎯 signal, ✅ filled, 💸 loss, 🤑 profit, ⚠️ warning, ❌ error)
- Async everywhere — `live_trader` runs sync CLOB calls via `loop.run_in_executor()`
- Strategy signals flow through `TradeSignal` dataclass (`strategies/base_strategy.py`)
- Position sizing via `LiveBalanceManager.get_position_size(confidence)` — scales with confidence and mode
