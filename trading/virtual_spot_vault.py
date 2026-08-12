"""Virtual Spot Vault V1 — profit-split savings for PAPER/VIRTUAL trading only.

Every profitable CLOSED paper trade (source='paper', net_pnl > 0) has a
configurable share of its net_pnl set aside into this vault instead of
staying in trading/futures equity; the rest stays in futures equity. Losing
and breakeven trades (net_pnl <= 0) never touch the vault — the full loss
reduces trading equity instead. Seed/demo trades (source != 'paper') are
never processed. No real Binance Spot account, no real transfer, ever.

Idempotency is enforced by the database (see database/db.py: `trade_id` is
UNIQUE on spot_vault_transfers), not just this class, so a given closed
trade can only ever be split once even across restarts or duplicate calls.

The spot/futures split percentage is read from persistent settings
(database/db.py: get_profit_split_settings/set_profit_split_settings) at the
moment each trade is processed, not from a fixed config value — changing the
setting via the Dashboard only affects trades split AFTER the change. Every
recorded split also freezes the percentages it used (`spot_percent_used`/
`futures_percent_used`), so a later setting change can never reinterpret an
already-processed trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from database.db import DatabaseManager

PAPER_SOURCE = 'paper'


@dataclass(frozen=True)
class ProfitSplitResult:
    trade_id: int
    symbol: str
    profit: float
    spot_amount: float
    futures_amount: float
    spot_percent_used: float
    futures_percent_used: float


class VirtualSpotVault:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def get_settings(self) -> tuple[float, float]:
        """Returns the current persistent (spot_percent, futures_percent)."""
        return self.db.get_profit_split_settings()

    def set_settings(self, spot_percent: float, futures_percent: float) -> None:
        """Persists new spot/futures percentages. Raises ValueError (Russian
        message) if spot_percent/futures_percent are negative or don't sum to
        100 — see database/db.py::set_profit_split_settings."""
        self.db.set_profit_split_settings(spot_percent, futures_percent)

    def process_closed_trade(self, trade: dict[str, Any], now: datetime | None = None) -> ProfitSplitResult | None:
        """Applies the profit split to one closed `trades` row, if eligible.

        Returns the applied split, or None when the trade isn't a profitable
        paper trade, or when it was already processed before (idempotent
        no-op — see the UNIQUE constraint in database/db.py)."""
        if trade.get('source') != PAPER_SOURCE:
            return None
        trade_id = trade.get('id')
        if trade_id is None:
            return None
        net_pnl = trade.get('net_pnl') or 0.0
        if net_pnl <= 0:
            return None

        spot_percent, futures_percent = self.get_settings()
        spot_amount = net_pnl * spot_percent / 100
        futures_amount = net_pnl - spot_amount
        symbol = trade.get('symbol', '')
        created_at = (now or datetime.now(timezone.utc)).isoformat()

        inserted_id = self.db.record_profit_split(
            trade_id=trade_id,
            symbol=symbol,
            profit=net_pnl,
            spot_amount=spot_amount,
            futures_amount=futures_amount,
            spot_percent_used=spot_percent,
            futures_percent_used=futures_percent,
            created_at=created_at,
        )
        if inserted_id is None:
            return None

        return ProfitSplitResult(
            trade_id=trade_id,
            symbol=symbol,
            profit=net_pnl,
            spot_amount=spot_amount,
            futures_amount=futures_amount,
            spot_percent_used=spot_percent,
            futures_percent_used=futures_percent,
        )

    def get_balance(self) -> float:
        return self.db.get_spot_vault_balance()

    def get_last_split(self) -> dict[str, Any] | None:
        row = self.db.get_last_profit_split()
        return dict(row) if row else None

    def get_split_for_trade(self, trade_id: int) -> dict[str, Any] | None:
        row = self.db.get_profit_split_for_trade(trade_id)
        return dict(row) if row else None
