# 5min_trade — Full Session Log (Backup)

> Last updated: March 3, 2026
> This file preserves ALL context from development sessions so nothing is lost if chat history resets.
> **Primary reference**: See SESSION_MEMORY.md for the latest, most complete state.

---

## March 3, 2026 — Fee Formula Fix + Binance Confirmation

### Changes (commit 24ccef5)
1. **Fee formula fixed** across 6 files: `0.25 × p × (1-p)²` (was wrong `C × 0.25 × (p*q)²` capped at 1.56%)
2. **BASE_TAKER_FEE_RATE**: 0.0156 → 0.03125
3. **Exit decision**: sell_fee uses current_price, settlement = FREE, removed TAKER_FEE_RATE race condition
4. **SpreadScalper**: min_profit_spread 3.5% → 7%
5. **Binance confirmation**: MeanReversionScalper + SpikeFade now check Binance 30s BTC direction
6. **CONCENTRATION mode**: disabled prob_closer + oracle_arb
7. **Oracle_arb**: kept enabled in MEDIUM + AGGRESSIVE (user decision)

### Mode Analysis Delivered
Full EV tables for all 4 modes (SEED/CONCENTRATION/MEDIUM/AGGRESSIVE) with survival rates, strategy counts, growth paths. Recommended CONCENTRATION with $10 start.

---

## March 2, 2026 — Sell Failure Fix + Strategy Overhaul

## Project Overview

Automated Polymarket crypto scalper bot for 5/15/30-minute Up/Down binary markets.
- **Deployed on**: Railway (auto-deploy on git push to main)
- **Language**: Python 3.11, asyncio
- **Telegram bot** for control: /trade /stop /status /balance /strategy
- **16 strategies** orchestrated by `dynamic_picker.py`
- **Live CLOB trading** via `py-clob-client` (FOK + GTC orders)

## Wallet Setup

| Component | Address |
|-----------|---------|
| Gnosis Safe (proxy, holds funds) | `0x4f9fBe936a35D556894737235dF49cFcD5d5CFC4` |
| EOA signer (signs txs) | `0x871faC3EEE45e620606c1d8e228984d2d322244F` |
| Chain | Polygon PoS |
| Token | USDC |
| Balance (as of Mar 2) | ~$2.96 (SEED mode) |

## Risk Mode Graduation

| Mode | Balance | Max Positions | Max Bet | Min Confidence |
|------|---------|--------------|---------|----------------|
| SEED | $0-5 | 2 | 50% | 0.90 |
| CONCENTRATION | $5-20 | 4 | 40% | 0.70 |
| MEDIUM | $20-100 | 8 | 30% | 0.50 |
| AGGRESSIVE | $100+ | 12 | 25% | 0.40 |

Disabled in SEED: `cheap_hunter`, `penny_sniper`, `prob_closer`, `oracle_arb`

## All Commits This Session (Chronological)

### Gas/Tx Bug Fixes (Phase 1)
- `f8ffbee` — Fixed signHash AttributeError in Gnosis Safe
- `06c07f6` — Fixed allowance=0 blocking sells
- `4377748` — Extracted `_execute_via_safe()` helper
- `acee8e0` — Added `ensure_ctf_approval()` at startup
- `712540d` — Added `/redeem` Telegram command
- `19197e4` — Fixed receipt timeout + nonce conflicts
- `1ac99b1` — Masked API key in logs
- `2bf8f78` — Made allowance=0 non-blocking
- `5b6c895` — Gas pricing fix (iteration 1)
- `190d545` — Gas pricing fix (iteration 2) + `_clear_pending_txs()`
- `32a0ed5` — Gas pricing fix (iteration 3)
- `5739a99` — Gas pricing fix (iteration 4) — ALL TXS WORKING

### Strategy & Feature Additions (Phase 2)
- `c090fe9` — Time Decay V2 (Binance cross-validation) + Early Mover strategy + dynamic_picker updates
- `e648fe9` — Position discovery improvements + USDC payout tracking + gasless relayer restructure

### Dynamic Stop-Loss & Guards (Phase 3)
- `d7beef5` — Dynamic stop-loss (entry-price-tiered, strategy-aware, time-aware, mode-multiplied). SEED max_positions 1→2. Cooldown/conflict scoped to SEED/CONCENTRATION only. oracle_arb disabled in SEED.
- `e6c438d` — Deep analysis fixes: aggressive sell pricing (cross spread 1-2¢), signal dedup (10s), spread guard (>6%), session breaker (SEED -40% → 5min pause), post-loss throttle (5s), oracle_arb MIN_EDGE 0.05→0.10, confidence cap 0.90→0.95
- `b3aeb32` — Copilot instructions file (ggns/pop shortcuts)
- `62ec46c` — Full knowledge base in copilot instructions

## Key Design Decisions (With Rationale)

### Why dynamic stop-loss instead of fixed %?
- Fixed -16% stop killed every position in 15 seconds (avg hold time was 15s)
- Cheap entries ($0.05) need -60% stops (lottery math: lose small, win big)
- Expensive entries ($0.85) need -8% stops (high-prob bets, protect gains)
- Near expiry (<30s): NO stop at all — let market settle to $1 or $0
- AGGRESSIVE mode gets 2x wider stops than SEED

### Why signal dedup instead of removing strategies?
- oracle_arb is valuable in CONCENTRATION+ mode — just spammy
- Same signal firing 14x in 60s wastes capital on identical bad trades
- 10s dedup window blocks spam but allows genuinely new signals through

### Why aggressive sell pricing?
- FOK sell at exact trigger price: 20% fill rate (dead)
- FOK sell 1-2¢ below: fills at BEST available bid anyway
- Exchange ALWAYS fills at best price — lower limit just widens matching pool
- Expected improvement: 20% → 60%+ fill rate

### Why session breaker at -40% not -30%?
- User feedback: "15 min pause is too long, growth-first"
- -30% triggers too often in volatile 5-min markets
- -40% only fires during extreme death spirals (52% drawdown in 90s)
- 5 min pause (not 15) — enough to break streak, short enough to resume

### Why confidence cap 0.90 → 0.95?
- At 0.90 cap, every signal hit exactly SEED's min_confidence threshold
- No way to distinguish great signals from mediocre ones
- At 0.95, strong signals get bigger position sizes → more profit on winners

## Live Trading Results (From Log Analysis)

### 5-Minute Session Data (14:49-14:54 UTC)
- 34 signals generated, 13 buy attempts, 6 orders filled (46% fill rate)
- 15 sell attempts, 3 fills (20% fill rate) ← BEFORE aggressive pricing fix
- 4 trades closed: ALL losses, ALL stop-losses
- 1 position settled unsold (auto-redeem picks it up)
- Total P&L: -$1.50 ($4.98 → ~$2.96)
- 52% peak drawdown in ~90 seconds

### Root Causes Identified
1. oracle_arb wrong 100% of the time (17 BTC UP signals while BTC crashed)
2. Stop-loss too tight (fixed -16%) killed positions before they could recover
3. FOK sell at exact price had 20% fill rate — positions couldn't exit
4. Signal spam: same signal every 4 seconds, burning capital on duplicates
5. 3-second re-entry after stop-loss (revenge trading)
6. All positions had <5 shares → GTC sell impossible

### What Was Fixed (Commits d7beef5 + e6c438d)
- Dynamic stop-loss with 6 price tiers + strategy override + mode multiplier
- Aggressive sell pricing (cross spread)
- Signal dedup (10s window)
- Spread guard (>6% rejected)
- Session breaker (-40% → 5min pause, SEED only)
- Post-loss throttle (5s global pause)
- oracle_arb MIN_EDGE doubled to 0.10

## Auto-Redeem System

- **Primary**: Gasless Builder relayer (`py-builder-relayer-client`)
- **Fallback**: On-chain via Gnosis Safe `execTransaction`
- **How it works**: Scans for resolved markets, merges positions, redeems conditional tokens → USDC
- **File**: `trading/auto_redeem.py` (~1177 lines)
- **Successfully redeemed**: 5 positions on-chain (confirmed)

## File Quick Reference

| File | Lines | Purpose |
|------|-------|---------|
| `app.py` | ~652 | Main scan loop, market discovery, signal→trade pipeline |
| `trading/live_trader.py` | ~1670 | Order execution, exits, sell retries, position tracking |
| `trading/live_balance_manager.py` | ~430 | Risk modes, sizing, graduation, session breaker |
| `trading/auto_redeem.py` | ~1177 | Auto-redemption (gasless + on-chain) |
| `strategies/dynamic_picker.py` | ~240 | Runs all 16 strategies, picks best signal |
| `strategies/oracle_arb.py` | ~175 | Chainlink oracle delay exploit via Binance |
| `strategies/time_decay.py` | ~176 | Near-expiry Binance cross-validated trades |
| `strategies/early_mover.py` | new | Buy cheap side on Binance reversal |
| `data/binance_signals.py` | ~530 | 4-signal engine: momentum, divergence, flow, VWAP |
| `config.py` | ~278 | All configuration, timeframe params |

## Environment Variables (Railway)

| Variable | Purpose |
|----------|---------|
| `POLY_PRIVATE_KEY` | EOA signer private key |
| `POLY_API_KEY` | Polymarket CLOB API key |
| `POLY_API_SECRET` | CLOB API secret |
| `POLY_PASSPHRASE` | CLOB API passphrase |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `PROXY_ADDRESS` | Gnosis Safe address |
| `BINANCE_API_KEY` | Binance API key (read-only) |
| `BINANCE_API_SECRET` | Binance API secret |
| `ENABLED_TIMEFRAMES` | e.g. "5,15" |

---

*This file is a backup. The actual AI instructions live in `.github/copilot-instructions.md`.*
