"""
Live Balance Manager — Risk Modes + Smart Sizing

MODES:
  🎯 CONCENTRATION: Safe growth. Keep 50% reserve, small bets, high confidence bar.
  ⚖️ MEDIUM: Balanced. Keep 30% reserve, moderate bets, 1/3 risk ratio.
  🔥 AGGRESSIVE: Full compound. Keep $2 reserve only, big bets, low confidence bar.

SMART RISK ADAPTATION (does NOT block trading):
  - Peak/daily drawdown tracking → alerts only (logged + Telegram /drawdown)
  - Consecutive loss sizing → gentle 5% reduction per loss, 5% growth per win
  - Never halts, never pauses — the bot's job is to TRADE

All modes respect Polymarket's $1 minimum order size.
Position count is dynamic based on balance.
"""

import time
from typing import Dict
from config import Config


class LiveRiskMode:
    """Defines parameters for a live trading risk mode."""
    def __init__(self, name: str, emoji: str, max_bet_pct: float,
                 reserve_pct: float, reserve_min: float,
                 max_pos_per_dollar: float, max_positions_cap: int,
                 min_confidence: float, description: str):
        self.name = name
        self.emoji = emoji
        self.max_bet_pct = max_bet_pct          # Max % of tradeable balance per trade
        self.reserve_pct = reserve_pct          # % of balance to keep as reserve
        self.reserve_min = reserve_min          # Absolute minimum reserve ($)
        self.max_pos_per_dollar = max_pos_per_dollar  # Positions per dollar of balance
        self.max_positions_cap = max_positions_cap    # Hard cap on positions
        self.min_confidence = min_confidence    # Minimum strategy confidence to trade
        self.description = description


# ═══════════════════════════════════════════════════════════════════
# RISK MODE DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

LIVE_MODES = {
    'seed': LiveRiskMode(
        name='SEED',
        emoji='🌱',
        max_bet_pct=50.0,       # Max 50% per trade — protect the stack
        reserve_pct=15.0,       # Keep 15% as buffer for sell fees/emergencies
        reserve_min=0.50,       # Always keep $0.50 minimum reserve
        max_pos_per_dollar=1.0, # 1 position per $1
        max_positions_cap=1,    # ONE position at a time — never spread $4 thin
        min_confidence=0.90,    # Very high bar — only near-certain trades fire
        description='$1-5 start — 1 position, 0.90 confidence, safe growth',
    ),
    'concentration': LiveRiskMode(
        name='CONCENTRATION',
        emoji='🎯',
        max_bet_pct=20.0,       # Max 20% per trade
        reserve_pct=40.0,       # Keep 40% safe
        reserve_min=2.0,        # Always keep $2
        max_pos_per_dollar=0.25,# 1 position per $4
        max_positions_cap=4,    # Max 4 positions
        min_confidence=0.65,    # High-confidence trades
        description='$5-20 — focused growth with safety net',
    ),
    'medium': LiveRiskMode(
        name='MEDIUM',
        emoji='⚖️',
        max_bet_pct=30.0,       # Max 30% per trade
        reserve_pct=25.0,       # Keep 25% safe
        reserve_min=2.00,       # Keep $2
        max_pos_per_dollar=0.35,# 1 position per ~$3
        max_positions_cap=8,    # Max 8 positions
        min_confidence=0.45,    # Moderate confidence bar
        description='$20-100 — balanced risk, more strategies enabled',
    ),
    'aggressive': LiveRiskMode(
        name='AGGRESSIVE',
        emoji='🔥',
        max_bet_pct=45.0,       # Max 45% per trade
        reserve_pct=10.0,       # Keep 10%
        reserve_min=1.00,       # Keep $1 minimum
        max_pos_per_dollar=0.5, # 1 position per $2
        max_positions_cap=12,   # Max 12 positions
        min_confidence=0.30,    # Low bar — trade aggressively
        description='$100+ — full compound, all 15 strategies, maximum growth',
    ),
}

# Auto-graduation thresholds
# The bot progresses: seed → concentration → medium → aggressive
GRADUATION_THRESHOLDS = {
    'seed': ('concentration', 5.0),       # $5 → graduate to concentration
    'concentration': ('medium', 20.0),    # $20 → graduate to medium
    'medium': ('aggressive', 100.0),      # $100 → graduate to aggressive
}

# Exported constant for bot/main.py seed progress display
SEED_GRADUATE_BALANCE = GRADUATION_THRESHOLDS['seed'][1]  # $5


class LiveBalanceManager:
    """
    Dynamic balance management for live trading.
    
    Features:
    - Respects Polymarket $1 minimum order size
    - Position count scales with balance
    - Reserve ensures you never fully bust
    - Multi-layer drawdown protection (daily + peak)
    - Consecutive loss sizing (shrink bets after losses)
    """

    # ── Consecutive loss/win sizing (gentle — never blocks) ──
    LOSS_SHRINK_FACTOR = 0.95      # Reduce bet by 5% per consecutive loss
    WIN_GROW_FACTOR = 1.05         # Increase bet by 5% per consecutive win
    MAX_SIZE_MULTIPLIER = 1.30     # Max growth from wins (130% of normal)
    MIN_SIZE_MULTIPLIER = 0.60     # Min shrink from losses (60% of normal)

    def __init__(self, balance: float, mode: str = 'concentration'):
        self.balance = balance
        self.mode_name = mode.lower()
        self.mode = LIVE_MODES.get(self.mode_name, LIVE_MODES['concentration'])
        self.open_positions = 0

        # ── Drawdown tracking (alert-only, never blocks) ──
        self.peak_balance = balance          # Highest balance ever seen
        self.daily_start_balance = balance   # Balance at start of day
        self._daily_reset_ts = time.time()   # When daily tracking started
        self._drawdown_alerted = False       # True if drawdown alert sent

        # ── Consecutive loss tracking ──
        self._consecutive_losses = 0
        self._consecutive_wins = 0
        self._size_multiplier = 1.0          # Dynamic bet size multiplier

    def set_mode(self, mode: str):
        """Switch risk mode."""
        mode = mode.lower()
        if mode in LIVE_MODES:
            old = self.mode.name
            self.mode_name = mode
            self.mode = LIVE_MODES[mode]
            print(f"📊 RISK MODE: {old} → {self.mode.emoji} {self.mode.name}", flush=True)
            return True
        return False

    def update_balance(self, new_balance: float):
        """Update balance after trade. Tracks peak and daily reset."""
        self.balance = new_balance
        # Update peak balance (only goes up)
        if new_balance > self.peak_balance:
            self.peak_balance = new_balance
        # Reset daily tracking every 24h
        now = time.time()
        if now - self._daily_reset_ts > 86400:  # 24 hours
            self.daily_start_balance = new_balance
            self._daily_reset_ts = now
            self._daily_paused_until = 0.0

    def record_result(self, won: bool):
        """Track consecutive wins/losses for dynamic sizing."""
        if won:
            self._consecutive_wins += 1
            self._consecutive_losses = 0
            # Grow size multiplier (capped)
            self._size_multiplier = min(
                self.MAX_SIZE_MULTIPLIER,
                self._size_multiplier * self.WIN_GROW_FACTOR
            )
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0
            # Shrink size multiplier (floored)
            self._size_multiplier = max(
                self.MIN_SIZE_MULTIPLIER,
                self._size_multiplier * self.LOSS_SHRINK_FACTOR
            )

    def reset_tracking(self) -> str:
        """Reset drawdown tracking and consecutive loss counter."""
        self.peak_balance = self.balance
        self.daily_start_balance = self.balance
        self._daily_reset_ts = time.time()
        self._drawdown_alerted = False
        self._size_multiplier = 1.0
        self._consecutive_losses = 0
        self._consecutive_wins = 0
        return f"✅ Tracking reset. Peak=${self.balance:.2f}, sizing=1.00×"

    @property
    def daily_pnl_pct(self) -> float:
        """Current daily PnL as percentage."""
        if self.daily_start_balance <= 0:
            return 0.0
        return (self.balance - self.daily_start_balance) / self.daily_start_balance * 100

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak as percentage."""
        if self.peak_balance <= 0:
            return 0.0
        return (self.peak_balance - self.balance) / self.peak_balance * 100

    @property
    def reserve(self) -> float:
        """Amount to keep untouched."""
        return max(self.mode.reserve_min, self.balance * self.mode.reserve_pct / 100)

    @property
    def tradeable_balance(self) -> float:
        """Balance available for trading."""
        return max(0, self.balance - self.reserve)

    @property
    def max_positions(self) -> int:
        """Max positions based on balance and mode."""
        # Dynamic: scale with balance, respect $1 min per position
        by_balance = int(self.tradeable_balance * self.mode.max_pos_per_dollar)
        by_min_size = int(self.tradeable_balance / Config.POLYMARKET_MIN_ORDER_SIZE)
        return max(1, min(by_balance, by_min_size, self.mode.max_positions_cap))

    def can_trade(self) -> tuple:
        """Check if we can open a new position. Drawdown is alert-only, never blocks."""
        # ── Drawdown alerts (log only, NEVER block trading) ──
        dd = self.drawdown_pct
        if dd >= 25 and not self._drawdown_alerted:
            self._drawdown_alerted = True
            print(f"⚠️ DRAWDOWN ALERT: {dd:.1f}% from peak "
                  f"${self.peak_balance:.2f} → ${self.balance:.2f}", flush=True)
        elif dd < 15:
            self._drawdown_alerted = False  # Reset alert when recovered

        # ── Standard checks (the ONLY things that block trading) ──
        if self.balance < Config.POLYMARKET_MIN_ORDER_SIZE:
            return False, "💀 Balance below minimum order size"
        if self.tradeable_balance < Config.POLYMARKET_MIN_ORDER_SIZE:
            return False, f"🛡️ Only reserve left (${self.reserve:.2f})"
        if self.open_positions >= self.max_positions:
            return False, f"📊 {self.open_positions}/{self.max_positions} positions open"
        return True, f"{self.mode.emoji} {self.mode.name}"

    def can_afford_dual_leg(self) -> bool:
        """Check if balance supports a dual-leg trade (2× minimum per leg)."""
        return self.tradeable_balance >= Config.POLYMARKET_MIN_ORDER_SIZE * 2

    def get_position_size(self, confidence: float) -> float:
        """
        Calculate position size. Dynamic based on balance, mode, confidence,
        and consecutive loss multiplier.
        
        After consecutive losses, sizes gently shrink by 5% each.
        After consecutive wins, sizes grow by 5% each (capped at 130%).
        Always returns at least $1 (Polymarket minimum) or 0 if can't trade.
        """
        # Scale bet size with confidence
        pct = self.mode.max_bet_pct * (0.5 + confidence * 0.5)
        size = self.tradeable_balance * pct / 100

        # Apply consecutive loss/win multiplier
        size *= self._size_multiplier

        # Enforce Polymarket minimum
        min_size = Config.POLYMARKET_MIN_ORDER_SIZE
        max_size = self.tradeable_balance * 0.50  # Never more than 50% of tradeable

        size = max(min_size, min(size, max_size))

        # Can't afford even the minimum?
        if size > self.tradeable_balance:
            return 0

        return round(size, 2)

    def check_auto_graduate(self) -> str:
        """
        Check if current mode should auto-graduate to the next tier.
        Progression: seed → concentration → medium → aggressive
        Returns message if graduated, empty string if not.
        """
        grad = GRADUATION_THRESHOLDS.get(self.mode_name)
        if grad:
            next_mode, threshold = grad
            if self.balance >= threshold:
                self.set_mode(next_mode)
                next_m = self.mode
                return (
                    f"🎉 GRADUATED! Balance ${self.balance:.2f} reached ${threshold:.2f}\n"
                    f"Auto-switched to {next_m.emoji} {next_m.name}: {next_m.description}"
                )
        return ''

    def get_strategy_filter(self) -> Dict:
        """Which strategies are enabled at this mode.
        
        ALL modes enable ALL strategies — the confidence floor is the ONLY filter.
        Each strategy's confidence formula already encodes its risk level.
        A 0.90 confidence mean_reversion is just as safe as a 0.90 arb.
        No arbitrary strategy whitelists — let the math decide.
        """
        return {
            'enabled': 'all',
            'disabled': [],
            'min_confidence': self.mode.min_confidence,
        }

    def get_status(self) -> Dict:
        return {
            'balance': self.balance,
            'mode': self.mode.name,
            'mode_emoji': self.mode.emoji,
            'mode_desc': self.mode.description,
            'tradeable': self.tradeable_balance,
            'reserve': self.reserve,
            'max_bet_pct': self.mode.max_bet_pct,
            'max_positions': self.max_positions,
            'open_positions': self.open_positions,
            'min_confidence': self.mode.min_confidence,
            # Drawdown protection
            'peak_balance': self.peak_balance,
            'drawdown_pct': self.drawdown_pct,
            'daily_pnl_pct': self.daily_pnl_pct,
            'drawdown_alerted': self._drawdown_alerted,
            # Consecutive loss sizing
            'consecutive_losses': self._consecutive_losses,
            'consecutive_wins': self._consecutive_wins,
            'size_multiplier': self._size_multiplier,
        }
