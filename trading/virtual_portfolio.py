"""Virtual (paper) portfolio state derived from SQLite paper trade history.

No real Binance balance is ever read here. Equity/realized-PnL/drawdown/loss
streak come from CLOSED trades (`trades` table, `source='paper'`, isolated
from seed/demo history — see database/db.py migration). Open-position count
and reserved margin come from `paper_positions`, PaperTradingEngine's own
live-state table (see trading/paper_trading_engine.py) — the `trades` table
never holds an 'open' row for a paper position, only its closed legs.

`current_equity` (trading/futures equity) only credits the futures-retained
share of each profitable trade; the rest was set aside into the
VirtualSpotVault (trading/virtual_spot_vault.py) and is reported separately
as `spot_vault_balance`, with `total_equity` as their sum. Losses always
reduce trading equity in full, never split.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import DEFAULT_RISK_CONFIG, RiskConfig
from database.db import DatabaseManager

PAPER_SOURCE = 'paper'


@dataclass(frozen=True)
class PortfolioState:
    starting_balance: float
    current_equity: float
    realized_pnl: float
    reserved_margin: float
    available_equity: float
    open_positions_count: int
    day_start_equity: float
    week_start_equity: float
    daily_realized_pnl: float
    weekly_realized_pnl: float
    daily_drawdown_percent: float
    weekly_drawdown_percent: float
    consecutive_losses: int
    spot_vault_balance: float
    total_equity: float


class VirtualPortfolio:
    """Computes virtual capital and risk-relevant state from paper trades only.

    Equity and drawdown figures are always recomputed fresh from the trades
    table rather than cached, so restarting the app simply re-derives the
    same state from the same persisted history.
    """

    def __init__(self, db: DatabaseManager, config: RiskConfig = DEFAULT_RISK_CONFIG) -> None:
        self.db = db
        self.config = config

    def _closed_paper_trades(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.get_trade_history(source=PAPER_SOURCE)]

    def _open_paper_positions(self) -> list[dict[str, Any]]:
        """Live paper_positions rows (PaperTradingEngine's source of truth for what's
        open) — NOT the `trades` table, which only ever holds closed legs for paper
        positions (see trading/paper_trading_engine.py)."""
        return [dict(row) for row in self.db.get_open_paper_positions()]

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _week_start(now: datetime) -> datetime:
        day_start = VirtualPortfolio._day_start(now)
        return day_start - timedelta(days=day_start.weekday())

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            normalized = value.replace('Z', '+00:00') if value.endswith('Z') else value
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @classmethod
    def _closed_before(cls, trade: dict[str, Any], boundary: datetime) -> bool:
        closed_at = cls._parse_timestamp(trade.get('closed_at'))
        return closed_at is not None and closed_at < boundary

    def _spot_vault_transfers_by_trade(self) -> dict[int, dict[str, Any]]:
        return {row['trade_id']: dict(row) for row in self.db.get_spot_vault_transfers()}

    @staticmethod
    def _trading_contribution(trade: dict[str, Any], transfers_by_trade: dict[int, dict[str, Any]]) -> float:
        """Only the futures-retained share of a profit counts toward trading
        equity — the rest went to the VirtualSpotVault (see
        trading/virtual_spot_vault.py). A loss counts in full: it is never
        split, so it reduces trading equity completely.

        Uses the ACTUAL amount recorded for this specific trade (frozen at the
        time it was processed), never the current Profit Split setting — so
        changing the setting later never reinterprets an already-closed trade.
        A profitable trade with no transfer record yet (not processed by the
        vault) counts in full, since nothing has actually been set aside."""
        net_pnl = trade.get('net_pnl') or 0.0
        if net_pnl <= 0:
            return net_pnl
        transfer = transfers_by_trade.get(trade.get('id'))
        return transfer['futures_amount'] if transfer is not None else net_pnl

    @staticmethod
    def _consecutive_losses(closed_trades: list[dict[str, Any]]) -> int:
        """Count the current losing streak from the most recently closed trade backwards.

        Breakeven trades (net_pnl == 0) are treated as neutral: they neither
        extend nor break the streak, so the scan simply continues past them
        to the next non-breakeven trade.
        """
        ordered = sorted(closed_trades, key=lambda t: t.get('closed_at') or '', reverse=True)
        streak = 0
        for trade in ordered:
            pnl = trade.get('net_pnl') or 0.0
            if pnl < 0:
                streak += 1
            elif pnl > 0:
                break
        return streak

    def get_state(self, now: datetime | None = None) -> PortfolioState:
        now = now or datetime.now(timezone.utc)
        starting_balance = self.config.initial_virtual_balance
        closed = self._closed_paper_trades()
        transfers_by_trade = self._spot_vault_transfers_by_trade()
        realized_pnl = sum(t.get('net_pnl') or 0.0 for t in closed)
        trading_pnl = sum(self._trading_contribution(t, transfers_by_trade) for t in closed)
        current_equity = starting_balance + trading_pnl

        day_start_dt = self._day_start(now)
        week_start_dt = self._week_start(now)
        trading_pnl_before_day = sum(
            self._trading_contribution(t, transfers_by_trade) for t in closed if self._closed_before(t, day_start_dt)
        )
        trading_pnl_before_week = sum(
            self._trading_contribution(t, transfers_by_trade) for t in closed if self._closed_before(t, week_start_dt)
        )
        day_start_equity = starting_balance + trading_pnl_before_day
        week_start_equity = starting_balance + trading_pnl_before_week
        daily_realized_pnl = current_equity - day_start_equity
        weekly_realized_pnl = current_equity - week_start_equity
        daily_drawdown_percent = (
            max(0.0, (day_start_equity - current_equity) / day_start_equity * 100) if day_start_equity > 0 else 0.0
        )
        weekly_drawdown_percent = (
            max(0.0, (week_start_equity - current_equity) / week_start_equity * 100) if week_start_equity > 0 else 0.0
        )

        open_positions = self._open_paper_positions()
        reserved_margin = sum(p.get('margin_used') or 0.0 for p in open_positions)
        open_positions_count = len(open_positions)
        available_equity = current_equity - reserved_margin
        spot_vault_balance = self.db.get_spot_vault_balance()

        return PortfolioState(
            starting_balance=starting_balance,
            current_equity=current_equity,
            realized_pnl=realized_pnl,
            reserved_margin=reserved_margin,
            available_equity=available_equity,
            open_positions_count=open_positions_count,
            day_start_equity=day_start_equity,
            week_start_equity=week_start_equity,
            daily_realized_pnl=daily_realized_pnl,
            weekly_realized_pnl=weekly_realized_pnl,
            daily_drawdown_percent=daily_drawdown_percent,
            weekly_drawdown_percent=weekly_drawdown_percent,
            consecutive_losses=self._consecutive_losses(closed),
            spot_vault_balance=spot_vault_balance,
            total_equity=current_equity + spot_vault_balance,
        )

    def open_symbols(self) -> set[str]:
        return {p['symbol'] for p in self._open_paper_positions()}

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self.open_symbols()

    def is_kill_switch_on(self) -> bool:
        return self.db.get_kill_switch()

    def set_kill_switch(self, value: bool) -> None:
        self.db.set_kill_switch(value)
