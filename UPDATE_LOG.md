# 5min_trade Update Log

## Overview
This log documents all major improvements and changes made to the 5min_trade Polymarket trading bot, from its initial version to the current state as of February 28, 2026.

---

## Major Improvements & Features

### 1. Bug Fixes and Core Logic Corrections
- Fixed fee calculation: Corrected to use the quadratic taker fee formula (`C×0.25×(p×(1-p))²`), matching Polymarket's new dynamic fee structure.
- Fixed event loop blocking: All synchronous CLOB client calls now run in an executor, preventing async freezes.
- GTC sell tracking: Pending sell orders are now tracked and confirmed before finalizing position closure.
- Startup order cleanup: On bot startup, all stale or orphaned orders are cancelled to prevent ghost positions.

### 2. Risk Management & Sizing
- Drawdown tracking: Added peak and daily drawdown tracking (now alert-only, never blocks trading).
- Consecutive loss/win sizing: Position size gently shrinks 5% per loss (min 60%) and grows 5% per win (max 130%).
- All drawdown and risk logic is alert-only—bot never halts itself.

### 3. Strategy & Performance Tracking
- StrategyTracker: Tracks win/loss/PnL per strategy, with confidence adjustments after 5+ trades.
- Strategy win rates and stats available via Telegram command `/stratstats`.

### 4. Trading Logic & Flexibility
- Position deduplication removed: Multiple positions on the same market are now allowed (per user request).
- No trading bottlenecks: All restrictions that could block trading (drawdown, dedup, etc.) have been removed or softened to alert-only.
- Telegram bot: New commands `/resume`, `/drawdown`, `/stratstats` for live monitoring and control.

### 5. Miscellaneous
- Configurable ARB_MAX_COMBINED_PRICE and fee constants.
- Improved flash crash detection and spread scalper logic.
- Dynamic per-leg fee handling in arbitrage and cross-timeframe strategies.
- All code syntax-checked and tested for stability.

---

## Summary of Changes from Initial Version
- +490 lines added, -72 lines removed across 13 files.
- 11 critical bug fixes and improvements implemented.
- 5 advanced features from top Polymarket bots integrated (drawdown alert, win tracking, startup cleanup, etc.).
- All over-restrictive logic removed per user feedback—bot is now optimized for aggressive, uninterrupted trading.
- All changes committed and pushed to GitHub (geetha4426/5min_trade).

---

## Last Updated
February 28, 2026
