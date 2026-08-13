"""Tests for Live Execution Engine V2 (execution/live_trading_engine.py).

Every test uses a FakeTradingClient/FakeAccountClient — no real network
call is ever made, and NOTHING here ever sends a real order (that's the
entire point of these tests: proving the gates work). LIVE_TRADING_ENABLED
is left unset (=> False) unless a specific test explicitly monkeypatches it
to 'true' to exercise the gated code paths — it is NEVER set to enable
anything beyond the scope of an individual test, and no test here ever
leaves it enabled for a real run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from binance.account_connector import BinanceAccountSnapshot, BinancePosition
from binance.symbol_rules import BinanceSymbolRules
from binance.trading_client import LiveTradingAPIError, LiveTradingError, LiveTradingTimeout
from database.db import DatabaseManager
from execution.live_execution_engine import LiveExecutionEngine
from execution.live_trading_engine import (
    LiveTradingEngine,
    STATUS_ALREADY_EXECUTED,
    STATUS_BLOCKED,
    STATUS_CRITICAL_UNPROTECTED_POSITION,
    STATUS_ENTRY_FAILED,
    STATUS_ENTRY_TIMEOUT_UNCONFIRMED,
    STATUS_PROTECTED,
)
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


class FakeAccountClientForLeverage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.call_count = 0
        # 1-indexed call numbers that raise instead of returning `rows` —
        # used to simulate "leverage check (call 1) succeeds, but the LATER
        # position check (during reconciliation, call 2) is inconclusive"
        # within a single attempt_entry() invocation.
        self.fail_on_calls: set[int] = set()

    def get_position_risk(self) -> list[dict[str, Any]]:
        self.call_count += 1
        if self.call_count in self.fail_on_calls:
            raise RuntimeError('simulated network failure')
        return self.rows


class FakeTradingClient:
    def __init__(self) -> None:
        self.position_mode: dict[str, Any] | Exception = {'dualSidePosition': False}
        self.open_position_responses: list[Any] = [{'orderId': 1001, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}]
        self.open_position_calls: list[dict[str, Any]] = []
        self.stop_loss_responses: list[Any] = [{'orderId': 2001, 'status': 'NEW'}]
        self.stop_loss_calls: list[dict[str, Any]] = []
        self.take_profit_responses: list[Any] = [{'orderId': 3001, 'status': 'NEW'}]
        self.take_profit_calls: list[dict[str, Any]] = []
        self.order_status_response: Any = None
        self.order_status_calls: list[tuple[Any, ...]] = []
        self.cancel_calls: list[tuple[Any, ...]] = []

    def get_position_mode(self) -> dict[str, Any]:
        if isinstance(self.position_mode, Exception):
            raise self.position_mode
        return self.position_mode

    def open_position(self, **kwargs: Any) -> dict[str, Any]:
        self.open_position_calls.append(kwargs)
        return self._consume(self.open_position_responses)

    def place_stop_loss(self, **kwargs: Any) -> dict[str, Any]:
        self.stop_loss_calls.append(kwargs)
        return self._consume(self.stop_loss_responses)

    def place_take_profit(self, **kwargs: Any) -> dict[str, Any]:
        self.take_profit_calls.append(kwargs)
        return self._consume(self.take_profit_responses)

    def get_order_status(self, symbol: str, order_id: Any = None, client_order_id: Any = None) -> dict[str, Any]:
        self.order_status_calls.append((symbol, order_id, client_order_id))
        if isinstance(self.order_status_response, Exception):
            raise self.order_status_response
        if self.order_status_response is None:
            raise LiveTradingAPIError('no order found')
        return self.order_status_response

    def cancel_order(self, symbol: str, order_id: Any = None, client_order_id: Any = None) -> dict[str, Any]:
        self.cancel_calls.append((symbol, order_id, client_order_id))
        return {'status': 'CANCELED'}

    @staticmethod
    def _consume(queue: list[Any]) -> Any:
        if not queue:
            raise AssertionError('no more fake responses queued')
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


BTCUSDT_EXCHANGE_ENTRY = {
    'symbol': 'BTCUSDT',
    'status': 'TRADING',
    'filters': [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001'},
        {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
    ],
}


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


class _FixedQuantitySizer:
    def __init__(self, quantity: float) -> None:
        self._quantity = quantity

    def calculate(self, entry_price: float, stop_loss: float, current_equity: float) -> dict[str, float]:
        return {'quantity': self._quantity}


def _build_v2_engine(
    tmp_path: Path,
    wallet_balance: float = 1000.0,
    binance_positions: list[BinancePosition] | None = None,
    leverage_rows: list[dict[str, Any]] | None = None,
    position_mode_dual: bool | Exception = False,
    whitelist: tuple[str, ...] = ('BTCUSDT',),
    live_kill_switch: bool = False,
    fresh_symbols: set[str] | None = None,
    min_qty: str = '0.001',
) -> tuple[LiveTradingEngine, DatabaseManager, FakeTradingClient, LiveExecutionEngine]:
    db = _manager(tmp_path)
    for symbol in whitelist:
        db.add_whitelist_symbol(symbol)
    if live_kill_switch:
        db.set_live_kill_switch(True)

    connector = FakeConnector(_connected_snapshot(wallet_balance=wallet_balance, positions=binance_positions or []))
    guard = LiveRiskGuard(connector, db)

    entry = dict(BTCUSDT_EXCHANGE_ENTRY)
    entry['filters'] = [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': min_qty},
        {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
    ]
    symbol_rules = BinanceSymbolRules(client=FakePublicClient({'symbols': [entry]}))
    market_service = FakeMarketService(fresh_symbols)
    exec_engine = LiveExecutionEngine(guard, symbol_rules, db, market_service)

    trading_client = FakeTradingClient()
    if isinstance(position_mode_dual, Exception):
        trading_client.position_mode = position_mode_dual
    else:
        trading_client.position_mode = {'dualSidePosition': position_mode_dual}

    default_leverage_rows = leverage_rows if leverage_rows is not None else [{'symbol': symbol, 'leverage': '5'} for symbol in whitelist]
    account_client = FakeAccountClientForLeverage(default_leverage_rows)

    engine = LiveTradingEngine(trading_client, account_client, guard, exec_engine, db)
    return engine, db, trading_client, exec_engine


# -- disabled by default ------------------------------------------------

def test_disabled_by_default_order_endpoint_never_called(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv('LIVE_TRADING_ENABLED', raising=False)
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'LIVE_TRADING_DISABLED' in result.reasons
    assert trading_client.open_position_calls == []


def test_live_trading_enabled_defaults_false_helper() -> None:
    assert LiveTradingEngine.is_live_trading_enabled() is False


# -- kill switch ----------------------------------------------------------

def test_live_kill_switch_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, live_kill_switch=True)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'LIVE_KILL_SWITCH_ON' in result.reasons
    assert trading_client.open_position_calls == []


def test_live_kill_switch_is_separate_from_paper_kill_switch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    db = _manager(tmp_path)
    db.set_kill_switch(True)  # paper trading kill switch
    assert db.get_live_kill_switch() is False  # must be unaffected


# -- invalid risk / margin -------------------------------------------------

def test_invalid_risk_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    exec_engine._sizer = _FixedQuantitySizer(0.005)  # notional 250, margin 50 (ok), wide stop -> loss too big

    result = engine.attempt_entry('BTCUSDT', _ready_setup(stop_loss=45000.0))

    assert result.status == STATUS_BLOCKED
    assert any('PLANNED_LOSS' in reason for reason in result.reasons)
    assert trading_client.open_position_calls == []


def test_margin_above_10_percent_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    exec_engine._sizer = _FixedQuantitySizer(0.05)  # notional 2500, margin 500 >> cap 100

    result = engine.attempt_entry('BTCUSDT', _ready_setup(stop_loss=49999.9))

    assert result.status == STATUS_BLOCKED
    assert any('MARGIN' in reason for reason in result.reasons)
    assert trading_client.open_position_calls == []


# -- leverage ---------------------------------------------------------------

def test_leverage_above_5_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, leverage_rows=[{'symbol': 'BTCUSDT', 'leverage': '10'}])

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'LEVERAGE_EXCEEDS_LIMIT' in result.reasons
    assert trading_client.open_position_calls == []


def test_unknown_leverage_blocks_entry_fail_safe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, leverage_rows=[])  # no row for BTCUSDT at all

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'LEVERAGE_UNKNOWN' in result.reasons
    assert trading_client.open_position_calls == []


# -- duplicate symbol / max positions ----------------------------------------

def test_duplicate_symbol_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    existing = BinancePosition(symbol='BTCUSDT', position_amt=0.01, entry_price=49000.0, unrealized_pnl=1.0, leverage=5, position_side='LONG')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, binance_positions=[existing])

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'DUPLICATE_LIVE_POSITION' in result.reasons
    assert trading_client.open_position_calls == []


def test_max_3_open_positions_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    positions = [
        BinancePosition(symbol='ETHUSDT', position_amt=1.0, entry_price=100.0, unrealized_pnl=0.0, leverage=5, position_side='LONG'),
        BinancePosition(symbol='SOLUSDT', position_amt=1.0, entry_price=100.0, unrealized_pnl=0.0, leverage=5, position_side='LONG'),
        BinancePosition(symbol='BNBUSDT', position_amt=1.0, entry_price=100.0, unrealized_pnl=0.0, leverage=5, position_side='LONG'),
    ]
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, binance_positions=positions)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'MAX_OPEN_POSITIONS' in result.reasons
    assert trading_client.open_position_calls == []


# -- position mode: ONE-WAY / HEDGE -----------------------------------------

def test_unknown_position_mode_blocks_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, position_mode_dual=LiveTradingError('boom'))

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'UNKNOWN_POSITION_MODE' in result.reasons
    assert trading_client.open_position_calls == []


def test_one_way_mode_uses_both_position_side_and_reduce_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, position_mode_dual=False)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    assert trading_client.open_position_calls[0]['position_side'] == 'BOTH'
    assert trading_client.stop_loss_calls[0]['position_side'] == 'BOTH'
    assert trading_client.stop_loss_calls[0]['reduce_only'] is True
    assert trading_client.take_profit_calls[0]['reduce_only'] is True


def test_hedge_mode_uses_long_short_position_side_no_reduce_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, position_mode_dual=True)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    assert trading_client.open_position_calls[0]['position_side'] == 'LONG'
    assert trading_client.stop_loss_calls[0]['position_side'] == 'LONG'
    assert trading_client.stop_loss_calls[0]['reduce_only'] is None  # never sent in Hedge Mode


# -- quantity rounding --------------------------------------------------

def test_entry_quantity_is_step_size_rounded(tmp_path: Path, monkeypatch) -> None:
    from decimal import Decimal

    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    sent_quantity = trading_client.open_position_calls[0]['quantity']
    assert (Decimal(str(sent_quantity)) % Decimal('0.001')) == 0
    assert sent_quantity > 0


# -- successful entry / SL / TP1 / TP2 ---------------------------------------

def test_entry_success_places_sl_and_tp1_tp2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    assert result.entry_order_id == '1001'
    assert result.sl_order_id == '2001'
    assert result.tp1_order_id == '3001'
    assert result.tp2_order_id == '3001'  # same fake response reused for both TP calls
    assert len(trading_client.stop_loss_calls) == 1
    assert trading_client.stop_loss_calls[0]['stop_price'] == 49000.0
    assert len(trading_client.take_profit_calls) == 2
    assert trading_client.take_profit_calls[0]['stop_price'] == 51000.0
    assert trading_client.take_profit_calls[1]['stop_price'] == 52000.0
    # TP1 is half the filled quantity, TP2 is the full filled quantity.
    assert trading_client.take_profit_calls[0]['quantity'] == pytest.approx(0.009 * 0.5)
    assert trading_client.take_profit_calls[1]['quantity'] == pytest.approx(0.009)

    log = dict(db.get_live_execution_log(result.execution_id))
    assert log['status'] == STATUS_PROTECTED
    assert log['setup_id'] == 7
    assert log['entry_order_id'] == '1001'
    assert log['sl_order_id'] == '2001'
    assert log['client_order_id'] == 'bot-7-entry'


def test_entry_not_confirmed_when_response_has_no_fill(tmp_path: Path, monkeypatch) -> None:
    """status=NEW/executedQty=0 is AMBIGUOUS (the order may still fill) —
    never assumed failed, never assumed safe. Reconciliation is inconclusive
    here (the fake order-status lookup fails), so it stays ambiguous."""
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [{'orderId': 9, 'status': 'NEW', 'executedQty': '0'}]

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert trading_client.stop_loss_calls == []  # never protects a position that was never confirmed filled


def test_http_200_alone_does_not_mean_filled(tmp_path: Path, monkeypatch) -> None:
    """A 200 response with status=REJECTED and executedQty=0 must NOT be
    treated as an open position — this is the literal requirement-12
    scenario. REJECTED is a definitive terminal status, so this is a
    confirmed (retryable) failure, not merely ambiguous."""
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [{'orderId': 9, 'status': 'REJECTED', 'executedQty': '0'}]

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_ENTRY_FAILED
    assert trading_client.stop_loss_calls == []


# -- CRITICAL: SL failure after confirmed entry ------------------------------

def test_sl_failure_after_confirmed_entry_is_critical_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.stop_loss_responses = [LiveTradingAPIError('margin insufficient')]

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_CRITICAL_UNPROTECTED_POSITION
    assert result.entry_order_id == '1001'
    assert result.error_message is not None
    # No TP was ever attempted after a failed SL — no improvised recovery.
    assert trading_client.take_profit_calls == []

    log = dict(db.get_live_execution_log(result.execution_id))
    assert log['status'] == STATUS_CRITICAL_UNPROTECTED_POSITION
    assert log['entry_order_id'] == '1001'
    assert 'margin insufficient' in (log['error_message'] or '')


def test_tp_failure_does_not_prevent_sl_protection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.take_profit_responses = [LiveTradingAPIError('rate limited')]

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED  # SL already in place -> position IS protected
    assert result.sl_order_id == '2001'
    assert result.tp1_order_id is None
    assert result.tp2_order_id is None


# -- entry timeout with idempotency ------------------------------------------

def test_entry_timeout_reconciles_via_order_status_no_duplicate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('client timed out')]
    trading_client.order_status_response = {'orderId': 5555, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED  # reconciled: the order DID reach Binance
    assert result.entry_order_id == '5555'
    assert len(trading_client.open_position_calls) == 1  # never retried with a second real order
    assert trading_client.order_status_calls  # reconciliation actually happened


def test_duplicate_execution_prevention_on_second_attempt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert first.status == STATUS_PROTECTED
    assert second.status == STATUS_ALREADY_EXECUTED
    assert len(trading_client.open_position_calls) == 1  # the second call never touched Binance again


def test_entry_timeout_then_retry_also_never_duplicates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('client timed out')]
    trading_client.order_status_response = {'orderId': 5555, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    second = engine.attempt_entry('BTCUSDT', _ready_setup())  # caller retries after the timeout

    assert first.status == STATUS_PROTECTED
    assert second.status == STATUS_ALREADY_EXECUTED
    assert len(trading_client.open_position_calls) == 1


# -- cancel protective orders (bot's own orders only) ------------------------

def test_cancel_protective_orders_only_cancels_logged_order_ids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    result = engine.attempt_entry('BTCUSDT', _ready_setup())
    assert result.status == STATUS_PROTECTED

    cancelled = engine.cancel_protective_orders(result.execution_id)

    assert set(cancelled) == {'2001', '3001'}
    for symbol, order_id, _client_order_id in trading_client.cancel_calls:
        assert symbol == 'BTCUSDT'
        assert order_id in {'2001', '3001'}


# -- final-audit regression tests (idempotency state machine) ---------------
# Requirement-5 checklist from the follow-up fix request, in order.

def test_1_paper_kill_switch_does_not_block_live_when_live_switch_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    db.set_kill_switch(True)  # PAPER switch only

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    assert 'LIVE_KILL_SWITCH_ON' not in result.reasons


def test_2_live_kill_switch_blocks_live(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path, live_kill_switch=True)

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_BLOCKED
    assert 'LIVE_KILL_SWITCH_ON' in result.reasons
    assert trading_client.open_position_calls == []


def test_3_entry_failed_allows_retry_after_full_risk_check(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingAPIError('insufficient margin')]

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    assert first.status == STATUS_ENTRY_FAILED

    trading_client.open_position_responses = [{'orderId': 1001, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}]
    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert second.status == STATUS_PROTECTED
    assert len(trading_client.open_position_calls) == 2  # the failed attempt + the retry
    # Still exactly one execution row for this client_order_id — reused, not duplicated.
    log = dict(db.get_live_execution_log_by_client_order_id('bot-7-entry'))
    assert log['status'] == STATUS_PROTECTED


def test_4_confirmed_entry_forbids_duplicate_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert first.status == STATUS_PROTECTED
    assert second.status == STATUS_ALREADY_EXECUTED
    assert len(trading_client.open_position_calls) == 1


def test_5_timeout_with_order_still_new_sends_no_new_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    trading_client.order_status_response = {'orderId': 42, 'status': 'NEW', 'executedQty': '0'}

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert len(trading_client.open_position_calls) == 1
    assert trading_client.order_status_calls  # reconciliation was actually attempted


def test_6_ambiguous_reconciliation_repeats_on_next_call_never_duplicating(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    trading_client.order_status_response = {'orderId': 42, 'status': 'NEW', 'executedQty': '0'}

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    calls_after_first = len(trading_client.order_status_calls)
    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert first.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert second.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert len(trading_client.order_status_calls) > calls_after_first  # reconciliation ran again
    assert len(trading_client.open_position_calls) == 1  # never duplicated


def test_7_timeout_then_filled_position_is_discovered_and_protected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    trading_client.order_status_response = {'orderId': 5555, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    assert result.entry_order_id == '5555'
    assert len(trading_client.open_position_calls) == 1
    assert trading_client.stop_loss_calls  # SL was placed for the discovered position


def test_7b_order_not_found_but_real_position_exists_is_discovered(tmp_path: Path, monkeypatch) -> None:
    """Distinct from 7: here the ORDER itself is confirmed gone (-2013), but
    the ACTUAL Binance position (checked separately) shows it filled anyway."""
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    trading_client.order_status_response = LiveTradingAPIError('Binance error -2013: Order does not exist.', code=-2013)
    engine.account_client.rows = [{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0.009', 'positionSide': 'BOTH'}]

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_PROTECTED
    assert len(trading_client.open_position_calls) == 1  # never sent a duplicate entry order
    assert trading_client.stop_loss_calls


def test_8_filled_position_without_sl_is_critical_unprotected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.stop_loss_responses = [LiveTradingAPIError('margin insufficient')]

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_CRITICAL_UNPROTECTED_POSITION
    assert engine.has_unprotected_position() is True


def test_9_new_entries_blocked_while_unprotected_position_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.stop_loss_responses = [LiveTradingAPIError('temporary issue')]
    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    assert first.status == STATUS_CRITICAL_UNPROTECTED_POSITION

    # Recovery still fails this time too -> stays critical -> blocks ALL new entries.
    trading_client.stop_loss_responses = [LiveTradingAPIError('still broken')]
    engine.account_client.rows = [{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0.009', 'positionSide': 'BOTH'}]

    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert second.status == STATUS_BLOCKED
    assert 'CRITICAL_UNPROTECTED_POSITION_ACTIVE' in second.reasons
    assert len(trading_client.open_position_calls) == 1  # only the original attempt ever reached Binance


def test_10_recovering_sl_clears_critical_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.stop_loss_responses = [LiveTradingAPIError('temporary issue')]

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    assert first.status == STATUS_CRITICAL_UNPROTECTED_POSITION
    assert engine.has_unprotected_position() is True

    # The position is still open, and this time SL placement succeeds.
    engine.account_client.rows = [{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0.009', 'positionSide': 'BOTH'}]
    trading_client.stop_loss_responses = [{'orderId': 8888, 'status': 'NEW'}]

    still_unprotected = engine._resolve_unprotected_positions()

    assert still_unprotected is False
    assert engine.has_unprotected_position() is False
    log = dict(db.get_live_execution_log(first.execution_id))
    assert log['status'] == STATUS_PROTECTED
    assert log['sl_order_id'] == '8888'


def test_11_cancelled_order_with_no_position_allows_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    trading_client.order_status_response = {'orderId': 42, 'status': 'CANCELED', 'executedQty': '0'}
    engine.account_client.rows = [{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0', 'positionSide': 'BOTH'}]

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    assert first.status == STATUS_ENTRY_FAILED

    trading_client.open_position_responses = [{'orderId': 1001, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}]
    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert second.status == STATUS_PROTECTED
    assert len(trading_client.open_position_calls) == 2


def test_12_unknown_order_and_unknown_position_never_duplicates_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    # Order-status lookup itself is inconclusive (not a confirmed -2013) —
    # by itself this is already enough to stay ambiguous, with no need to
    # even reach a (also inconclusive) position check.
    trading_client.order_status_response = LiveTradingAPIError('temporary network error')

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    second = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert first.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert second.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert len(trading_client.open_position_calls) == 1  # never duplicated despite being retried


def test_12b_order_not_found_and_position_check_inconclusive_stays_ambiguous(tmp_path: Path, monkeypatch) -> None:
    """A stricter variant of 12: the order is CONFIRMED gone (-2013), but the
    position cross-check itself can't be completed either — must still stay
    ambiguous rather than assuming "no execution happened"."""
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.open_position_responses = [LiveTradingTimeout('timed out')]
    trading_client.order_status_response = LiveTradingAPIError('Binance error -2013: Order does not exist.', code=-2013)
    # Call 1 (leverage check) succeeds; call 2 (position check during
    # reconciliation) is inconclusive.
    engine.account_client.fail_on_calls = {2}

    result = engine.attempt_entry('BTCUSDT', _ready_setup())

    assert result.status == STATUS_ENTRY_TIMEOUT_UNCONFIRMED
    assert len(trading_client.open_position_calls) == 1  # exactly one attempt ever made


def test_13_kill_switch_does_not_block_recovery_but_still_blocks_new_entries(tmp_path: Path, monkeypatch) -> None:
    """The Live Kill Switch must block NEW entries only — it must never
    prevent recovering an already-open unprotected position (see
    attempt_entry()'s gate order: CRITICAL resolution now runs BEFORE the
    kill-switch check, on purpose)."""
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    engine, db, trading_client, exec_engine = _build_v2_engine(tmp_path)
    trading_client.stop_loss_responses = [LiveTradingAPIError('temporary issue')]

    first = engine.attempt_entry('BTCUSDT', _ready_setup())
    assert first.status == STATUS_CRITICAL_UNPROTECTED_POSITION

    # Turn the kill switch ON *after* the position went critical, then make
    # recovery succeed this time.
    db.set_live_kill_switch(True)
    engine.account_client.rows = [{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0.009', 'positionSide': 'BOTH'}]
    trading_client.stop_loss_responses = [{'orderId': 9999, 'status': 'NEW'}]

    second = engine.attempt_entry('SOLUSDT', _ready_setup(symbol='SOLUSDT', id=8))

    # Recovery ran (SL got placed) despite the kill switch...
    assert engine.has_unprotected_position() is False
    log = dict(db.get_live_execution_log(first.execution_id))
    assert log['status'] == STATUS_PROTECTED
    assert log['sl_order_id'] == '9999'
    # ...but the kill switch still blocked the NEW entry attempt on SOLUSDT.
    assert second.status == STATUS_BLOCKED
    assert 'LIVE_KILL_SWITCH_ON' in second.reasons
    # Only the original BTC entry (before the kill switch was even set)
    # ever reached Binance — the SOL attempt never placed an order.
    assert len(trading_client.open_position_calls) == 1
    assert trading_client.open_position_calls[0]['symbol'] == 'BTCUSDT'


# -- architectural guard -----------------------------------------------------

FORBIDDEN_METHOD_NAMES = (
    'withdraw', 'transfer', 'margin_loan', 'change_leverage', 'set_leverage', 'spot_order',
)


def test_engine_has_no_forbidden_methods() -> None:
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(LiveTradingEngine, name)
