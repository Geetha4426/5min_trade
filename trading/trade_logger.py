"""
Trade Logger — CSV log of every trade event for analysis.

Logs BUY, SELL, SETTLE events with full context:
  timestamp, event, coin, direction, timeframe, strategy, price,
  shares, cost_usd, pnl, fees, balance, mode, confidence, reason, order_type

File location: data/trades_log.csv (auto-created, appends)
Download via Telegram: /logs
"""

import csv
import os
import time
from datetime import datetime
from typing import Optional

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
LOG_FILE = os.path.join(LOG_DIR, 'trades_log.csv')

FIELDS = [
    'timestamp',       # ISO 8601
    'epoch',           # Unix timestamp (for sorting)
    'event',           # BUY | SELL | SETTLE | CANCEL
    'coin',            # BTC, ETH, SOL, XRP
    'direction',       # UP | DOWN | BOTH
    'timeframe',       # 5, 15, 30, 60
    'strategy',        # cross_tf_arb, spike_fade, etc.
    'order_type',      # FOK | GTC
    'entry_price',     # Price at entry
    'exit_price',      # Price at exit (sell/settle only)
    'shares',          # Number of shares
    'cost_usd',        # Total cost in USD
    'pnl_gross',       # Gross PnL before fees
    'pnl_net',         # Net PnL after fees
    'fees',            # Total fees (entry + exit)
    'pnl_pct',         # PnL as % of cost
    'balance_after',   # Balance after this event
    'mode',            # SEED, PLANT, CONCENTRATION, etc.
    'confidence',      # Strategy confidence score
    'reason',          # profit_take, stop_loss, sell_failed_settle, etc.
    'market_id',       # Polymarket market ID
    'trade_id',        # Internal trade ID
    'duration_secs',   # Seconds position was held (sell/settle only)
    'win_streak',      # Current consecutive wins
    'loss_streak',     # Current consecutive losses
    'size_multiplier', # Current sizing multiplier
]


class TradeLogger:
    """Append-only CSV trade logger."""

    def __init__(self, log_file: str = LOG_FILE):
        self.log_file = log_file
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self._ensure_header()

    def _ensure_header(self):
        """Write CSV header if file doesn't exist or is empty."""
        if not os.path.exists(self.log_file) or os.path.getsize(self.log_file) == 0:
            with open(self.log_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()

    def _write_row(self, row: dict):
        """Append a single row to the CSV."""
        with open(self.log_file, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
            writer.writerow(row)

    def log_buy(self, trade: dict, balance_after: float, mode: str,
                balance_mgr=None):
        """Log a BUY event when a position is opened."""
        now = datetime.utcnow()
        self._write_row({
            'timestamp': now.isoformat() + 'Z',
            'epoch': int(time.time()),
            'event': 'BUY',
            'coin': trade.get('coin', ''),
            'direction': trade.get('direction', ''),
            'timeframe': trade.get('timeframe', ''),
            'strategy': trade.get('strategy', ''),
            'order_type': trade.get('order_type', ''),
            'entry_price': f"{trade.get('entry_price', 0):.4f}",
            'exit_price': '',
            'shares': f"{trade.get('shares', 0):.2f}",
            'cost_usd': f"{trade.get('size_usd', 0):.2f}",
            'pnl_gross': '',
            'pnl_net': '',
            'fees': '',
            'pnl_pct': '',
            'balance_after': f"{balance_after:.2f}",
            'mode': mode,
            'confidence': f"{trade.get('confidence', 0):.3f}",
            'reason': '',
            'market_id': trade.get('market_id', ''),
            'trade_id': trade.get('id', ''),
            'duration_secs': '',
            'win_streak': str(getattr(balance_mgr, '_consecutive_wins', 0)) if balance_mgr else '',
            'loss_streak': str(getattr(balance_mgr, '_consecutive_losses', 0)) if balance_mgr else '',
            'size_multiplier': f"{getattr(balance_mgr, '_size_multiplier', 1.0):.2f}" if balance_mgr else '',
        })

    def log_sell(self, pos: dict, balance_after: float, mode: str,
                 balance_mgr=None):
        """Log a SELL/SETTLE event when a position is closed."""
        now = datetime.utcnow()
        reason = pos.get('exit_reason', '')
        event = 'SETTLE' if reason in ('sell_failed_settle', 'market_settled') else 'SELL'

        # Duration
        entry_ts = pos.get('placed_at', 0)
        duration = int(time.time() - entry_ts) if entry_ts else ''

        self._write_row({
            'timestamp': now.isoformat() + 'Z',
            'epoch': int(time.time()),
            'event': event,
            'coin': pos.get('coin', ''),
            'direction': pos.get('direction', ''),
            'timeframe': pos.get('timeframe', ''),
            'strategy': pos.get('strategy', ''),
            'order_type': pos.get('order_type', ''),
            'entry_price': f"{pos.get('entry_price', 0):.4f}",
            'exit_price': f"{pos.get('exit_price', 0):.4f}",
            'shares': f"{pos.get('shares', 0):.2f}",
            'cost_usd': f"{pos.get('size_usd', 0):.2f}",
            'pnl_gross': f"{pos.get('pnl_gross', 0):.4f}",
            'pnl_net': f"{pos.get('pnl', 0):.4f}",
            'fees': f"{pos.get('fees', 0):.4f}",
            'pnl_pct': f"{pos.get('pnl_pct', 0):.1f}",
            'balance_after': f"{balance_after:.2f}",
            'mode': mode,
            'confidence': f"{pos.get('confidence', 0):.3f}",
            'reason': reason,
            'market_id': pos.get('market_id', ''),
            'trade_id': pos.get('id', ''),
            'duration_secs': str(duration),
            'win_streak': str(getattr(balance_mgr, '_consecutive_wins', 0)) if balance_mgr else '',
            'loss_streak': str(getattr(balance_mgr, '_consecutive_losses', 0)) if balance_mgr else '',
            'size_multiplier': f"{getattr(balance_mgr, '_size_multiplier', 1.0):.2f}" if balance_mgr else '',
        })

    @property
    def log_path(self) -> str:
        return self.log_file

    @property
    def trade_count(self) -> int:
        """Count non-header lines in log."""
        if not os.path.exists(self.log_file):
            return 0
        with open(self.log_file, 'r', encoding='utf-8') as f:
            return max(0, sum(1 for _ in f) - 1)

    @property
    def file_size_kb(self) -> float:
        if not os.path.exists(self.log_file):
            return 0.0
        return os.path.getsize(self.log_file) / 1024
