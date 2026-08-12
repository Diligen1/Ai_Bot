"""Tests for VirtualSpotVault (Profit Split V1, paper/virtual trading only)."""
from __future__ import annotations

from pathlib import Path

from database.db import DatabaseManager
from trading.virtual_spot_vault import VirtualSpotVault


def _manager(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / 'trading.db'))
    db.create_tables()
    return db


def _closed_trade(db: DatabaseManager, net_pnl: float, source: str = 'paper', symbol: str = 'BTCUSDT') -> int:
    return db.add_trade(
        symbol=symbol, side='BUY', status='closed', net_pnl=net_pnl,
        opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00',
        source=source,
    )


def test_ten_profit_splits_five_five(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 10.0)

    result = vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 10.0})

    assert result is not None
    assert result.spot_amount == 5.0
    assert result.futures_amount == 5.0
    assert vault.get_balance() == 5.0


def test_four_profit_splits_two_two(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 4.0)

    result = vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 4.0})

    assert result is not None
    assert result.spot_amount == 2.0
    assert result.futures_amount == 2.0
    assert vault.get_balance() == 2.0


def test_loss_does_not_add_to_vault(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, -8.0)

    result = vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': -8.0})

    assert result is None
    assert vault.get_balance() == 0.0


def test_breakeven_does_not_add_to_vault(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 0.0)

    result = vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 0.0})

    assert result is None
    assert vault.get_balance() == 0.0


def test_multiple_profitable_trades_accumulate(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    for pnl in (10.0, 4.0, 6.0):
        trade_id = _closed_trade(db, pnl)
        vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': pnl})

    # 5.0 + 2.0 + 3.0
    assert vault.get_balance() == 10.0


def test_profit_and_loss_mixed(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    profit_id = _closed_trade(db, 10.0)
    loss_id = _closed_trade(db, -6.0)

    profit_result = vault.process_closed_trade({'id': profit_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 10.0})
    loss_result = vault.process_closed_trade({'id': loss_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': -6.0})

    assert profit_result is not None
    assert loss_result is None
    # Only the profit's spot share ever reaches the vault; the loss never touches it.
    assert vault.get_balance() == 5.0


def test_restart_persistence(tmp_path: Path) -> None:
    db_path = str(tmp_path / 'trading.db')
    db = DatabaseManager(db_path)
    db.create_tables()
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 20.0)
    vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 20.0})
    assert vault.get_balance() == 10.0

    fresh_db = DatabaseManager(db_path)
    fresh_db.create_tables()
    fresh_vault = VirtualSpotVault(fresh_db)
    assert fresh_vault.get_balance() == 10.0


def test_duplicate_processing_is_idempotent(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 10.0)
    trade = {'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 10.0}

    first = vault.process_closed_trade(trade)
    second = vault.process_closed_trade(trade)
    third = vault.process_closed_trade(trade)

    assert first is not None
    assert second is None
    assert third is None
    assert vault.get_balance() == 5.0  # not 15.0 — split only ever applied once


def test_seed_trade_does_not_affect_vault(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 50.0, source='seed')

    result = vault.process_closed_trade({'id': trade_id, 'source': 'seed', 'symbol': 'BTCUSDT', 'net_pnl': 50.0})

    assert result is None
    assert vault.get_balance() == 0.0


def test_last_split_reports_most_recent(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    vault = VirtualSpotVault(db)
    for symbol, pnl in (('BTCUSDT', 10.0), ('ETHUSDT', 4.0)):
        trade_id = _closed_trade(db, pnl, symbol=symbol)
        vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': symbol, 'net_pnl': pnl})

    last = vault.get_last_split()
    assert last is not None
    assert last['symbol'] == 'ETHUSDT'
    assert last['spot_amount'] == 2.0


def test_custom_split_percentages_are_respected(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    db.set_profit_split_settings(30.0, 70.0)
    vault = VirtualSpotVault(db)
    trade_id = _closed_trade(db, 100.0)

    result = vault.process_closed_trade({'id': trade_id, 'source': 'paper', 'symbol': 'BTCUSDT', 'net_pnl': 100.0})

    assert result is not None
    assert result.spot_amount == 30.0
    assert result.futures_amount == 70.0
    assert result.spot_percent_used == 30.0
    assert result.futures_percent_used == 70.0
