from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from database.db import DatabaseManager
from risk.risk_manager import PositionSizeCalculator, RiskManager
from trading.virtual_portfolio import VirtualPortfolio
from config.settings import DEFAULT_RISK_CONFIG

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


def _manager(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / 'trading.db'))
    db.create_tables()
    return db


def _risk_manager(tmp_path: Path) -> RiskManager:
    db = _manager(tmp_path)
    return RiskManager(db, VirtualPortfolio(db))


def _ready_setup(**overrides: Any) -> dict[str, Any]:
    base = {
        'symbol': 'BTCUSDT',
        'direction': 'LONG',
        'setup_type': 'TREND_PULLBACK',
        'entry_zone_low': 100.0,
        'entry_zone_high': 102.0,
        'stop_loss': 95.0,
        'take_profit_1': 110.0,
        'take_profit_2': 115.0,
        'risk_reward_tp1': 2.0,
        'risk_reward_tp2': 3.0,
        'invalidation_level': 95.0,
        'confidence': 'HIGH',
        'technical_score': 70,
        'setup_score': 80,
        'status': 'READY',
        'created_at': '',
        'expires_at': '',
        'reasons': [],
        'rejection_reasons': [],
    }
    base.update(overrides)
    return base


# --- PositionSizeCalculator -------------------------------------------------

def test_position_size_respects_risk_budget_and_margin_caps() -> None:
    calc = PositionSizeCalculator(DEFAULT_RISK_CONFIG)
    result = calc.calculate(entry_price=102.0, stop_loss=95.0, current_equity=500.0)
    risk_budget = 500.0 * DEFAULT_RISK_CONFIG.risk_per_trade_percent / 100
    max_margin = 500.0 * DEFAULT_RISK_CONFIG.max_margin_percent / 100
    assert result['planned_loss_at_stop'] <= risk_budget + 1e-9
    assert result['margin_required'] <= max_margin + 1e-9
    assert result['quantity'] > 0


def test_position_size_caps_by_margin_when_stop_is_tight() -> None:
    # A very tight stop implies a huge notional to spend the whole risk budget;
    # the margin cap should bind instead.
    calc = PositionSizeCalculator(DEFAULT_RISK_CONFIG)
    result = calc.calculate(entry_price=100.0, stop_loss=99.99, current_equity=500.0)
    max_margin = 500.0 * DEFAULT_RISK_CONFIG.max_margin_percent / 100
    assert result['margin_required'] <= max_margin + 1e-9


# --- RiskManager.evaluate: approved path ------------------------------------

def test_approved_when_all_checks_pass(tmp_path: Path) -> None:
    rm = _risk_manager(tmp_path)
    decision = rm.evaluate('BTCUSDT', _ready_setup(), market_status='LIVE', now=NOW)
    assert decision['approved'] is True
    assert decision['block_reasons'] == []
    assert decision['position_quantity'] > 0
    assert decision['current_equity'] == 500.0


# --- Hard rules --------------------------------------------------------------

def test_kill_switch_blocks_approval(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    portfolio = VirtualPortfolio(db)
    portfolio.set_kill_switch(True)
    rm = RiskManager(db, portfolio)
    decision = rm.evaluate('BTCUSDT', _ready_setup(), now=NOW)
    assert decision['approved'] is False
    assert 'KILL_SWITCH_ON' in decision['block_reasons']


def test_missing_setup_blocks_approval(tmp_path: Path) -> None:
    rm = _risk_manager(tmp_path)
    decision = rm.evaluate('BTCUSDT', None, now=NOW)
    assert decision['approved'] is False
    assert 'NO_SETUP' in decision['block_reasons']


def test_setup_not_ready_blocks_approval(tmp_path: Path) -> None:
    rm = _risk_manager(tmp_path)
    decision = rm.evaluate('BTCUSDT', _ready_setup(status='WAITING'), now=NOW)
    assert decision['approved'] is False
    assert 'SETUP_NOT_READY' in decision['block_reasons']


def test_stale_market_data_blocks_approval(tmp_path: Path) -> None:
    rm = _risk_manager(tmp_path)
    decision = rm.evaluate('BTCUSDT', _ready_setup(), market_status='STALE', now=NOW)
    assert decision['approved'] is False
    assert 'STALE_MARKET_DATA' in decision['block_reasons']


def test_rr_below_minimum_blocks_approval(tmp_path: Path) -> None:
    rm = _risk_manager(tmp_path)
    decision = rm.evaluate('BTCUSDT', _ready_setup(risk_reward_tp1=1.0), now=NOW)
    assert decision['approved'] is False
    assert 'RR_BELOW_MINIMUM' in decision['block_reasons']


def test_invalid_stop_blocks_approval(tmp_path: Path) -> None:
    rm = _risk_manager(tmp_path)
    decision = rm.evaluate('BTCUSDT', _ready_setup(stop_loss=0.0), now=NOW)
    assert decision['approved'] is False
    assert 'INVALID_STOP' in decision['block_reasons']


def test_daily_loss_limit_blocks_approval(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    db.add_trade(
        symbol='ETHUSDT', side='SELL', status='closed', net_pnl=-20.0,
        opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper',
    )
    rm = RiskManager(db, VirtualPortfolio(db))
    decision = rm.evaluate('BTCUSDT', _ready_setup(), now=NOW)
    assert decision['approved'] is False
    assert 'DAILY_LOSS_LIMIT' in decision['block_reasons']


def test_weekly_loss_limit_blocks_approval(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    # Spread losses across the week so no single day trips the (tighter) daily limit,
    # but the weekly total exceeds the weekly limit.
    db.add_trade(symbol='ETHUSDT', side='SELL', status='closed', net_pnl=-12.0, opened_at='2026-08-10T09:00:00+00:00', closed_at='2026-08-10T10:00:00+00:00', source='paper')
    db.add_trade(symbol='ETHUSDT', side='SELL', status='closed', net_pnl=-12.0, opened_at='2026-08-11T09:00:00+00:00', closed_at='2026-08-11T10:00:00+00:00', source='paper')
    db.add_trade(symbol='ETHUSDT', side='SELL', status='closed', net_pnl=-12.0, opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper')
    rm = RiskManager(db, VirtualPortfolio(db))
    decision = rm.evaluate('BTCUSDT', _ready_setup(), now=NOW)
    assert decision['approved'] is False
    assert 'WEEKLY_LOSS_LIMIT' in decision['block_reasons']
    assert 'DAILY_LOSS_LIMIT' not in decision['block_reasons']


def test_loss_streak_blocks_approval(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    for i, pnl in enumerate([-1.0, -1.0, -1.0]):
        db.add_trade(symbol='ETHUSDT', side='SELL', status='closed', net_pnl=pnl, opened_at=f'2026-01-0{i+1}T00:00:00+00:00', closed_at=f'2026-01-0{i+1}T01:00:00+00:00', source='paper')
    rm = RiskManager(db, VirtualPortfolio(db))
    decision = rm.evaluate('BTCUSDT', _ready_setup(), now=NOW)
    assert decision['approved'] is False
    assert 'LOSS_STREAK_LIMIT' in decision['block_reasons']


def _open_paper_position(db: DatabaseManager, symbol: str, margin_used: float = 10.0) -> int:
    return db.add_paper_position(
        session_id=None, symbol=symbol, direction='LONG', status='OPEN',
        entry_price=100.0, planned_entry=100.0, quantity_initial=1.0, quantity_remaining=1.0,
        margin_used=margin_used, leverage=5, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0,
        opened_at='2026-01-01T00:00:00+00:00',
    )


def test_max_open_positions_blocks_approval(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    for symbol in ('ETHUSDT', 'SOLUSDT', 'BNBUSDT'):
        _open_paper_position(db, symbol)
    rm = RiskManager(db, VirtualPortfolio(db))
    decision = rm.evaluate('BTCUSDT', _ready_setup(), now=NOW)
    assert decision['approved'] is False
    assert 'MAX_OPEN_POSITIONS' in decision['block_reasons']


def test_duplicate_symbol_position_blocks_approval(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    _open_paper_position(db, 'BTCUSDT')
    rm = RiskManager(db, VirtualPortfolio(db))
    decision = rm.evaluate('BTCUSDT', _ready_setup(), now=NOW)
    assert decision['approved'] is False
    assert 'DUPLICATE_SYMBOL_POSITION' in decision['block_reasons']


# --- Invariants: hold for every approved decision ----------------------------

@pytest.mark.parametrize(
    "entry_price,stop_loss,equity_pnl",
    [
        (102.0, 95.0, 0.0),
        (30000.0, 29500.0, 0.0),
        (1.5, 1.42, 0.0),
        (102.0, 95.0, 200.0),
        (102.0, 95.0, -50.0),
    ],
)
def test_approved_decision_invariants(tmp_path: Path, entry_price: float, stop_loss: float, equity_pnl: float) -> None:
    db = _manager(tmp_path)
    if equity_pnl:
        db.add_trade(
            symbol='ZECUSDT', side='BUY', status='closed', net_pnl=equity_pnl,
            opened_at='2026-01-01T00:00:00+00:00', closed_at='2026-01-01T01:00:00+00:00', source='paper',
        )
    rm = RiskManager(db, VirtualPortfolio(db))
    setup = _ready_setup(entry_zone_low=entry_price - 0.5, entry_zone_high=entry_price, stop_loss=stop_loss)
    decision = rm.evaluate('BTCUSDT', setup, now=NOW)

    if decision['approved']:
        assert decision['planned_loss_at_stop'] <= decision['risk_budget'] + 1e-6
        assert decision['margin_required'] <= decision['current_equity'] * rm.config.max_margin_percent / 100 + 1e-6
        assert decision['leverage'] <= rm.config.leverage
        assert decision['position_quantity'] > 0
        assert decision['current_equity'] > 0
