import os
from pathlib import Path

import pytest

from database.db import DatabaseManager


def test_create_tables_and_seed(tmp_path: Path) -> None:
    db_file = tmp_path / 'trading.db'
    manager = DatabaseManager(str(db_file))
    manager.create_tables()
    assert db_file.exists()
    manager.add_trade(symbol='BTCUSDT', side='BUY', status='open', entry_price=100.0, quantity=1.0, leverage=1.0, opened_at='2026-01-01T00:00:00')
    trades = manager.get_open_trades()
    assert len(trades) == 1
    assert trades[0]['symbol'] == 'BTCUSDT'


def test_close_trade_and_history(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    trade_id = manager.add_trade(symbol='ETHUSDT', side='SELL', status='open', entry_price=2000.0, quantity=0.1, leverage=2.0, opened_at='2026-01-01T00:00:00')
    manager.close_trade(trade_id, exit_price=1950.0, realized_pnl=5.0, net_pnl=4.0, fees=0.1, funding=0.0, exit_reason='Test close', closed_at='2026-01-01T01:00:00')
    assert len(manager.get_open_trades()) == 0
    history = manager.get_trade_history()
    assert len(history) == 1
    assert history[0]['status'] == 'closed'
    assert history[0]['net_pnl'] == 4.0


def test_analytics_stats_empty(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    stats = manager.get_analytics_stats()
    assert stats['total_trades'] == '0'
    assert stats['win_rate'] == '0%'
    assert stats['net_profit'] == '0.00 USDT'


def test_dashboard_stats_empty(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    stats = manager.get_dashboard_stats()
    assert stats['futures_balance'] == '500.00 USDT'
    assert stats['total_equity'] == '500.00 USDT'
    assert stats['trades'] == '0'


def test_add_signal_and_recent(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    manager.add_signal(symbol='BTCUSDT', direction='BUY', technical_score=50.0, historical_score=50.0, ai_score=50.0, final_score=50.0, decision='WAIT', reason='Test', created_at='2026-01-01T00:00:00')
    signals = manager.get_recent_signals()
    assert len(signals) == 1
    assert signals[0]['symbol'] == 'BTCUSDT'


def test_whitelist_symbol_management(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    symbols = manager.get_whitelist_symbols()
    assert len(symbols) == 5
    assert {row['symbol'] for row in symbols} == {'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'ZECUSDT'}
    manager.add_whitelist_symbol('BTCUSDT')
    symbols_after = manager.get_whitelist_symbols()
    assert len(symbols_after) == 5
    manager.remove_whitelist_symbol('BTCUSDT')
    assert all(row['symbol'] != 'BTCUSDT' for row in manager.get_whitelist_symbols())


def test_whitelist_empty_storage_persists(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    for symbol in [row['symbol'] for row in manager.get_whitelist_symbols()]:
        manager.remove_whitelist_symbol(symbol)
    manager.create_tables()
    assert manager.get_whitelist_symbols() == []


def test_analytics_stats_with_closed_trades(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    manager.add_trade(
        symbol='ETHUSDT',
        side='BUY',
        status='closed',
        entry_price=1900.0,
        exit_price=1950.0,
        quantity=0.2,
        leverage=2.0,
        margin_used=38.0,
        realized_pnl=10.0,
        net_pnl=8.0,
        fees=0.1,
        funding=0.0,
        opened_at='2026-01-01T00:00:00',
        closed_at='2026-01-01T01:00:00',
    )
    manager.add_trade(
        symbol='BTCUSDT',
        side='SELL',
        status='closed',
        entry_price=30000.0,
        exit_price=29900.0,
        quantity=0.01,
        leverage=2.0,
        margin_used=150.0,
        realized_pnl=-10.0,
        net_pnl=-9.0,
        fees=0.2,
        funding=0.0,
        opened_at='2026-01-02T00:00:00',
        closed_at='2026-01-02T01:00:00',
    )
    stats = manager.get_analytics_stats()
    assert stats['total_trades'] == '2'
    assert stats['wins'] == '1'
    assert stats['losses'] == '1'
    assert stats['net_profit'] == '-1.00 USDT'


def test_paper_trading_enabled_defaults_false(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    assert manager.get_paper_trading_enabled() is False
    manager.set_paper_trading_enabled(True)
    assert manager.get_paper_trading_enabled() is True


def test_add_and_update_paper_position(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    position_id = manager.add_paper_position(
        session_id=None, symbol='BTCUSDT', direction='LONG', status='OPEN',
        entry_price=100.0, planned_entry=100.0, quantity_initial=1.0, quantity_remaining=1.0,
        margin_used=20.0, leverage=5, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0,
        opened_at='2026-01-01T00:00:00+00:00',
    )
    assert len(manager.get_open_paper_positions()) == 1

    manager.update_paper_position(position_id, status='PARTIALLY_CLOSED', quantity_remaining=0.5, tp1_hit=1, realized_pnl=5.0)
    row = dict(manager.get_paper_position(position_id))
    assert row['status'] == 'PARTIALLY_CLOSED'
    assert row['quantity_remaining'] == 0.5
    assert row['tp1_hit'] == 1

    manager.update_paper_position(position_id, status='CLOSED', quantity_remaining=0.0, closed_at='2026-01-01T02:00:00+00:00')
    assert manager.get_open_paper_positions() == []


def test_update_paper_position_rejects_unknown_field(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    position_id = manager.add_paper_position(
        session_id=None, symbol='BTCUSDT', direction='LONG', status='OPEN',
        entry_price=100.0, planned_entry=100.0, quantity_initial=1.0, quantity_remaining=1.0,
        margin_used=20.0, leverage=5, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0,
        opened_at='2026-01-01T00:00:00+00:00',
    )
    with pytest.raises(ValueError):
        manager.update_paper_position(position_id, entry_price=999.0)


def test_get_or_create_active_session_reuses_open_session(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    first = manager.get_or_create_active_session(500.0)
    second = manager.get_or_create_active_session(500.0)
    assert first == second
    manager.end_session(first)
    third = manager.get_or_create_active_session(500.0)
    assert third != first


def test_setup_execution_status_marks_executed(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    setup_id = manager.add_setup(symbol='BTCUSDT', direction='LONG', setup_type='TREND_PULLBACK')
    assert dict(manager.get_recent_setups(limit=1)[0])['execution_status'] == 'PENDING'
    manager.mark_setup_executed(setup_id)
    assert dict(manager.get_recent_setups(limit=1)[0])['execution_status'] == 'EXECUTED'


def test_trade_history_source_filter_and_paper_columns(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    manager.add_trade(
        symbol='BTCUSDT', side='BUY', status='closed', net_pnl=5.0,
        opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00',
        source='paper', position_id=42, setup_id=7, strategy_version='v1', setup_score=88,
    )
    manager.add_trade(
        symbol='ETHUSDT', side='SELL', status='closed', net_pnl=-2.0,
        opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00',
        source='seed',
    )
    paper_only = manager.get_trade_history(source='paper')
    assert len(paper_only) == 1
    row = dict(paper_only[0])
    assert row['position_id'] == 42
    assert row['setup_id'] == 7
    assert row['strategy_version'] == 'v1'
    assert row['setup_score'] == 88

    paper_analytics = manager.get_analytics_stats(source='paper')
    assert paper_analytics['total_trades'] == '1'
