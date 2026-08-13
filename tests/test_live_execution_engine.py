"""Tests for Live Execution Engine V1 — DRY RUN (execution/live_execution_engine.py).

All tests use fakes/mocks — no real network calls to Binance are ever made,
and no order is ever sent (DRY_RUN_EXECUTION is always True in V1; nothing
this engine holds is even capable of sending one — see the architectural
guard tests at the bottom of this file).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from binance.account_connector import BinanceAccountSnapshot, BinancePosition
from binance.symbol_rules import BinanceSymbolRules, SymbolRules
from database.db import DatabaseManager
from execution.live_execution_engine import DRY_RUN_EXECUTION, LiveExecutionEngine
from execution.order_intent import STATUS_READY, STATUS_REJECTED
from risk.live_risk_guard import LiveRiskGuard


def _manager(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / 'trading.db'))
    db.create_tables()
    return db


class FakeConnector:
    def __init__(self, snapshot: BinanceAccountSnapshot) -> None:
        self._snapshot = snapshot

    def get_snapshot(self) -> BinanceAccountSnapshot:
        return self._snapshot


class FakePublicClient:
    def __init__(self, exchange_info: dict[str, Any]) -> None:
        self._exchange_info = exchange_info

    def get_exchange_info(self) -> dict[str, Any]:
        return self._exchange_info


class FakeMarketService:
    def __init__(self, fresh_symbols: set[str] | None = None) -> None:
        self._fresh = fresh_symbols if fresh_symbols is not None else {'BTCUSDT'}

    def is_data_fresh(self, symbol: str) -> bool:
        return symbol in self._fresh


BTCUSDT_EXCHANGE_ENTRY = {
    'symbol': 'BTCUSDT',
    'status': 'TRADING',
    'filters': [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001'},
        {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
    ],
}


def _btc_symbol_rules(min_qty: str = '0.001') -> BinanceSymbolRules:
    entry = dict(BTCUSDT_EXCHANGE_ENTRY)
    entry['filters'] = [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': min_qty},
        {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
    ]
    return BinanceSymbolRules(client=FakePublicClient({'symbols': [entry]}))


def _connected_snapshot(wallet_balance: float = 1000.0, positions: list[BinancePosition] | None = None) -> BinanceAccountSnapshot:
    return BinanceAccountSnapshot(
        status='CONNECTED', wallet_balance=wallet_balance, available_balance=wallet_balance,
        unrealized_pnl=0.0, can_trade=True, positions=positions or [],
    )


def _ready_setup(**overrides: Any) -> dict[str, Any]:
    base = {
        'symbol': 'BTCUSDT', 'direction': 'LONG', 'setup_type': 'TREND_PULLBACK',
        'entry_zone_low': 49500.0, 'entry_zone_high': 50000.0, 'stop_loss': 49000.0,
        'take_profit_1': 51000.0, 'take_profit_2': 52000.0, 'status': 'READY', 'id': 7,
    }
    base.update(overrides)
    return base


def _build_engine(
    tmp_path: Path,
    snapshot: BinanceAccountSnapshot,
    whitelist: tuple[str, ...] = ('BTCUSDT',),
    kill_switch: bool = False,
    fresh_symbols: set[str] | None = None,
    min_qty: str = '0.001',
) -> tuple[LiveExecutionEngine, DatabaseManager]:
    db = _manager(tmp_path)
    for symbol in whitelist:
        db.add_whitelist_symbol(symbol)
    if kill_switch:
        db.set_live_kill_switch(True)  # LIVE kill switch — LiveRiskGuard never reads the paper one
    connector = FakeConnector(snapshot)
    guard = LiveRiskGuard(connector, db)
    rules = _btc_symbol_rules(min_qty=min_qty)
    market_service = FakeMarketService(fresh_symbols)
    engine = LiveExecutionEngine(guard, rules, db, market_service)
    return engine, db


# -- happy path: READY OrderIntent -----------------------------------------

def test_ready_order_intent_is_correctly_sized_and_rounded(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_READY
    assert intent.rejection_reasons == []
    assert intent.symbol == 'BTCUSDT'
    assert intent.side == 'BUY'
    assert intent.position_side == 'LONG'
    assert intent.leverage == 5
    assert intent.setup_id == 7
    # quantity must be an exact multiple of stepSize (0.001) — proves real
    # rounding happened, not raw sizer output.
    assert (Decimal(str(intent.quantity)) % Decimal('0.001')) == 0
    assert intent.quantity > 0
    # entry/stop must be exact multiples of tickSize (0.10).
    assert (Decimal(str(intent.entry_price)) * 10) % 1 == 0
    assert intent.risk_decision_snapshot['margin_required'] <= intent.risk_decision_snapshot['max_margin'] + 1e-9
    assert intent.risk_decision_snapshot['planned_loss'] <= intent.risk_decision_snapshot['risk_budget'] + 1e-9


def test_get_last_intent_reflects_most_recent_build(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))
    assert engine.get_last_intent() is None

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert engine.get_last_intent() is intent


# -- min quantity ------------------------------------------------------------

def test_min_qty_rejects_when_below_minimum(tmp_path: Path) -> None:
    # min_qty deliberately far above what correct sizing would ever produce.
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0), min_qty='1.0')

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_REJECTED
    assert 'QUANTITY_BELOW_MIN_QTY' in intent.rejection_reasons


# -- invalid / non-whitelisted symbol ---------------------------------------

def test_symbol_not_in_whitelist_is_rejected(tmp_path: Path) -> None:
    # A fresh DB auto-seeds a default whitelist (BTCUSDT/ETHUSDT/SOLUSDT/
    # BNBUSDT/ZECUSDT — see database/db.py::_initialize_default_whitelist),
    # so use a symbol that's genuinely absent from it.
    engine, db = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0), whitelist=())
    assert 'DOGEUSDT' not in {row['symbol'] for row in db.get_whitelist_symbols()}

    intent = engine.build_order_intent('DOGEUSDT', _ready_setup(symbol='DOGEUSDT'))

    assert intent.status == STATUS_REJECTED
    assert 'SYMBOL_NOT_WHITELISTED' in intent.rejection_reasons


def test_symbol_missing_from_exchange_info_is_rejected(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    db.add_whitelist_symbol('DOGEUSDT')
    connector = FakeConnector(_connected_snapshot(wallet_balance=1000.0))
    guard = LiveRiskGuard(connector, db)
    rules = BinanceSymbolRules(client=FakePublicClient({'symbols': [BTCUSDT_EXCHANGE_ENTRY]}))  # no DOGEUSDT entry
    engine = LiveExecutionEngine(guard, rules, db, FakeMarketService({'DOGEUSDT'}))

    intent = engine.build_order_intent('DOGEUSDT', _ready_setup(symbol='DOGEUSDT'))

    assert intent.status == STATUS_REJECTED
    assert 'SYMBOL_RULES_UNAVAILABLE' in intent.rejection_reasons


# -- duplicate live position / max positions --------------------------------

def test_duplicate_live_position_is_rejected(tmp_path: Path) -> None:
    existing = BinancePosition(symbol='BTCUSDT', position_amt=0.01, entry_price=49000.0, unrealized_pnl=1.0, leverage=5, position_side='LONG')
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0, positions=[existing]))

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_REJECTED
    assert 'DUPLICATE_LIVE_POSITION' in intent.rejection_reasons


def test_max_open_positions_is_rejected(tmp_path: Path) -> None:
    positions = [
        BinancePosition(symbol='ETHUSDT', position_amt=1.0, entry_price=3000.0, unrealized_pnl=0.0, leverage=5, position_side='LONG'),
        BinancePosition(symbol='SOLUSDT', position_amt=10.0, entry_price=100.0, unrealized_pnl=0.0, leverage=5, position_side='LONG'),
        BinancePosition(symbol='BNBUSDT', position_amt=2.0, entry_price=400.0, unrealized_pnl=0.0, leverage=5, position_side='LONG'),
    ]
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0, positions=positions))

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_REJECTED
    assert 'MAX_OPEN_POSITIONS' in intent.rejection_reasons


# -- kill switch / not connected / stale data / setup not ready -------------

def test_kill_switch_on_is_rejected(tmp_path: Path) -> None:
    """The LIVE kill switch — not the separate paper-trading one — is what
    OrderIntent's KILL_SWITCH_ON reason must reflect (final-audit fix #1)."""
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0), kill_switch=True)

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_REJECTED
    assert 'KILL_SWITCH_ON' in intent.rejection_reasons


def test_paper_kill_switch_does_not_reject_order_intent(tmp_path: Path) -> None:
    """Regression: turning ONLY the paper-trading kill switch on must never
    produce a KILL_SWITCH_ON rejection here."""
    engine, db = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))
    db.set_kill_switch(True)
    assert db.get_live_kill_switch() is False

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert 'KILL_SWITCH_ON' not in intent.rejection_reasons


def test_binance_not_connected_is_rejected(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, BinanceAccountSnapshot(status='DISABLED'))

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_REJECTED
    assert 'BINANCE_NOT_CONNECTED' in intent.rejection_reasons


def test_stale_market_data_is_rejected(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0), fresh_symbols=set())

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_REJECTED
    assert 'STALE_MARKET_DATA' in intent.rejection_reasons


def test_setup_not_ready_is_rejected(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))

    intent = engine.build_order_intent('BTCUSDT', _ready_setup(status='WAITING'))

    assert intent.status == STATUS_REJECTED
    assert 'SETUP_NOT_READY' in intent.rejection_reasons


# -- risk-budget / margin invariant re-check (defense in depth) -------------
#
# PositionSizeCalculator already sizes quantity so margin/planned-loss never
# exceed their caps by construction (same as everywhere else in this
# project), and rounding only ever floors quantity further down — so these
# two invariant checks cannot be triggered by normal sizing output. To prove
# the guard itself actually works, these tests inject a deliberately wrong
# sizer, mirroring the paper-trading engine's ApprovingFixedQuantityRiskManager
# defense-in-depth tests.

class _FixedQuantitySizer:
    def __init__(self, quantity: float) -> None:
        self._quantity = quantity

    def calculate(self, entry_price: float, stop_loss: float, current_equity: float) -> dict[str, float]:
        return {'quantity': self._quantity}


def test_margin_exceeds_limit_is_rejected(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))
    engine._sizer = _FixedQuantitySizer(0.05)  # notional 0.05*50000=2500 -> margin 500 >> max_margin 100

    setup = _ready_setup(stop_loss=49999.9)  # tiny stop distance keeps planned_loss small
    intent = engine.build_order_intent('BTCUSDT', setup)

    assert intent.status == STATUS_REJECTED
    assert 'MARGIN_EXCEEDS_LIMIT' in intent.rejection_reasons
    assert 'PLANNED_LOSS_EXCEEDS_BUDGET' not in intent.rejection_reasons


def test_planned_loss_exceeds_risk_budget_is_rejected(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))
    engine._sizer = _FixedQuantitySizer(0.005)  # notional 250 -> margin 50 (within cap 100)

    setup = _ready_setup(stop_loss=45000.0)  # wide 10% stop -> big loss_rate_percent
    intent = engine.build_order_intent('BTCUSDT', setup)

    assert intent.status == STATUS_REJECTED
    assert 'PLANNED_LOSS_EXCEEDS_BUDGET' in intent.rejection_reasons
    assert 'MARGIN_EXCEEDS_LIMIT' not in intent.rejection_reasons


# -- DRY RUN: never sends a real order ---------------------------------------

def test_dry_run_flag_is_true() -> None:
    assert DRY_RUN_EXECUTION is True


def test_dry_run_never_sends_order(tmp_path: Path, capsys: Any) -> None:
    engine, _ = _build_engine(tmp_path, _connected_snapshot(wallet_balance=1000.0))

    intent = engine.build_order_intent('BTCUSDT', _ready_setup())

    assert intent.status == STATUS_READY
    captured = capsys.readouterr()
    assert 'ORDER INTENT READY' in captured.out
    assert 'НЕ отправлен' in captured.out


FORBIDDEN_METHOD_NAMES = (
    'create_order', 'place_order', 'cancel_order', 'new_order', 'close_order',
    'transfer', 'withdraw', 'change_leverage', 'set_leverage',
    'close_position', 'modify_position', 'change_margin_type', 'send_order', 'submit_order',
)


def test_engine_has_no_order_sending_methods() -> None:
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(LiveExecutionEngine, name), f'LiveExecutionEngine must not implement {name}()'


def test_order_intent_dataclass_has_no_order_sending_methods() -> None:
    from execution.order_intent import OrderIntent
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(OrderIntent, name), f'OrderIntent must not implement {name}()'


def test_symbol_rules_has_no_order_sending_methods() -> None:
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(BinanceSymbolRules, name), f'BinanceSymbolRules must not implement {name}()'
