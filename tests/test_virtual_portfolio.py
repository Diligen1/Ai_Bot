from datetime import datetime, timezone
from pathlib import Path

from database.db import DatabaseManager
from trading.virtual_portfolio import VirtualPortfolio


def _manager(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(str(tmp_path / 'trading.db'))
    manager.create_tables()
    return manager


def _open_paper_position(db: DatabaseManager, symbol: str, margin_used: float) -> int:
    return db.add_paper_position(
        session_id=None, symbol=symbol, direction='LONG', status='OPEN',
        entry_price=100.0, planned_entry=100.0, quantity_initial=1.0, quantity_remaining=1.0,
        margin_used=margin_used, leverage=5, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0,
        opened_at='2026-01-01T00:00:00+00:00',
    )


def test_initial_balance_no_trades(tmp_path: Path) -> None:
    portfolio = VirtualPortfolio(_manager(tmp_path))
    state = portfolio.get_state()
    assert state.starting_balance == 500.0
    assert state.current_equity == 500.0
    assert state.realized_pnl == 0.0


def test_profit_increases_equity(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=10.0, opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state()
    assert state.current_equity == 510.0
    assert state.realized_pnl == 10.0


def test_loss_decreases_equity(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=-15.0, opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state()
    assert state.current_equity == 485.0


def test_multiple_trades_aggregate_correctly(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    for i, pnl in enumerate([10.0, -4.0, 7.5, -2.5]):
        db.add_trade(
            symbol='BTCUSDT', side='BUY', status='closed', net_pnl=pnl,
            opened_at=f'2026-01-0{i+1}T00:00:00+00:00', closed_at=f'2026-01-0{i+1}T01:00:00+00:00', source='paper',
        )
    state = VirtualPortfolio(db).get_state()
    assert state.realized_pnl == 11.0
    assert state.current_equity == 511.0


def test_seed_trades_excluded_from_equity(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=100.0, opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00', source='seed')
    state = VirtualPortfolio(db).get_state()
    assert state.current_equity == 500.0
    assert state.realized_pnl == 0.0


def test_daily_pnl_only_counts_todays_trades(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=5.0, opened_at='2026-08-11T00:00:00+00:00', closed_at='2026-08-11T10:00:00+00:00', source='paper')
    db.add_trade(symbol='ETHUSDT', side='BUY', status='closed', net_pnl=3.0, opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state(now=now)
    assert state.day_start_equity == 505.0
    assert state.current_equity == 508.0
    assert state.daily_realized_pnl == 3.0


def test_weekly_pnl_only_counts_this_weeks_trades(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)  # Wednesday; week starts Mon 2026-08-10
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=2.0, opened_at='2026-08-08T00:00:00+00:00', closed_at='2026-08-08T10:00:00+00:00', source='paper')
    db.add_trade(symbol='ETHUSDT', side='BUY', status='closed', net_pnl=4.0, opened_at='2026-08-11T00:00:00+00:00', closed_at='2026-08-11T10:00:00+00:00', source='paper')
    db.add_trade(symbol='SOLUSDT', side='BUY', status='closed', net_pnl=3.0, opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state(now=now)
    assert state.week_start_equity == 502.0
    assert state.current_equity == 509.0
    assert state.weekly_realized_pnl == 7.0


def test_daily_drawdown_percent_on_loss(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=-10.0, opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state(now=now)
    assert state.day_start_equity == 500.0
    assert state.current_equity == 490.0
    assert round(state.daily_drawdown_percent, 2) == 2.0


def test_daily_drawdown_percent_zero_on_profit(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=10.0, opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state(now=now)
    assert state.daily_drawdown_percent == 0.0


def test_consecutive_losses_basic(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    for i, pnl in enumerate([-1.0, -1.0, -1.0]):
        db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=pnl, opened_at=f'2026-01-0{i+1}T00:00:00+00:00', closed_at=f'2026-01-0{i+1}T01:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state()
    assert state.consecutive_losses == 3


def test_profit_resets_loss_streak(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    # Oldest -> newest: loss, loss, WIN, loss, loss (streak counts back from the newest trade)
    pnls = [-1.0, -1.0, 5.0, -1.0, -1.0]
    for i, pnl in enumerate(pnls):
        db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=pnl, opened_at=f'2026-01-0{i+1}T00:00:00+00:00', closed_at=f'2026-01-0{i+1}T01:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state()
    assert state.consecutive_losses == 2


def test_breakeven_trade_is_neutral_for_streak(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    # Oldest -> newest: loss, breakeven, loss (breakeven neither breaks nor extends the streak)
    pnls = [-1.0, 0.0, -1.0]
    for i, pnl in enumerate(pnls):
        db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=pnl, opened_at=f'2026-01-0{i+1}T00:00:00+00:00', closed_at=f'2026-01-0{i+1}T01:00:00+00:00', source='paper')
    state = VirtualPortfolio(db).get_state()
    assert state.consecutive_losses == 2


def test_open_positions_count_and_reserved_margin(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    _open_paper_position(db, 'BTCUSDT', 40.0)
    _open_paper_position(db, 'ETHUSDT', 25.0)
    state = VirtualPortfolio(db).get_state()
    assert state.open_positions_count == 2
    assert state.reserved_margin == 65.0
    assert state.available_equity == 500.0 - 65.0


def test_has_open_position_detects_duplicate_symbol(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    _open_paper_position(db, 'BTCUSDT', 40.0)
    portfolio = VirtualPortfolio(db)
    assert portfolio.has_open_position('BTCUSDT') is True
    assert portfolio.has_open_position('ETHUSDT') is False


def test_kill_switch_persists_across_new_instances(tmp_path: Path) -> None:
    db_path = str(tmp_path / 'trading.db')
    db = DatabaseManager(db_path)
    db.create_tables()
    portfolio = VirtualPortfolio(db)
    assert portfolio.is_kill_switch_on() is False
    portfolio.set_kill_switch(True)

    fresh_db = DatabaseManager(db_path)
    fresh_db.create_tables()
    fresh_portfolio = VirtualPortfolio(fresh_db)
    assert fresh_portfolio.is_kill_switch_on() is True


def test_state_persists_across_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / 'trading.db')
    db = DatabaseManager(db_path)
    db.create_tables()
    db.add_trade(symbol='BTCUSDT', side='BUY', status='closed', net_pnl=25.0, opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00', source='paper')

    fresh_db = DatabaseManager(db_path)
    fresh_db.create_tables()
    state = VirtualPortfolio(fresh_db).get_state()
    assert state.current_equity == 525.0
