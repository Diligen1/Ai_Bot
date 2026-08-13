"""OrderIntent — the terminal, NEVER-SENT artifact of Live Execution Engine V1.

An OrderIntent describes what a real Binance Futures order WOULD look like:
built from an already-built TradeSetup, BinanceSymbolRules-correct
quantity/price, and REAL account risk state (risk/live_risk_guard.py).
Nothing in this project ever turns an OrderIntent into a real HTTP request —
see execution/live_execution_engine.py (DRY_RUN_EXECUTION) and
tests/test_live_execution_engine.py::test_dry_run_never_sends_order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUS_READY = 'READY'
STATUS_REJECTED = 'REJECTED'


@dataclass(frozen=True)
class OrderIntent:
    status: str
    symbol: str
    side: str
    position_side: str
    order_type: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    leverage: int
    reduce_only: bool
    setup_id: int | None
    risk_decision_snapshot: dict[str, Any]
    rejection_reasons: list[str] = field(default_factory=list)
