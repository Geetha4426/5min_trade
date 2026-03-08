"""
Inline Keyboards for the Telegram bot.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard():
    """Main menu keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Start Trading", callback_data="cmd_trade"),
            InlineKeyboardButton("⏹️ Stop", callback_data="cmd_stop"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
            InlineKeyboardButton("💰 Balance", callback_data="cmd_balance"),
        ],
        [
            InlineKeyboardButton("🧠 Strategy", callback_data="cmd_strategy"),
            InlineKeyboardButton("📜 History", callback_data="cmd_history"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="cmd_settings"),
        ],
    ])


def timeframe_keyboard():
    """Timeframe selection keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ 5 min", callback_data="tf_5"),
            InlineKeyboardButton("🕐 15 min", callback_data="tf_15"),
            InlineKeyboardButton("🕑 30 min", callback_data="tf_30"),
        ],
        [
            InlineKeyboardButton("🌀 All Timeframes", callback_data="tf_all"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])


def strategy_keyboard():
    """Strategy selection keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎰 Cheap Hunter", callback_data="strat_cheap_hunter"),
            InlineKeyboardButton("📉📈 Reversal", callback_data="strat_momentum_reversal"),
        ],
        [
            InlineKeyboardButton("📈 Trend", callback_data="strat_trend_follower"),
            InlineKeyboardButton("🔀 Straddle", callback_data="strat_straddle"),
        ],
        [
            InlineKeyboardButton("📊 Spread", callback_data="strat_spread_scalper"),
            InlineKeyboardButton("🎯 Mid Sniper", callback_data="strat_mid_sniper"),
        ],
        [
            InlineKeyboardButton("💰 YES+NO Arb", callback_data="strat_yes_no_arb"),
            InlineKeyboardButton("🎯 Oracle", callback_data="strat_oracle_arb"),
        ],
        [InlineKeyboardButton("⏰ Time Decay", callback_data="strat_time_decay")],
        [InlineKeyboardButton("⚡ Dynamic (All 9)", callback_data="strat_dynamic")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])


def coin_keyboard(enabled_coins: list = None):
    """Coin selection keyboard with tick/untick toggle."""
    if enabled_coins is None:
        from config import Config
        enabled_coins = Config.ENABLED_COINS

    all_coins = [
        ('BTC', '₿'),
        ('ETH', 'Ξ'),
        ('SOL', '◎'),
        ('XRP', '✕'),
    ]
    buttons = []
    for coin, icon in all_coins:
        tick = '✅' if coin in enabled_coins else '❌'
        buttons.append(InlineKeyboardButton(
            f"{tick} {icon} {coin}", callback_data=f"coin_{coin}"
        ))

    return InlineKeyboardMarkup([
        buttons[:2],
        buttons[2:],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])


def confirm_keyboard(action: str):
    """Confirmation keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{action}"),
            InlineKeyboardButton("❌ Cancel", callback_data="back_main"),
        ],
    ])


def settings_keyboard():
    """Settings keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📏 Position Size", callback_data="set_position_size")],
        [InlineKeyboardButton("🛡️ Risk Limits", callback_data="set_risk")],
        [InlineKeyboardButton("⏱️ Timeframes", callback_data="set_timeframes")],
        [InlineKeyboardButton("🪙 Coins", callback_data="set_coins")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])
