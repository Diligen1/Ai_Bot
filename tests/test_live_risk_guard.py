"""Tests for the Live Account Risk Guard (risk/live_risk_guard.py).

All tests use a fake BinanceAccountConnector — no real network calls to
Binance are ever made, and no order/leverage/transfer method is exercised
because none exists on LiveRiskGuard (see the architectural guard test).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from binance.account_connector import BinanceAccountSnapshot, BinancePosition
from config.settings import DEFAULT_RISK_CONFIG
from database.db import DatabaseManager
from risk.live_risk_guard import LiveRiskGuard, LiveRiskSnapshot


def _manager(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / 'trading.db'))
    db.create_tables()
    return db


class FakeConnector:
    def __init__(self, snapshot: BinanceAccountSnapshot) -> None:
        self._snapshot = snapshot

    def get_snapshot(self) -> BinanceAccountSnapshot:
        return self._snapshot


def _connected_snapshot(
    wallet_balance: float,
    available_balance: float | None = None,
    unrealized_pnl: float = 0.0,
    positions: list[BinancePosition] | None = None,
) -> BinanceAccountSnapshot:
    return BinanceAccountSnapshot(
        status='CONNECTED',
        wallet_balance=wallet_balance,
        available_balance=available_balance if available_balance is not None else wallet_balance,
        unrealized_pnl=unrealized_pnl,
        can_trade=True,
        positions=positions or [],
    )


# -- architectural guards ---------------------------------------------------

FORBIDDEN_METHOD_NAMES = (
    'create_order', 'place_order', 'cancel_order', 'new_order', 'close_order',
    'transfer', 'withdraw', 'change_leverage', 'set_leverage',
    'close_position', 'modify_position', 'change_margin_type',
)


def test_no_trading_or_transfer_methods_exist() -> None:
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(LiveRiskGuard, name), f'LiveRiskGuard must not implement {name}()'


def test_constructor_never_accepts_virtual_portfolio() -> None:
    """VirtualPortfolio (paper capital) and the real Binance account must
    never be mixed — LiveRiskGuard's constructor must not even have a
    parameter shaped like a portfolio dependency."""
    params = set(inspect.signature(LiveRiskGuard.__init__).parameters)
    assert not any('portfolio' in name.lower() for name in params)


# -- equity-based limits (500 / 1000 USDT, max margin 10%, risk 1%) -------

def test_500_usdt_equity_computes_correct_limits(tmp_path: Path) -> None:
    connector = FakeConnector(_connected_snapshot(wallet_balance=500.0))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert snapshot.status == 'CONNECTED'
    assert snapshot.real_equity == 500.0
    assert snapshot.max_allowed_margin == 50.0  # 10% of 500
    assert snapshot.risk_budget == 5.0  # 1% of 500


def test_1000_usdt_equity_computes_correct_limits(tmp_path: Path) -> None:
    connector = FakeConnector(_connected_snapshot(wallet_balance=1000.0))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert snapshot.real_equity == 1000.0
    assert snapshot.max_allowed_margin == 100.0  # 10% of 1000
    assert snapshot.risk_budget == 10.0  # 1% of 1000


def test_max_margin_percent_matches_configured_10_percent(tmp_path: Path) -> None:
    connector = FakeConnector(_connected_snapshot(wallet_balance=2500.0))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert DEFAULT_RISK_CONFIG.max_margin_percent == 10.0
    assert snapshot.max_allowed_margin == 2500.0 * 0.10


def test_risk_per_trade_percent_matches_configured_1_percent(tmp_path: Path) -> None:
    connector = FakeConnector(_connected_snapshot(wallet_balance=2500.0))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert DEFAULT_RISK_CONFIG.risk_per_trade_percent == 1.0
    assert snapshot.risk_budget == 2500.0 * 0.01


def test_leverage_limit_matches_configured_5x(tmp_path: Path) -> None:
    connector = FakeConnector(_connected_snapshot(wallet_balance=500.0))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert DEFAULT_RISK_CONFIG.leverage == 5
    assert snapshot.leverage_limit == 5


# -- open positions (3-position limit) --------------------------------------

def test_three_open_positions_reflected_against_limit(tmp_path: Path) -> None:
    positions = [
        BinancePosition(symbol='BTCUSDT', position_amt=0.1, entry_price=50000.0, unrealized_pnl=10.0, leverage=5, position_side='BOTH'),
        BinancePosition(symbol='ETHUSDT', position_amt=-1.0, entry_price=3000.0, unrealized_pnl=-5.0, leverage=5, position_side='BOTH'),
        BinancePosition(symbol='SOLUSDT', position_amt=5.0, entry_price=100.0, unrealized_pnl=2.0, leverage=3, position_side='BOTH'),
    ]
    connector = FakeConnector(_connected_snapshot(wallet_balance=1000.0, positions=positions))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert snapshot.open_positions_count == 3
    assert snapshot.max_positions_limit == 3  # DEFAULT_RISK_CONFIG.max_open_positions
    assert len(snapshot.positions) == 3


def test_current_exposure_is_sum_of_position_notionals(tmp_path: Path) -> None:
    positions = [
        BinancePosition(symbol='BTCUSDT', position_amt=0.1, entry_price=50000.0, unrealized_pnl=0.0, leverage=5, position_side='BOTH'),
        BinancePosition(symbol='ETHUSDT', position_amt=-2.0, entry_price=3000.0, unrealized_pnl=0.0, leverage=5, position_side='BOTH'),
    ]
    connector = FakeConnector(_connected_snapshot(wallet_balance=10000.0, positions=positions))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    # |0.1 * 50000| + |-2.0 * 3000| = 5000 + 6000 = 11000
    assert snapshot.current_exposure == 11000.0


# -- kill switch ------------------------------------------------------------

def test_kill_switch_off_by_default(tmp_path: Path) -> None:
    connector = FakeConnector(_connected_snapshot(wallet_balance=500.0))
    db = _manager(tmp_path)
    guard = LiveRiskGuard(connector, db)

    snapshot = guard.get_snapshot()

    assert snapshot.kill_switch_on is False


def test_kill_switch_on_is_reflected(tmp_path: Path) -> None:
    """LiveRiskGuard must react to the LIVE kill switch, not the separate
    paper-trading one — see database/db.py get_live_kill_switch()."""
    connector = FakeConnector(_connected_snapshot(wallet_balance=500.0))
    db = _manager(tmp_path)
    db.set_live_kill_switch(True)
    guard = LiveRiskGuard(connector, db)

    snapshot = guard.get_snapshot()

    assert snapshot.kill_switch_on is True


def test_kill_switch_reported_even_when_binance_disabled(tmp_path: Path) -> None:
    connector = FakeConnector(BinanceAccountSnapshot(status='DISABLED'))
    db = _manager(tmp_path)
    db.set_live_kill_switch(True)
    guard = LiveRiskGuard(connector, db)

    snapshot = guard.get_snapshot()

    assert snapshot.status == 'DISABLED'
    assert snapshot.kill_switch_on is True


# -- regression: LIVE kill switch must never be confused with PAPER's ------

def test_paper_kill_switch_on_does_not_affect_live_risk_guard(tmp_path: Path) -> None:
    """CRITICAL regression test (final-audit finding #1): LiveRiskGuard must
    read ONLY the LIVE kill switch. Turning the separate PAPER-trading kill
    switch on (paper_trading_engine.py) must never block/flag live risk."""
    connector = FakeConnector(_connected_snapshot(wallet_balance=500.0))
    db = _manager(tmp_path)
    db.set_kill_switch(True)  # PAPER kill switch only
    assert db.get_live_kill_switch() is False  # LIVE switch is untouched
    guard = LiveRiskGuard(connector, db)

    snapshot = guard.get_snapshot()

    assert snapshot.kill_switch_on is False


def test_live_kill_switch_on_blocks_regardless_of_paper_switch(tmp_path: Path) -> None:
    """The converse: the LIVE kill switch alone must be sufficient to flag
    LiveRiskGuard as blocked, with the PAPER switch left OFF."""
    connector = FakeConnector(_connected_snapshot(wallet_balance=500.0))
    db = _manager(tmp_path)
    assert db.get_kill_switch() is False  # PAPER switch stays off
    db.set_live_kill_switch(True)
    guard = LiveRiskGuard(connector, db)

    snapshot = guard.get_snapshot()

    assert snapshot.kill_switch_on is True


# -- status passthrough (DISABLED / ERROR never crash, never fabricate numbers) --

def test_disabled_status_passes_through_with_zeroed_fields(tmp_path: Path) -> None:
    connector = FakeConnector(BinanceAccountSnapshot(status='DISABLED'))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert snapshot.status == 'DISABLED'
    assert snapshot.real_equity == 0.0
    assert snapshot.max_allowed_margin == 0.0
    assert snapshot.risk_budget == 0.0
    assert snapshot.positions == []


def test_error_status_passes_through_with_message(tmp_path: Path) -> None:
    connector = FakeConnector(BinanceAccountSnapshot(status='ERROR', error_message='Binance error -1021: timestamp'))
    guard = LiveRiskGuard(connector, _manager(tmp_path))

    snapshot = guard.get_snapshot()

    assert snapshot.status == 'ERROR'
    assert snapshot.error_message == 'Binance error -1021: timestamp'
    assert snapshot.real_equity == 0.0


# -- no mixing with VirtualPortfolio ----------------------------------------

def test_snapshot_never_carries_virtual_portfolio_data(tmp_path: Path) -> None:
    """LiveRiskSnapshot's fields must come exclusively from the Binance
    connector — asserting the dataclass has no virtual/paper-named field is
    a cheap structural guard against future accidental mixing."""
    field_names = set(LiveRiskSnapshot.__dataclass_fields__.keys())
    assert not any('virtual' in name.lower() or 'paper' in name.lower() for name in field_names)
