"""Tests for persistent Profit Split settings (spot/futures percentages).

Covers: database/db.py::get_profit_split_settings/set_profit_split_settings,
VirtualSpotVault reading the CURRENT persistent setting per trade, and old
(already-split) trades never being recalculated after a setting change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from database.db import DatabaseManager
from trading.virtual_spot_vault import VirtualSpotVault


def _manager(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / 'trading.db'))
    db.create_tables()
    return db


def _closed_trade(db: DatabaseManager, net_pnl: float, symbol: str = 'BTCUSDT') -> int:
    return db.add_trade(
        symbol=symbol, side='BUY', status='closed', net_pnl=net_pnl,
        opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00',
        source='paper',
    )


def test_settings_default_to_fifty_fifty(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    assert db.get_profit_split_settings() == (50.0, 50.0)


@pytest.mark.parametrize(
    "spot_percent,futures_percent",
    [(50.0, 50.0), (30.0, 70.0), (0.0, 100.0), (100.0, 0.0)],
)
def test_new_trade_uses_persisted_settings(tmp_path: Path, spot_percent: float, futures_percent: float) -> None:
    db = _manager(tmp_path)
    db.set_profit_split_settings(spot_percent, futures_percent)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 100.0)

    result = vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 100.0})

    assert result is not None
    assert result.spot_amount == pytest.approx(100.0 * spot_percent / 100)
    assert result.futures_amount == pytest.approx(100.0 * futures_percent / 100)
    assert result.spot_percent_used == spot_percent
    assert result.futures_percent_used == futures_percent


def test_invalid_sum_is_rejected(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    with pytest.raises(ValueError):
        db.set_profit_split_settings(40.0, 70.0)  # sums to 110
    # Rejected settings must not overwrite the previous (default) values.
    assert db.get_profit_split_settings() == (50.0, 50.0)


def test_negative_percent_is_rejected(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    with pytest.raises(ValueError):
        db.set_profit_split_settings(-10.0, 110.0)
    assert db.get_profit_split_settings() == (50.0, 50.0)


def test_negative_futures_percent_is_rejected(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    with pytest.raises(ValueError):
        db.set_profit_split_settings(110.0, -10.0)
    assert db.get_profit_split_settings() == (50.0, 50.0)


def test_settings_persist_across_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / 'trading.db')
    db = DatabaseManager(db_path)
    db.create_tables()
    db.set_profit_split_settings(30.0, 70.0)

    fresh_db = DatabaseManager(db_path)
    fresh_db.create_tables()
    assert fresh_db.get_profit_split_settings() == (30.0, 70.0)


def test_old_trade_does_not_recalculate_after_settings_change(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)

    # Trade #1 is split under the default 50/50.
    trade_id_1 = _closed_trade(db, 10.0)
    first = vault.process_closed_trade({'id': trade_id_1, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 10.0})
    assert first is not None
    assert first.spot_amount == 5.0
    assert first.futures_amount == 5.0

    # Setting changes to 30/70.
    db.set_profit_split_settings(30.0, 70.0)

    # Trade #2 (processed AFTER the change) must use the new percentages.
    trade_id_2 = _closed_trade(db, 10.0)
    second = vault.process_closed_trade({'id': trade_id_2, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 10.0})
    assert second is not None
    assert second.spot_amount == 3.0
    assert second.futures_amount == 7.0

    # Trade #1's already-recorded split must be untouched by the setting change.
    stored_first = db.get_profit_split_for_trade(trade_id_1)
    assert stored_first['spot_amount'] == 5.0
    assert stored_first['futures_amount'] == 5.0
    assert stored_first['spot_percent_used'] == 50.0
    assert stored_first['futures_percent_used'] == 50.0

    # Vault balance reflects both splits as actually recorded: 5.0 + 3.0 = 8.0
    assert vault.get_balance() == 8.0
