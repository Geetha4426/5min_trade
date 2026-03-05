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
from typing import Dict, Optional
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
        max_positions_cap=2,    # 2 positions — allows arb pairs (UP+DOWN)
        min_confidence=0.90,    # Very high bar — only near-certain trades fire
        description='$1-5 start — 2 positions, 0.90 confidence, safe growth',
    ),
    'plant': LiveRiskMode(
        name='PLANT',
        emoji='🌿',
        max_bet_pct=35.0,       # 35% — bigger bets than seed, still safe
        reserve_pct=20.0,       # 20% reserve
        reserve_min=2.0,        # Always keep $2
        max_pos_per_dollar=0.5, # 1 position per $2
        max_positions_cap=3,    # 3 positions max
        min_confidence=0.90,    # Same high bar as SEED — only best signals
        description='$5-15 — SEED confidence with dynamic $2-4 sizing',
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
# The bot progresses: seed → plant → medium → aggressive
# (concentration is an alternative manual mode at the same $5-20 range)
GRADUATION_THRESHOLDS = {
    'seed': ('plant', 5.0),           # $5 → graduate to plant
    'plant': ('medium', 20.0),        # $20 → graduate to medium
    'concentration': ('medium', 20.0),# $20 → graduate to medium
    'medium': ('aggressive', 100.0),  # $100 → graduate to aggressive
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

        # ── Cheap hunter mode override ──
        # When True, relaxes position limits (up to 10) and forces $1 bets
        self.cheap_hunter_mode = False

        # ── Session loss circuit breaker (SEED mode) ──
        # Pauses trading for 5 min if >40% session loss. Only in SEED: tiny
        # capital can evaporate in seconds without a breaker.
        self._session_start_balance: Optional[float] = None
        self._session_paused_until: float = 0

        # ── Open position value tracking ──
        # Allows session breaker to include estimated position value,
        # preventing false triggers when USDC drops after a buy.
        self._estimated_position_value: float = 0.0

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
        # Reset session breaker
        self._session_start_balance = self.balance
        self._session_paused_until = 0
        self._estimated_position_value = 0.0
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
        """Max positions based on balance and mode.
        
        Cheap hunter mode: allows up to 10 positions regardless of SEED cap.
        Normal mode: respects mode.max_positions_cap.
        """
        if self.cheap_hunter_mode:
            # Allow many small lottery bets — the whole point is volume
            by_balance = int(self.tradeable_balance / Config.POLYMARKET_MIN_ORDER_SIZE)
            return max(1, min(by_balance, 10))
        # Dynamic: scale with balance, respect $1 min per position
        by_balance = int(self.tradeable_balance * self.mode.max_pos_per_dollar)
        by_min_size = int(self.tradeable_balance / Config.POLYMARKET_MIN_ORDER_SIZE)
        return max(1, min(by_balance, by_min_size, self.mode.max_positions_cap))

    def can_trade(self, is_arb: bool = False) -> tuple:
        """Check if we can open a new position. Drawdown is alert-only, never blocks."""
        # ── Session loss circuit breaker (SEED/PLANT only) ──
        if self.mode_name in ('seed', 'plant'):
            if self._session_start_balance is None:
                self._session_start_balance = self.balance
            if self._session_start_balance > 0:
                # Include estimated open position value to avoid false
                # triggers when USDC drops purely from buying (not losing).
                effective_balance = self.balance + self._estimated_position_value
                session_loss_pct = (1 - effective_balance / self._session_start_balance) * 100
                if session_loss_pct >= 40:
                    now = time.time()
                    if self._session_paused_until == 0:
                        self._session_paused_until = now + 300  # 5 min pause
                        print(f"\n{'='*60}", flush=True)
                        print(f"🚨 SESSION BREAKER: -{session_loss_pct:.0f}% loss in SEED mode", flush=True)
                        print(f"  ${self._session_start_balance:.2f} → ${effective_balance:.2f} "
                              f"(USDC ${self.balance:.2f} + positions ~${self._estimated_position_value:.2f})", flush=True)
                        print(f"  Pausing for 5 minutes to break the losing streak.", flush=True)
                        print(f"{'='*60}\n", flush=True)
                    if now < self._session_paused_until:
                        remaining = (self._session_paused_until - now) / 60
                        return False, f"🚨 Session breaker ({remaining:.0f}m left)"
                    else:
                        # Pause expired — start new session
                        self._session_start_balance = self.balance
                        self._session_paused_until = 0

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

        # Dynamic arb slot: SEED/PLANT allow +1 position for dual-leg arb
        effective_cap = self.max_positions
        if is_arb and self.mode_name in ('seed', 'plant'):
            effective_cap = min(self.mode.max_positions_cap + 1, 3)

        if self.open_positions >= effective_cap:
            return False, f"📊 {self.open_positions}/{effective_cap} positions open"
        return True, f"{self.mode.emoji} {self.mode.name}"

    def can_afford_dual_leg(self) -> bool:
        """Check if balance supports a dual-leg trade (2× minimum per leg)."""
        return self.tradeable_balance >= Config.POLYMARKET_MIN_ORDER_SIZE * 2

    def get_position_size(self, confidence: float) -> float:
        """
        Calculate position size. Dynamic based on balance, mode, confidence,
        and consecutive loss multiplier.
        
        Cheap hunter mode: fixed $1 per bet (the whole strategy is to spread
        small bets across many markets).
        
        After consecutive losses, sizes gently shrink by 5% each.
        After consecutive wins, sizes grow by 5% each (capped at 130%).
        Always returns at least $1 (Polymarket minimum) or 0 if can't trade.
        """
        min_size = Config.POLYMARKET_MIN_ORDER_SIZE

        # Cheap hunter: fixed $1 bets — never risk more per lottery ticket
        if self.cheap_hunter_mode:
            return min_size if self.tradeable_balance >= min_size else 0

        # Scale bet size with confidence
        pct = self.mode.max_bet_pct * (0.5 + confidence * 0.5)
        size = self.tradeable_balance * pct / 100

        # Apply consecutive loss/win multiplier
        size *= self._size_multiplier

        # Enforce Polymarket minimum
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

    def check_auto_demote(self) -> str:
        """
        Auto-demote to a lower tier if balance dropped below the current tier's entry.
        Ensures the bot restarts in the correct mode after drawdowns.
        
        Demotion thresholds (reverse of graduation):
          aggressive → medium  if balance < $100
          medium → concentration  if balance < $20
          concentration → seed  if balance < $5
          seed stays seed (lowest tier)
        """
        DEMOTION = {
            'aggressive': ('medium', 100.0),
            'medium': ('concentration', 20.0),
            'concentration': ('seed', 5.0),
            'plant': ('seed', 3.0),    # Drop below $3 → back to seed
        }
        demote = DEMOTION.get(self.mode_name)
        if demote:
            lower_mode, threshold = demote
            if self.balance < threshold:
                old_name = self.mode.name
                self.set_mode(lower_mode)
                # Recursively check if we need to demote further
                # (e.g., aggressive with $3 should go all the way to seed)
                further = self.check_auto_demote()
                msg = (
                    f"📉 AUTO-DEMOTE: {old_name} → {self.mode.emoji} {self.mode.name}\n"
                    f"Balance ${self.balance:.2f} < ${threshold:.2f} threshold"
                )
                if further:
                    msg += f"\n{further}"
                return msg
        return ''

    def get_strategy_filter(self) -> Dict:
        """Which strategies are enabled at this mode.
        
        ALL modes enable ALL strategies — the confidence floor is the ONLY filter.
        EXCEPT:
        
        SEED ($1-5): blocks cheap_hunter, penny_sniper, prob_closer, oracle_arb
          - Lottery bets and thin-edge plays can wipe out $1-5 instantly.
        
        CONCENTRATION ($5-20): blocks prob_closer, oracle_arb
          - prob_closer: buys $0.90-0.95 for 5-10% edge.  One loss = -$1+
            on a $10 account.  Terrible R:R below $20.
          - oracle_arb: fires too often on noise and lost $1.17 in 60s
            during live testing (3 stop-losses in a row).  Needs larger
            bankroll to absorb variance.
        
        MEDIUM/AGGRESSIVE ($20+): all 16 strategies enabled.
        """
        disabled = []
        if self.mode_name in ('seed', 'plant'):
            # SEED/PLANT = capital preservation. Block strategies with bad risk/reward.
            disabled = ['cheap_hunter', 'penny_sniper', 'prob_closer', 'oracle_arb']
        elif self.mode_name == 'concentration':
            # CONCENTRATION ($5-20): prob_closer buys at $0.90-0.95 for 5-10%
            # edge.  One loss wipes $1+ on a $10 account — terrible R:R.
            # oracle_arb fires too often on noise and lost $1.17 in 60s during
            # live testing.  Both need a larger bankroll to absorb variance.
            disabled = ['prob_closer', 'oracle_arb']

        return {
            'enabled': 'all',
            'disabled': disabled,
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
