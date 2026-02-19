"""
Live Balance Manager — 3 Risk Modes for Real Trading

MODES:
  🎯 CONCENTRATION: Safe growth. Keep 50% reserve, small bets, high confidence bar.
  ⚖️ MEDIUM: Balanced. Keep 30% reserve, moderate bets, 1/3 risk ratio.
  🔥 AGGRESSIVE: Full compound. Keep $2 reserve only, big bets, low confidence bar.

All modes respect Polymarket's $1 minimum order size.
Position count is dynamic based on balance.
"""

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
        max_bet_pct=100.0,      # Can bet 100% — we only trade guaranteed arbs
        reserve_pct=0.0,        # Zero reserve — every cent counts at $1
        reserve_min=0.0,        # No minimum reserve
        max_pos_per_dollar=1.0, # 1 position per $1
        max_positions_cap=1,    # Only 1 position at a time (focus)
        min_confidence=0.90,    # ONLY near-guaranteed trades
        description='$1 start — near-zero risk, arb-only until $5',
    ),
    'concentration': LiveRiskMode(
        name='CONCENTRATION',
        emoji='🎯',
        max_bet_pct=15.0,       # Max 15% per trade
        reserve_pct=50.0,       # Keep 50% safe
        reserve_min=2.0,        # Always keep $2
        max_pos_per_dollar=0.2, # 1 position per $5
        max_positions_cap=3,    # Max 3 positions
        min_confidence=0.70,    # Only high-confidence trades
        description='Safe growth — focus on multiplying initial balance',
    ),
    'medium': LiveRiskMode(
        name='MEDIUM',
        emoji='⚖️',
        max_bet_pct=25.0,       # Max 25% per trade
        reserve_pct=30.0,       # Keep 30% safe
        reserve_min=1.50,       # Keep $1.50
        max_pos_per_dollar=0.3, # 1 position per $3.33
        max_positions_cap=6,    # Max 6 positions
        min_confidence=0.50,    # Moderate confidence bar
        description='Balanced — 1/3 risk ratio, steady growth',
    ),
    'aggressive': LiveRiskMode(
        name='AGGRESSIVE',
        emoji='🔥',
        max_bet_pct=40.0,       # Max 40% per trade
        reserve_pct=10.0,       # Keep 10%
        reserve_min=1.00,       # Keep $1 minimum
        max_pos_per_dollar=0.5, # 1 position per $2
        max_positions_cap=10,   # Max 10 positions
        min_confidence=0.35,    # Low bar — trade aggressively
        description='Full compound — maximum growth, higher risk',
    ),
}

# Auto-graduation thresholds for seed mode
SEED_GRADUATE_BALANCE = 5.0  # Graduate to concentration at $5


class LiveBalanceManager:
    """
    Dynamic balance management for live trading.
    
    Respects Polymarket $1 minimum order size.
    Position count scales with balance.
    Reserve ensures you never fully bust.
    """

    def __init__(self, balance: float, mode: str = 'concentration'):
        self.balance = balance
        self.mode_name = mode.lower()
        self.mode = LIVE_MODES.get(self.mode_name, LIVE_MODES['concentration'])
        self.open_positions = 0

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
        """Update balance after trade."""
        self.balance = new_balance

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
        """Check if we can open a new position."""
        if self.balance < Config.POLYMARKET_MIN_ORDER_SIZE:
            return False, "💀 Balance below minimum order size"
        if self.tradeable_balance < Config.POLYMARKET_MIN_ORDER_SIZE:
            return False, f"🛡️ Only reserve left (${self.reserve:.2f})"
        if self.open_positions >= self.max_positions:
            return False, f"📊 {self.open_positions}/{self.max_positions} positions open"
        return True, f"{self.mode.emoji} {self.mode.name}"

    def get_position_size(self, confidence: float) -> float:
        """
        Calculate position size. Dynamic based on balance, mode, and confidence.
        
        Always returns at least $1 (Polymarket minimum) or 0 if can't trade.
        """
        # Scale bet size with confidence
        pct = self.mode.max_bet_pct * (0.5 + confidence * 0.5)
        size = self.tradeable_balance * pct / 100

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
        Check if seed mode should auto-graduate to concentration.
        Returns message if graduated, empty string if not.
        """
        if self.mode_name == 'seed' and self.balance >= SEED_GRADUATE_BALANCE:
            self.set_mode('concentration')
            return (
                f"🎉 GRADUATED! Balance ${self.balance:.2f} reached ${SEED_GRADUATE_BALANCE:.2f}\n"
                f"Auto-switched to 🎯 CONCENTRATION mode for safer growth."
            )
        return ''

    def get_strategy_filter(self) -> Dict:
        """Which strategies are enabled at this mode."""
        if self.mode_name == 'seed':
            # SEED MODE: Only guaranteed-profit strategies
            return {
                'enabled': ['yes_no_arb', 'cross_tf_arb', 'oracle_arb'],
                'disabled': ['straddle', 'trend_follower', 'penny_sniper',
                           'cheap_hunter', 'momentum_reversal', 'spread_scalper',
                           'mid_sniper', 'time_decay'],
                'min_confidence': self.mode.min_confidence,
            }
        elif self.mode_name == 'concentration':
            return {
                'enabled': ['cheap_hunter', 'oracle_arb', 'yes_no_arb',
                           'cross_tf_arb'],
                'disabled': ['straddle', 'trend_follower', 'penny_sniper'],
                'min_confidence': self.mode.min_confidence,
            }
        elif self.mode_name == 'medium':
            return {
                'enabled': ['cheap_hunter', 'oracle_arb', 'yes_no_arb',
                           'cross_tf_arb', 'mid_sniper', 'spread_scalper',
                           'penny_sniper'],
                'disabled': ['straddle'],
                'min_confidence': self.mode.min_confidence,
            }
        else:  # aggressive
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
        }
