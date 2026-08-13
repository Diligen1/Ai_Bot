"""Tests for the Live Trading Runtime (execution/live_trading_runtime.py) —
the automated multi-symbol loop that is the ONLY caller of
LiveTradingEngine.attempt_entry() anywhere in the running app.

Every test uses Fake market/analysis/trading/account clients — no real
network call is ever made, and NOTHING here ever sends a real order.
LIVE_TRADING_ENABLED is left unset (=> False) unless a specific test
explicitly monkeypatches it to 'true' to exercise the gated code paths, and
is never left enabled after a test finishes (monkeypatch auto-reverts).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

from binance.account_connector import BinanceAccountSnapshot, BinancePosition
from binance.symbol_rules import BinanceSymbolRules
from binance.trading_client import LiveTradingAPIError
from database.db import DatabaseManager
from execution import live_trading_runtime as runtime_module
from execution.live_execution_engine import LiveExecutionEngine
from execution.live_trading_engine import LiveTradingEngine, STATUS_BLOCKED, STATUS_PROTECTED
from execution.live_trading_runtime import LiveTradingRuntime
from market.setup_engine import TradeSetupEngine
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
    """Serves both is_data_fresh (used by LiveExecutionEngine) and
    get_symbol_data (used directly by the runtime)."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.fresh: set[str] = set()

    def set_price(self, symbol: str, price: float) -> None:
        self.data.setdefault(symbol, {})['last_price'] = price
        self.fresh.add(symbol)

    def get_symbol_data(self, symbol: str) -> dict[str, Any]:
        return self.data.get(symbol, {})

    def is_data_fresh(self, symbol: str) -> bool:
        return symbol in self.fresh


class FakeAnalysisService:
    def __init__(self) -> None:
        self.analysis: dict[str, dict[str, Any]] = {}

    def get_analysis(self, symbol: str) -> dict[str, Any] | None:
        return self.analysis.get(symbol)


class DictSetupEngine(TradeSetupEngine):
    """Returns a per-symbol, caller-controlled setup dict."""

    def __init__(self, setups: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.setups = setups
        self.calls: list[str] = []

    def build_setup(self, analysis: dict[str, Any], market_data: dict[str, Any]) -> dict[str, Any]:
        symbol = analysis.get('symbol')
        self.calls.append(symbol)
        return dict(self.setups.get(symbol, {'setup_type': 'NO_SETUP', 'status': 'REJECTED', 'symbol': symbol}))


class RaisingSetupEngine(DictSetupEngine):
    """Like DictSetupEngine, but raises for a chosen set of symbols — used to
    prove one symbol's failure never blocks the rest of the cycle."""

    def __init__(self, setups: dict[str, dict[str, Any]], raise_for: set[str]) -> None:
        super().__init__(setups)
        self.raise_for = raise_for

    def build_setup(self, analysis: dict[str, Any], market_data: dict[str, Any]) -> dict[str, Any]:
        symbol = analysis.get('symbol')
        if symbol in self.raise_for:
            self.calls.append(symbol)
            raise RuntimeError(f'simulated analysis failure for {symbol}')
        return super().build_setup(analysis, market_data)


class FakeAccountClientForLeverage:
    """Serves BOTH the leverage lookup and the real-position lookup — the
    real LiveTradingEngine calls get_position_risk() for each."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def get_position_risk(self) -> list[dict[str, Any]]:
        return self.rows


class FakeTradingClient:
    def __init__(self) -> None:
        self.position_mode: dict[str, Any] = {'dualSidePosition': False}
        self.open_position_responses: list[Any] = [{'orderId': 1001, 'status': 'FILLED', 'executedQty': '0.009', 'avgPrice': '50000.0'}]
        self.open_position_calls: list[dict[str, Any]] = []
        self.stop_loss_responses: list[Any] = [{'orderId': 2001, 'status': 'NEW'}]
        self.stop_loss_calls: list[dict[str, Any]] = []
        self.take_profit_responses: list[Any] = [{'orderId': 3001, 'status': 'NEW'}]
        self.order_status_response: Any = None

    def get_position_mode(self) -> dict[str, Any]:
        return self.position_mode

    def open_position(self, **kwargs: Any) -> dict[str, Any]:
        self.open_position_calls.append(kwargs)
        return self._consume(self.open_position_responses)

    def place_stop_loss(self, **kwargs: Any) -> dict[str, Any]:
        self.stop_loss_calls.append(kwargs)
        return self._consume(self.stop_loss_responses)

    def place_take_profit(self, **kwargs: Any) -> dict[str, Any]:
        return self._consume(self.take_profit_responses)

    def get_order_status(self, symbol: str, order_id: Any = None, client_order_id: Any = None) -> dict[str, Any]:
        if isinstance(self.order_status_response, Exception):
            raise self.order_status_response
        if self.order_status_response is None:
            raise LiveTradingAPIError('no order found')
        return self.order_status_response

    def cancel_order(self, symbol: str, order_id: Any = None, client_order_id: Any = None) -> dict[str, Any]:
        return {'status': 'CANCELED'}

    @staticmethod
    def _consume(queue: list[Any]) -> Any:
        if not queue:
            raise AssertionError('no more fake responses queued')
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def _connected_snapshot(wallet_balance: float = 1000.0, positions: list[BinancePosition] | None = None) -> BinanceAccountSnapshot:
    return BinanceAccountSnapshot(
        status='CONNECTED', wallet_balance=wallet_balance, available_balance=wallet_balance,
        unrealized_pnl=0.0, can_trade=True, positions=positions or [],
    )


def _ready_setup(symbol: str = 'BTCUSDT', **overrides: Any) -> dict[str, Any]:
    base = {
        'symbol': symbol, 'direction': 'LONG', 'setup_type': 'TREND_PULLBACK',
        'entry_zone_low': 49500.0, 'entry_zone_high': 50000.0, 'stop_loss': 49000.0,
        'take_profit_1': 51000.0, 'take_profit_2': 52000.0, 'status': 'READY', 'id': 7,
    }
    base.update(overrides)
    return base


def _waiting_setup(symbol: str) -> dict[str, Any]:
    return {'symbol': symbol, 'setup_type': 'NO_SETUP', 'status': 'WAITING', 'direction': None}


def _build_runtime(
    tmp_path: Path,
    whitelist: tuple[str, ...] = ('BTCUSDT',),
    setups: dict[str, dict[str, Any]] | None = None,
    binance_positions: list[BinancePosition] | None = None,
    leverage_rows: list[dict[str, Any]] | None = None,
    wallet_balance: float = 1000.0,
    live_kill_switch: bool = False,
    min_qty: str = '0.001',
    interval_seconds: float = 1000.0,
    raise_for: set[str] | None = None,
    symbols_provider: Any = None,
):
    db = _manager(tmp_path)
    # create_tables() seeds a default 5-symbol whitelist on a fresh DB (see
    # database/db.py::_initialize_default_whitelist) — reset to exactly the
    # symbols this test wants before adding them.
    for existing_symbol in {row['symbol'] for row in db.get_whitelist_symbols()} - set(whitelist):
        db.remove_whitelist_symbol(existing_symbol)
    for symbol in whitelist:
        db.add_whitelist_symbol(symbol)
    if live_kill_switch:
        db.set_live_kill_switch(True)

    connector = FakeConnector(_connected_snapshot(wallet_balance=wallet_balance, positions=binance_positions or []))
    guard = LiveRiskGuard(connector, db)

    entries = [
        {
            'symbol': symbol, 'status': 'TRADING',
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': min_qty},
                {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
            ],
        }
        for symbol in whitelist
    ]
    symbol_rules = BinanceSymbolRules(client=FakePublicClient({'symbols': entries}))

    market_service = FakeMarketService()
    analysis_service = FakeAnalysisService()
    for symbol in whitelist:
        market_service.set_price(symbol, 50000.0)
        analysis_service.analysis[symbol] = {'symbol': symbol, 'status': 'LIVE'}

    exec_engine = LiveExecutionEngine(guard, symbol_rules, db, market_service)

    trading_client = FakeTradingClient()
    default_leverage_rows = leverage_rows if leverage_rows is not None else [{'symbol': s, 'leverage': '5'} for s in whitelist]
    account_client = FakeAccountClientForLeverage(default_leverage_rows)

    live_engine = LiveTradingEngine(trading_client, account_client, guard, exec_engine, db)

    setups = setups or {}
    if raise_for:
        setup_engine: DictSetupEngine = RaisingSetupEngine(setups, raise_for)
    else:
        setup_engine = DictSetupEngine(setups)

    provider = symbols_provider or (lambda: [row['symbol'] for row in db.get_whitelist_symbols()])
    runtime = LiveTradingRuntime(
        live_engine, setup_engine, market_service, analysis_service, provider,
        interval_seconds=interval_seconds,
    )
    return runtime, db, trading_client, live_engine, setup_engine, market_service, analysis_service


# -- 1. lifecycle ---------------------------------------------------------

def test_1_runtime_starts_and_stops(tmp_path: Path) -> None:
    runtime, *_ = _build_runtime(tmp_path)
    assert runtime.is_running() is False

    runtime.start()
    assert runtime.is_running() is True

    runtime.stop()
    assert runtime.is_running() is False


def test_1b_double_start_never_spawns_a_second_thread(tmp_path: Path) -> None:
    runtime, *_ = _build_runtime(tmp_path)
    runtime.start()
    first_thread = runtime._thread
    runtime.start()  # second call, e.g. from a re-imported module
    assert runtime._thread is first_thread
    runtime.stop()


# -- 2. LIVE=false -> zero order calls -------------------------------------

def test_2_live_disabled_never_calls_order_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv('LIVE_TRADING_ENABLED', raising=False)
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups={'BTCUSDT': _ready_setup()},
    )

    runtime.run_once()

    assert trading_client.open_position_calls == []
    result = live_engine.get_last_execution_result()
    assert result.status == STATUS_BLOCKED
    assert 'LIVE_TRADING_DISABLED' in result.reasons


# -- 3. multi-symbol: all whitelist symbols checked ------------------------

def test_3_live_enabled_all_five_symbols_checked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    whitelist = ('BNBUSDT', 'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ZECUSDT')
    setups = {symbol: _waiting_setup(symbol) for symbol in whitelist}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=whitelist, setups=setups,
    )

    runtime.run_once()

    assert sorted(runtime.get_last_symbols_checked()) == sorted(whitelist)
    assert sorted(setup_engine.calls) == sorted(whitelist)


# -- 4. NO_SETUP on one symbol does not stop the cycle ---------------------

def test_4_no_setup_on_btc_does_not_block_eth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _waiting_setup('BTCUSDT'), 'ETHUSDT': _waiting_setup('ETHUSDT')}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT', 'ETHUSDT'), setups=setups,
    )

    runtime.run_once()

    assert set(runtime.get_last_symbols_checked()) == {'BTCUSDT', 'ETHUSDT'}


# -- 5. one symbol's exception never blocks the others ---------------------

def test_5_exception_on_btc_does_not_block_eth_and_sol(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'ETHUSDT': _waiting_setup('ETHUSDT'), 'SOLUSDT': _waiting_setup('SOLUSDT')}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT', 'ETHUSDT', 'SOLUSDT'), setups=setups, raise_for={'BTCUSDT'},
    )

    runtime.run_once()

    assert 'BTCUSDT' in runtime.get_last_cycle_errors()
    assert set(runtime.get_last_symbols_checked()) == {'ETHUSDT', 'SOLUSDT'}


# -- 6/7. dynamic whitelist, no restart -------------------------------------

def test_6_new_whitelist_symbol_picked_up_next_cycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _waiting_setup('BTCUSDT'), 'ETHUSDT': _waiting_setup('ETHUSDT')}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups,
    )

    runtime.run_once()
    assert runtime.get_last_symbols_checked() == ['BTCUSDT']

    db.add_whitelist_symbol('ETHUSDT')
    market.set_price('ETHUSDT', 100.0)
    analysis.analysis['ETHUSDT'] = {'symbol': 'ETHUSDT', 'status': 'LIVE'}

    runtime.run_once()
    assert set(runtime.get_last_symbols_checked()) == {'BTCUSDT', 'ETHUSDT'}


def test_7_removed_whitelist_symbol_disappears_next_cycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _waiting_setup('BTCUSDT'), 'ETHUSDT': _waiting_setup('ETHUSDT')}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT', 'ETHUSDT'), setups=setups,
    )

    runtime.run_once()
    assert set(runtime.get_last_symbols_checked()) == {'BTCUSDT', 'ETHUSDT'}

    db.remove_whitelist_symbol('ETHUSDT')

    runtime.run_once()
    assert runtime.get_last_symbols_checked() == ['BTCUSDT']


# -- 8/9. existing real position only blocks its own symbol -----------------

def test_8_and_9_existing_zec_position_blocks_only_zec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    existing = [BinancePosition(symbol='ZECUSDT', position_amt=1.0, entry_price=30.0, unrealized_pnl=0.0, leverage=5, position_side='BOTH')]
    setups = {'ZECUSDT': _ready_setup('ZECUSDT', id=10), 'BTCUSDT': _ready_setup('BTCUSDT', id=11)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('ZECUSDT', 'BTCUSDT'), setups=setups, binance_positions=existing,
        leverage_rows=[{'symbol': 'ZECUSDT', 'leverage': '5'}, {'symbol': 'BTCUSDT', 'leverage': '5'}],
    )

    runtime.run_once()

    assert set(runtime.get_last_symbols_checked()) == {'ZECUSDT', 'BTCUSDT'}
    assert [call['symbol'] for call in trading_client.open_position_calls] == ['BTCUSDT']


# -- 10. max open positions ---------------------------------------------

def test_10_max_open_positions_blocks_new_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    existing = [
        BinancePosition(symbol='ETHUSDT', position_amt=1.0, entry_price=100.0, unrealized_pnl=0.0, leverage=5, position_side='BOTH'),
        BinancePosition(symbol='SOLUSDT', position_amt=1.0, entry_price=20.0, unrealized_pnl=0.0, leverage=5, position_side='BOTH'),
        BinancePosition(symbol='BNBUSDT', position_amt=1.0, entry_price=300.0, unrealized_pnl=0.0, leverage=5, position_side='BOTH'),
    ]
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=12)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups, binance_positions=existing,
    )

    runtime.run_once()

    assert trading_client.open_position_calls == []
    result = live_engine.get_last_execution_result()
    assert result.status == STATUS_BLOCKED


# -- 11. kill switch --------------------------------------------------------

def test_11_kill_switch_blocks_new_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=13)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups, live_kill_switch=True,
    )

    runtime.run_once()

    assert trading_client.open_position_calls == []
    result = live_engine.get_last_execution_result()
    assert result.status == STATUS_BLOCKED
    assert 'LIVE_KILL_SWITCH_ON' in result.reasons


# -- 12/13. CRITICAL blocks everything, recovery still runs -----------------

def test_12_critical_unprotected_position_blocks_all_symbols(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=14), 'ETHUSDT': _ready_setup('ETHUSDT', id=15)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT', 'ETHUSDT'), setups=setups,
        leverage_rows=[{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0.01', 'positionSide': 'BOTH'},
                       {'symbol': 'ETHUSDT', 'leverage': '5'}],
    )
    # Pre-seed a CRITICAL_UNPROTECTED_POSITION row that can never be
    # resolved this cycle (no stop_loss recorded -> _try_protect returns False).
    db.add_live_execution_log(
        execution_id='exec-critical', client_order_id='bot-critical-entry', setup_id=99,
        symbol='BTCUSDT', side='BUY', quantity=0.01, status='CRITICAL_UNPROTECTED_POSITION',
    )

    runtime.run_once()

    assert set(runtime.get_last_symbols_checked()) == {'BTCUSDT', 'ETHUSDT'}
    assert trading_client.open_position_calls == []
    assert live_engine.has_unprotected_position() is True


def test_13_recovery_still_runs_during_critical_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=16)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups,
        # get_position_risk() reports the position as already FLAT -> recovery
        # resolves it to POSITION_CLOSED_EXTERNALLY without placing any order.
        leverage_rows=[{'symbol': 'BTCUSDT', 'leverage': '5', 'positionAmt': '0', 'positionSide': 'BOTH'}],
    )
    db.add_live_execution_log(
        execution_id='exec-critical', client_order_id='bot-critical-entry', setup_id=99,
        symbol='BTCUSDT', side='BUY', quantity=0.01, stop_loss=49000.0, position_side='BOTH',
        status='CRITICAL_UNPROTECTED_POSITION',
    )

    runtime.run_once()

    log = dict(db.get_live_execution_log('exec-critical'))
    assert log['status'] == 'POSITION_CLOSED_EXTERNALLY'
    assert live_engine.has_unprotected_position() is False


# -- 14/15. READY-only gate at the runtime level -----------------------------

def test_14_ready_setup_calls_attempt_entry_exactly_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=17)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups,
    )
    calls: list[str] = []
    original = live_engine.attempt_entry

    def _spy(symbol, setup):
        calls.append(symbol)
        return original(symbol, setup)

    live_engine.attempt_entry = _spy

    runtime.run_once()

    assert calls == ['BTCUSDT']


def test_15_rejected_setup_never_calls_attempt_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    for status in ('NO_SETUP', 'WAITING', 'REJECTED', 'MISSED_ENTRY', 'INVALIDATED', 'EXPIRED'):
        setups = {'BTCUSDT': {'symbol': 'BTCUSDT', 'setup_type': 'NO_SETUP', 'status': status, 'direction': None}}
        runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
            tmp_path, whitelist=('BTCUSDT',), setups=setups,
        )
        calls: list[str] = []
        live_engine.attempt_entry = lambda symbol, setup, _calls=calls: (_calls.append(symbol), None)[1]

        runtime.run_once()

        assert calls == [], f'attempt_entry called for status={status}'


# -- 16. no duplicate entry across cycles ------------------------------------

def test_16_same_ready_setup_across_two_cycles_places_one_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=18)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups,
    )

    runtime.run_once()
    first_result = live_engine.get_last_execution_result()
    assert first_result.status == STATUS_PROTECTED

    runtime.run_once()
    second_result = live_engine.get_last_execution_result()

    assert len(trading_client.open_position_calls) == 1
    assert second_result.status != STATUS_PROTECTED or second_result.reasons  # ALREADY_EXECUTED path, never a new order


# -- 17. Dashboard is not required for the runtime ---------------------------

def test_17_runtime_module_has_no_app_dependency(tmp_path: Path) -> None:
    """Static check: the module imports nothing from app.* or fastapi — the
    Dashboard/web layer is not a dependency of the runtime at all."""
    tree = ast.parse(inspect.getsource(runtime_module))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(name.startswith('app') for name in imported_modules)
    assert not any(name.startswith('fastapi') for name in imported_modules)


def test_17b_run_once_works_without_any_web_framework_import(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    setups = {'BTCUSDT': _ready_setup('BTCUSDT', id=19)}
    runtime, db, trading_client, live_engine, setup_engine, market, analysis = _build_runtime(
        tmp_path, whitelist=('BTCUSDT',), setups=setups,
    )
    # No app.server / FastAPI / TestClient anywhere in this test file or in
    # execution/live_trading_runtime.py — this call proves the runtime is
    # fully usable standalone.
    runtime.run_once()
    assert runtime.get_last_cycle_at() is not None


# -- 18. no real Binance requests during tests -------------------------------
# Every test above uses FakeTradingClient/FakeAccountClientForLeverage/
# FakePublicClient exclusively — the real binance.trading_client.LiveTradingClient
# and binance.account_client.BinanceAccountClient are never constructed
# anywhere in this file, so no test here can ever reach the network,
# regardless of what a developer's local LIVE_TRADING_ENABLED is set to.
