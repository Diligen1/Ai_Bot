"""Live Account Risk Guard — READ-ONLY risk math over the REAL Binance account.

Applies the exact same hard risk limits already used for paper/virtual
trading (see config/settings.py::RiskConfig, risk/risk_manager.py) to REAL
Binance futures equity, purely for display. This module NEVER places,
cancels, or modifies a real order, never changes leverage, never transfers
or withdraws funds — it only reads (via BinanceAccountConnector, itself
read-only) and computes numbers. No real order is ever placed by this
project at this stage.

VirtualPortfolio (paper capital) and the real Binance account are never
mixed here: every figure in LiveRiskSnapshot is derived exclusively from
BinanceAccountConnector's snapshot plus the shared Kill Switch flag — this
module never imports or reads VirtualPortfolio, paper_positions, or any
paper/virtual equity figure.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from binance.account_connector import BinanceAccountConnector, BinancePosition
from config.settings import DEFAULT_RISK_CONFIG, RiskConfig
from database.db import DatabaseManager


@dataclass(frozen=True)
class LiveRiskSnapshot:
    status: str  # DISABLED / CONNECTED / ERROR — mirrors BinanceAccountConnector
    real_equity: float = 0.0
    available_balance: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions_count: int = 0
    positions: list[BinancePosition] = field(default_factory=list)
    max_allowed_margin: float = 0.0
    risk_budget: float = 0.0
    current_exposure: float = 0.0
    kill_switch_on: bool = False
    leverage_limit: int = 0
    max_positions_limit: int = 0
    error_message: str | None = None


class LiveRiskGuard:
    def __init__(
        self,
        connector: BinanceAccountConnector,
        db: DatabaseManager,
        config: RiskConfig = DEFAULT_RISK_CONFIG,
    ) -> None:
        self._connector = connector
        self._db = db
        self.config = config

    def get_snapshot(self) -> LiveRiskSnapshot:
        account = self._connector.get_snapshot()
        # LIVE kill switch only — deliberately NOT get_kill_switch(), which is
        # the separate PAPER-trading kill switch (trading/paper_trading_engine.py).
        # Turning paper trading off must never be mistaken for stopping live risk,
        # and vice versa — see database/db.py get_live_kill_switch/set_live_kill_switch.
        kill_switch_on = self._db.get_live_kill_switch()

        if account.status != 'CONNECTED':
            return LiveRiskSnapshot(
                status=account.status,
                kill_switch_on=kill_switch_on,
                leverage_limit=self.config.leverage,
                max_positions_limit=self.config.max_open_positions,
                error_message=account.error_message,
            )

        max_allowed_margin = account.wallet_balance * self.config.max_margin_percent / 100
        risk_budget = account.wallet_balance * self.config.risk_per_trade_percent / 100
        current_exposure = sum(abs(position.position_amt) * position.entry_price for position in account.positions)

        return LiveRiskSnapshot(
            status='CONNECTED',
            real_equity=account.wallet_balance,
            available_balance=account.available_balance,
            unrealized_pnl=account.unrealized_pnl,
            open_positions_count=len(account.positions),
            positions=account.positions,
            max_allowed_margin=max_allowed_margin,
            risk_budget=risk_budget,
            current_exposure=current_exposure,
            kill_switch_on=kill_switch_on,
            leverage_limit=self.config.leverage,
            max_positions_limit=self.config.max_open_positions,
        )
