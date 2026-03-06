# Session Memory — March 3, 2026

> **Purpose**: Preserves ALL session context so nothing is lost if chat resets.
> **Last updated**: March 3, 2026

---

## LATEST COMMIT (DEPLOYED)

```
24ccef5 — fix: correct Polymarket fee formula + exit logic + Binance confirmation
```
**Pushed to main** on March 3, 2026. Railway auto-deploys.

---

## WHAT CHANGED — MARCH 3 SESSION

### 1. FEE FORMULA FIX (Critical — affected ALL PnL calculations)

**The Bug**: Fee formula was fundamentally wrong in 6 files.
- **OLD (wrong)**: `C_calibration × 0.25 × (p*q)²` → capped at 1.56%
- **NEW (correct)**: `0.25 × p × (1-p)²` → peaks ~3.7% at p≈0.33, 3.125% at p=0.50

| Price (p) | Old Fee Rate | Real Fee Rate | Error |
|-----------|-------------|--------------|-------|
| 0.10 | ~0.20% | 2.03% | 10x under |
| 0.33 | ~1.23% | 3.70% | 3x under |
| 0.50 | 1.56% | 3.125% | 2x under |
| 0.78 | ~1.12% | 0.94% | ~1.2x over |
| 0.90 | ~0.56% | 0.23% | ~2.5x over |

**Files fixed**: live_trader.py, probability_closer.py, cross_timeframe_arb.py, continuous.py, config.py

**Source**: Polymarket docs, ctf-exchange/Fees.sol, NegRiskAdapter.sol, Grok independent analysis, X devs @TVS_Kolia @DextersSolab @0xPhilanthrop

### 2. EXIT DECISION FIX (live_trader.py)
- sell_fee now computed at current_price (was using entry_price fee)
- Settlement = **FREE** (0% fee) → max_payout = `1.0 / (entry_price × (1 + entry_fee))`
- Removed `self.TAKER_FEE_RATE = trade_fee` race condition (leaked one trade's fee into other positions)
- Fee stored per-position in trade dict as `fee_rate`

### 3. BINANCE CONFIRMATION (swing_scalpers.py)
- **MeanReversionScalper**: Skip if Binance confirms crash, boost +0.06 if supports bounce
- **SpikeFade**: Skip if Binance aligns with spike, boost +0.06 if opposes spike

### 4. STRATEGY FILTERING (live_balance_manager.py)
| Mode | Disabled Strategies |
|------|-------------------|
| SEED ($0-5) | cheap_hunter, penny_sniper, prob_closer, oracle_arb |
| CONCENTRATION ($5-20) | **prob_closer, oracle_arb** ← NEW |
| MEDIUM ($20-100) | (none) |
| AGGRESSIVE ($100+) | (none) |

### 5. SPREAD SCALPER THRESHOLD (continuous.py)
- `min_profit_spread` bumped from `ask × 0.035` to `ask × 0.07`
- Old 3.5% assumed 1.56% per leg; real ≈ 3.125% per leg → need 7%

---

## MARCH 2 SESSION — SELL FAILURE FIX

### Root Cause
Commit `4cc7330` broke ALL sells by making `_ensure_conditional_allowance` block on stale CLOB cache data.

### Fix
Reverted to fire-and-forget: call `update_balance_allowance()` and proceed, never check response.

### Sell Flow (Current)
1. `_close_position()` → fire-and-forget allowance
2. FOK sell → FOK 95% → GTC → GTC -1¢
3. After 3 failures → `sell_failed_settle` (auto-settle, wait for redeem)

---

## POLYMARKET FEE FORMULA — DEFINITIVE REFERENCE

```
fee_amount = num_shares × 0.25 × [price × (1 - price)]²
effective_rate = 0.25 × price × (1 - price)²
```

- Settlement (payout): **FREE** (0% fee)
- Peak: ~3.7% at p≈0.33
- At p=0.50: 3.125%
- At p=0.78: 0.94%
- At p=0.90: 0.23%

Source: `ctf-exchange/Fees.sol` → `getInitialFees()`. py-clob-client fetches per-token rate from `/fee-rate?token_id=X`.

---

## FOUR TRADING MODES

| Mode | Balance | Positions | Max Bet | Min Conf | Active Strategies |
|------|---------|-----------|---------|----------|------------------|
| SEED | $0-5 | 2 | 50% | 0.90 | ~6 |
| CONCENTRATION | $5-20 | 4 | 40% | 0.65 | ~10 |
| MEDIUM | $20-100 | 8 | 30% | 0.45 | All 16 |
| AGGRESSIVE | $100+ | 12 | 25% | 0.30 | All 16 |

**Recommendation**: Start with $10 in CONCENTRATION mode.

---

## ORACLE_ARB STATUS

Disabled in SEED + CONCENTRATION. Enabled in MEDIUM + AGGRESSIVE.

**How it works**: Exploits Chainlink oracle ~60s update lag vs Binance real-time.
**Problems**: Fires on noise, 3 consecutive losses in 60s, no time-near-expiry gate.
**Potential fix (not implemented)**: Add `remaining_time < 120s` gate.

---

## COMMIT HISTORY

| Commit | Description | Date |
|--------|-------------|------|
| `24ccef5` | Fee formula fix + exit logic + Binance confirmation | Mar 3 |
| `c49acf2` | Sell fire-and-forget revert (the real fix) | Mar 2 |
| `712540d` | allowance=0 non-blocking + force-approve startup | Mar 2 |
| `acee8e0` | mask RPC API key in logs | Mar 2 |
| `4377748` | robust receipt handling, 180s timeout, nonce retry | Mar 2 |
| `06c07f6` | /redeem Telegram command | Mar 2 |
| `f8ffbee` | signHash→unsafe_sign_hash + CTF approval via Safe | Mar 2 |
| `e6c438d` | aggressive sell pricing, signal dedup, spread guard, session breaker | Mar 2 |
| `d7beef5` | Dynamic stop-loss (entry-price-tiered, strategy-aware) | Mar 2 |
| `c090fe9` | Time Decay V2 + Early Mover + dynamic_picker | Mar 2 |

---

## WALLET & DEPLOYMENT

| Component | Value |
|-----------|-------|
| Gnosis Safe | `0x4f9fBe936a35D556894737235dF49cFcD5d5CFC4` |
| EOA signer | `0x871faC3EEE45e620606c1d8e228984d2d322244F` |
| Chain | Polygon PoS |
| Token | USDC |
| Deploy | Railway (auto on push to main) |
| Python | 3.11+ |
| Balance (Mar 2) | ~$2.96 (SEED mode) |

---

## KEY ARCHITECTURE

### Allowance vs Approval
- **CLOB allowance** (`/balance-allowance`): Stale cached view. NEVER block sells on this.
- **On-chain approval** (`isApprovedForAll`): The real truth. Set once at startup.
- **`update_balance_allowance`**: Fire-and-forget GET asking CLOB to re-read chain.

### Auto-Redeem
- Primary: Gasless Builder relayer
- Fallback: On-chain via Gnosis Safe `execTransaction`
- Successfully redeemed 5 positions

---

## FILE REFERENCE

| File | ~Lines | Purpose |
|------|--------|---------|
| `app.py` | 652 | Main scan loop, market discovery, signal→trade pipeline |
| `trading/live_trader.py` | 1670 | Order execution, exits, sell retries, position tracking |
| `trading/live_balance_manager.py` | 430 | Risk modes, sizing, graduation, session breaker |
| `trading/auto_redeem.py` | 1177 | Auto-redemption (gasless + on-chain) |
| `strategies/dynamic_picker.py` | 240 | Runs all 16 strategies, picks best signal |
| `strategies/swing_scalpers.py` | 320 | MeanReversion, SpikeFade, ExpiryRush, BinanceMomentum |
| `strategies/continuous.py` | 377 | TrendFollower, Straddle, SpreadScalper, MidPriceSniper |
| `strategies/probability_closer.py` | 200 | Near-expiry high-prob entry |
| `strategies/cross_timeframe_arb.py` | 180 | 5min/15min overlapping market arb |
| `strategies/oracle_arb.py` | 175 | Chainlink oracle delay exploit |
| `strategies/time_decay.py` | 176 | Near-expiry Binance cross-validated |
| `strategies/early_mover.py` | 150 | Buy cheap side on Binance reversal |
| `data/binance_signals.py` | 530 | 4-signal engine: momentum, divergence, flow, VWAP |
| `config.py` | 278 | All configuration, timeframe params |

---

## ENVIRONMENT VARIABLES (Railway)

| Variable | Purpose |
|----------|---------|
| `POLY_PRIVATE_KEY` | EOA signer private key |
| `POLY_API_KEY` | Polymarket CLOB API key |
| `POLY_API_SECRET` | CLOB API secret |
| `POLY_PASSPHRASE` | CLOB API passphrase |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `PROXY_ADDRESS` | Gnosis Safe address |
| `BINANCE_API_KEY` | Binance API key (read-only) |
| `BINANCE_API_SECRET` | Binance API secret |
| `POLYGON_RPC_URL` | Alchemy Polygon RPC |
| `ENABLED_TIMEFRAMES` | e.g. "5,15" |
| `TAKER_FEE_RATE` | Default 0.03125 |

---

*End of session memory*
- poly_trade: uses `create_market_order` (MarketOrderArgs) → market orders (FAK/FOK)
- Both work. Different approach, same CLOB.

### Infrastructure
- Railway: Python 3.11, EU-West
- `eth-account` on Railway has `unsafe_sign_hash` (not `signHash`)
- Proxy wallet: `0x4f9fBe936a35D556894737235dF49cFcD5d5CFC4` (Gnosis Safe)
- EOA signer: `0x871faC3E...244F`
- Alchemy RPC: set as POLYGON_RPC_URL on Railway

---

## TOMORROW'S FIRST ACTION
1. `git add -A && git commit -m "fix: revert _ensure_conditional_allowance to fire-and-forget (root cause of all sell failures)" && git push`
2. Railway auto-deploys
3. Wait for a sell signal → confirm it works in logs
4. If sell works → done. The root cause is fixed.
