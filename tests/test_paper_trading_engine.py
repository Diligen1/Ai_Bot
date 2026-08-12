"""Tests for PaperTradingEngine.

Entry-zone / WAITING / MISSED_ENTRY status logic itself is TradeSetupEngine's
responsibility and is covered by tests/test_setup_engine.py. These tests use
a fixed/dict-driven setup engine so PaperTradingEngine's own behaviour (does
it correctly respect READY/approved, TP1/TP2/SL mechanics, fees/slippage,
idempotency, recovery) can be verified deterministically without any live
Binance network call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import DEFAULT_RISK_CONFIG
from database.db import DatabaseManager
from market.setup_engine import TradeSetupEngine
from risk.risk_manager import RiskManager
from trading.paper_trading_engine import PaperTradingEngine
from trading.virtual_portfolio import VirtualPortfolio

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


class FakeMarketService:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def set_price(self, symbol: str, price: float, candles_15m: list | None = None) -> None:
        entry = self.data.setdefault(symbol, {})
        entry['last_price'] = price
        if candles_15m is not None:
            entry['candles_15m'] = candles_15m

    def get_symbol_data(self, symbol: str) -> dict[str, Any]:
        return self.data.get(symbol, {})

    def is_data_fresh(self, symbol: str) -> bool:
        return symbol in self.data


class FakeAnalysisService:
    def __init__(self) -> None:
        self.analysis: dict[str, dict[str, Any] | None] = {}

    def get_analysis(self, symbol: str) -> dict[str, Any] | None:
        return self.analysis.get(symbol)


class FixedSetupEngine(TradeSetupEngine):
    """Returns a single caller-controlled setup dict, bypassing zone-building math."""

    def __init__(self, setup: dict[str, Any]) -> None:
        super().__init__()
        self.fixed = setup

    def build_setup(self, analysis: dict[str, Any], symbol_data: dict[str, Any]) -> dict[str, Any]:
        return dict(self.fixed)


class DictSetupEngine(TradeSetupEngine):
    """Returns a per-symbol setup dict, for multi-symbol scenarios."""

    def __init__(self, setups: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.setups = setups

    def build_setup(self, analysis: dict[str, Any], symbol_data: dict[str, Any]) -> dict[str, Any]:
        symbol = analysis.get('symbol')
        return dict(self.setups.get(symbol, {'setup_type': 'NO_SETUP', 'status': 'REJECTED'}))


def _manager(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / 'trading.db'))
    db.create_tables()
    return db


def _long_setup(**overrides: Any) -> dict[str, Any]:
    base = {
        'symbol': 'BTCUSDT', 'direction': 'LONG', 'setup_type': 'TREND_PULLBACK',
        'entry_zone_low': 100.0, 'entry_zone_high': 102.0, 'stop_loss': 95.0,
        'take_profit_1': 110.0, 'take_profit_2': 120.0,
        'risk_reward_tp1': 2.0, 'risk_reward_tp2': 3.0, 'invalidation_level': 95.0,
        'confidence': 'HIGH', 'technical_score': 80, 'setup_score': 85,
        'status': 'READY', 'created_at': NOW.isoformat(), 'expires_at': NOW.isoformat(),
        'reasons': [], 'rejection_reasons': [], 'analysis_snapshot': {},
    }
    base.update(overrides)
    return base


def _short_setup(**overrides: Any) -> dict[str, Any]:
    base = {
        'symbol': 'ETHUSDT', 'direction': 'SHORT', 'setup_type': 'TREND_PULLBACK',
        'entry_zone_low': 98.0, 'entry_zone_high': 100.0, 'stop_loss': 105.0,
        'take_profit_1': 90.0, 'take_profit_2': 80.0,
        'risk_reward_tp1': 2.0, 'risk_reward_tp2': 3.0, 'invalidation_level': 105.0,
        'confidence': 'HIGH', 'technical_score': 80, 'setup_score': 85,
        'status': 'READY', 'created_at': NOW.isoformat(), 'expires_at': NOW.isoformat(),
        'reasons': [], 'rejection_reasons': [], 'analysis_snapshot': {},
    }
    base.update(overrides)
    return base


def _fake_analysis(symbol: str, direction: str, last_price: float, status: str = 'LIVE') -> dict[str, Any]:
    return {
        'symbol': symbol, 'status': status, 'last_price': last_price, 'direction': direction,
        'technical_score': 80, 'confidence': 'HIGH',
        'market_regime': 'TREND_UP' if direction == 'LONG' else 'TREND_DOWN',
        'timeframe_alignment': 'STRONG_LONG' if direction == 'LONG' else 'STRONG_SHORT',
        'trade_candidate': True, 'reasons': [], 'block_reasons': [],
        'analysis': {'4h': {}, '1h': {}, '15m': {}},
        'volume_current': 100, 'volume_average': 90, 'volume_ratio': 1.1,
        'atr': 1.0, 'atr_percent': 1.0,
    }


def _build(tmp_path: Path, setup: dict[str, Any], enabled: bool = True):
    symbol = setup['symbol']
    db = _manager(tmp_path)
    db.set_paper_trading_enabled(enabled)
    market = FakeMarketService()
    analysis_service = FakeAnalysisService()
    setup_engine = FixedSetupEngine(setup)
    portfolio = VirtualPortfolio(db)
    risk_manager = RiskManager(db, portfolio)
    engine = PaperTradingEngine(db, portfolio, risk_manager, setup_engine, market, analysis_service, lambda: [symbol])
    return db, market, analysis_service, portfolio, engine, symbol


# -- enable switch ------------------------------------------------------

def test_paper_trading_disabled_by_default(tmp_path: Path) -> None:
    db = _manager(tmp_path)
    assert db.get_paper_trading_enabled() is False


def test_paper_trading_enable_disable_persistence(tmp_path: Path) -> None:
    db_path = str(tmp_path / 'trading.db')
    db = DatabaseManager(db_path)
    db.create_tables()
    db.set_paper_trading_enabled(True)

    fresh = DatabaseManager(db_path)
    fresh.create_tables()
    assert fresh.get_paper_trading_enabled() is True

    fresh.set_paper_trading_enabled(False)
    fresher = DatabaseManager(db_path)
    fresher.create_tables()
    assert fresher.get_paper_trading_enabled() is False


def test_disabled_engine_never_opens_position(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup, enabled=False)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


# -- entry --------------------------------------------------------------

def test_long_entry_opens_position(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)

    positions = db.get_open_paper_positions()
    assert len(positions) == 1
    pos = dict(positions[0])
    assert pos['direction'] == 'LONG'
    assert pos['status'] == 'OPEN'
    assert pos['quantity_initial'] > 0
    # LONG entry buy fill must be worse (higher) than the quoted price (adverse slippage)
    assert pos['entry_price'] > 101.0


def test_short_entry_opens_position(tmp_path: Path) -> None:
    setup = _short_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 99.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'SHORT', 99.0)
    engine.run_once(now=NOW)

    positions = db.get_open_paper_positions()
    assert len(positions) == 1
    pos = dict(positions[0])
    assert pos['direction'] == 'SHORT'
    # SHORT entry sell fill must be worse (lower) than the quoted price
    assert pos['entry_price'] < 99.0


def test_no_entry_while_setup_waiting(tmp_path: Path) -> None:
    setup = _long_setup(status='WAITING')
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


def test_no_entry_on_missed_entry_status(tmp_path: Path) -> None:
    setup = _long_setup(status='MISSED_ENTRY')
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 130.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 130.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


def test_entry_zone_reached_transitions_to_open(tmp_path: Path) -> None:
    setup = _long_setup(status='WAITING')
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 105.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 105.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == [], "still WAITING: must not chase price"

    engine.setup_engine.fixed['status'] = 'READY'
    market.set_price(symbol, 101.0)
    engine.run_once(now=NOW)
    assert len(db.get_open_paper_positions()) == 1


def test_risk_rejected_blocks_entry(tmp_path: Path) -> None:
    setup = _long_setup(risk_reward_tp1=1.0)  # below RiskConfig.minimum_risk_reward
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


def test_stale_market_data_blocks_entry(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    # Never call market.set_price -> is_data_fresh(symbol) is False
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


# -- duplicate-execution / idempotency -----------------------------------

def test_duplicate_execution_does_not_open_second_position(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    engine.run_once(now=NOW)
    engine.run_once(now=NOW)
    assert len(db.get_open_paper_positions()) == 1


def test_executed_setup_is_marked_in_setups_table(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)

    recent = db.get_recent_setups(limit=1)
    assert len(recent) == 1
    assert dict(recent[0])['execution_status'] == 'EXECUTED'


def test_max_open_positions_blocks_new_entry(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    for other in ('ETHUSDT', 'SOLUSDT', 'BNBUSDT'):
        db.add_paper_position(
            session_id=None, symbol=other, direction='LONG', status='OPEN',
            entry_price=10.0, planned_entry=10.0, quantity_initial=1.0, quantity_remaining=1.0,
            margin_used=5.0, leverage=5, stop_loss=9.0, take_profit_1=11.0, take_profit_2=12.0,
            opened_at=NOW.isoformat(),
        )
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions(symbol=symbol) == []


def test_kill_switch_blocks_new_entries_but_keeps_managing_existing(tmp_path: Path) -> None:
    setups = {'BTCUSDT': _long_setup(symbol='BTCUSDT'), 'ETHUSDT': _long_setup(symbol='ETHUSDT', entry_zone_low=50.0, entry_zone_high=52.0, stop_loss=45.0, take_profit_1=60.0, take_profit_2=70.0)}
    db = _manager(tmp_path)
    db.set_paper_trading_enabled(True)
    market = FakeMarketService()
    analysis_service = FakeAnalysisService()
    setup_engine = DictSetupEngine(setups)
    portfolio = VirtualPortfolio(db)
    risk_manager = RiskManager(db, portfolio)
    engine = PaperTradingEngine(db, portfolio, risk_manager, setup_engine, market, analysis_service, lambda: ['BTCUSDT', 'ETHUSDT'])

    # ETHUSDT has no fresh market data yet (is_data_fresh == False), so this
    # first tick only opens BTCUSDT.
    market.set_price('BTCUSDT', 101.0)
    analysis_service.analysis['BTCUSDT'] = _fake_analysis('BTCUSDT', 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert len(db.get_open_paper_positions()) == 1
    btc_position = dict(db.get_open_paper_positions(symbol='BTCUSDT')[0])

    portfolio.set_kill_switch(True)
    market.set_price('ETHUSDT', 51.0)
    analysis_service.analysis['ETHUSDT'] = _fake_analysis('ETHUSDT', 'LONG', 51.0)
    # ETHUSDT is a fresh READY setup with no open position -> must NOT open now.
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions(symbol='ETHUSDT') == []

    # But the already-open BTCUSDT position must still be managed (TP1 fires).
    market.set_price('BTCUSDT', 110.5)
    engine.run_once(now=NOW)
    btc_position = dict(db.get_paper_position(btc_position['id']))
    assert btc_position['tp1_hit'] == 1


# -- TP1 / TP2 / SL mechanics ---------------------------------------------

def test_tp1_partial_close(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])
    initial_qty = pos['quantity_initial']

    market.set_price(symbol, 110.5)
    engine.run_once(now=NOW)
    pos = dict(db.get_paper_position(pos['id']))
    assert pos['tp1_hit'] == 1
    assert pos['status'] == 'PARTIALLY_CLOSED'
    assert abs(pos['quantity_remaining'] - initial_qty * 0.5) < 1e-9
    assert pos['realized_pnl'] > 0

    trades = db.get_trade_history(source='paper')
    assert len(trades) == 1
    assert dict(trades[0])['exit_reason'] == 'TP1'


def test_tp2_final_close(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])

    market.set_price(symbol, 110.5)
    engine.run_once(now=NOW)
    market.set_price(symbol, 121.0)
    engine.run_once(now=NOW)

    pos = dict(db.get_paper_position(pos['id']))
    assert pos['status'] == 'CLOSED'
    assert pos['tp2_hit'] == 1
    assert pos['quantity_remaining'] == 0.0
    assert db.get_open_paper_positions() == []

    trades = db.get_trade_history(source='paper')
    assert len(trades) == 2
    assert {dict(t)['exit_reason'] for t in trades} == {'TP1', 'TP2'}


def test_long_stop_loss(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])

    market.set_price(symbol, 94.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_paper_position(pos['id']))
    assert pos['status'] == 'STOPPED'
    assert pos['quantity_remaining'] == 0.0
    assert pos['realized_pnl'] < 0


def test_short_stop_loss(tmp_path: Path) -> None:
    setup = _short_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 99.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'SHORT', 99.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])

    market.set_price(symbol, 106.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_paper_position(pos['id']))
    assert pos['status'] == 'STOPPED'
    assert pos['realized_pnl'] < 0


def test_intrabar_ambiguity_resolves_to_stop_loss(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])

    # Live price sits safely inside the range, but the latest CLOSED 15m candle's
    # [low, high] shows both the stop (95) and TP1 (110) were touched.
    ambiguous_candle = [
        {'open': 101, 'high': 103, 'low': 100, 'close': 102},
        {'open': 101, 'high': 111, 'low': 94, 'close': 108},
        {'open': 108, 'high': 109, 'low': 107, 'close': 108},
    ]
    market.set_price(symbol, 108.0, candles_15m=ambiguous_candle)
    engine.run_once(now=NOW)
    pos = dict(db.get_paper_position(pos['id']))
    assert pos['status'] == 'STOPPED', "ambiguous same-candle SL+TP must resolve conservatively to SL"


# -- fees / slippage / pnl -----------------------------------------------

def test_fees_are_recorded_on_close(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    market.set_price(symbol, 110.5)
    engine.run_once(now=NOW)

    trade = dict(db.get_trade_history(source='paper')[0])
    assert trade['fees'] > 0


def test_unrealized_pnl_matches_open_position_math(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])

    unrealized = PaperTradingEngine.calculate_unrealized_pnl(pos, 105.0)
    expected = (105.0 - pos['entry_price']) * pos['quantity_remaining']
    assert abs(unrealized - expected) < 1e-9
    assert unrealized > 0


def test_partial_close_produces_two_pnl_legs_summing_to_position_realized_pnl(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    pos = dict(db.get_open_paper_positions()[0])

    market.set_price(symbol, 110.5)
    engine.run_once(now=NOW)
    market.set_price(symbol, 121.0)
    engine.run_once(now=NOW)

    pos = dict(db.get_paper_position(pos['id']))
    trades = [dict(t) for t in db.get_trade_history(source='paper')]
    assert abs(sum(t['net_pnl'] for t in trades) - pos['realized_pnl']) < 1e-6


def test_portfolio_equity_updates_after_close(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    market.set_price(symbol, 110.5)
    engine.run_once(now=NOW)
    market.set_price(symbol, 121.0)
    engine.run_once(now=NOW)

    state = portfolio.get_state(now=NOW)
    # Total virtual capital (trading equity + spot vault) always tracks total
    # realized PnL 1:1 — Profit Split only decides WHERE the profit sits, not
    # how much of it exists (see trading/virtual_spot_vault.py).
    assert state.total_equity == 500.0 + state.realized_pnl
    assert state.realized_pnl > 0
    assert state.spot_vault_balance > 0
    assert state.current_equity < 500.0 + state.realized_pnl


# -- risk-limit integration -----------------------------------------------

def test_loss_streak_blocks_new_entry(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    for i, pnl in enumerate([-1.0, -1.0, -1.0]):
        db.add_trade(symbol='ZECUSDT', side='SELL', status='closed', net_pnl=pnl, opened_at=f'2026-01-0{i+1}T00:00:00+00:00', closed_at=f'2026-01-0{i+1}T01:00:00+00:00', source='paper')
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


def test_daily_loss_limit_blocks_new_entry(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    db.add_trade(symbol='ZECUSDT', side='SELL', status='closed', net_pnl=-20.0, opened_at='2026-08-12T09:00:00+00:00', closed_at='2026-08-12T10:00:00+00:00', source='paper')
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


def test_weekly_loss_limit_blocks_new_entry(tmp_path: Path) -> None:
    setup = _long_setup()
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    for day, pnl in (('10', -12.0), ('11', -12.0), ('12', -12.0)):
        db.add_trade(symbol='ZECUSDT', side='SELL', status='closed', net_pnl=pnl, opened_at=f'2026-08-{day}T09:00:00+00:00', closed_at=f'2026-08-{day}T10:00:00+00:00', source='paper')
    market.set_price(symbol, 101.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 101.0)
    engine.run_once(now=NOW)
    assert db.get_open_paper_positions() == []


# -- restart recovery -------------------------------------------------------

def test_restart_recovers_open_position_and_continues_managing_it(tmp_path: Path) -> None:
    db_path = str(tmp_path / 'trading.db')
    db = DatabaseManager(db_path)
    db.create_tables()
    position_id = db.add_paper_position(
        session_id=None, symbol='BTCUSDT', direction='LONG', status='OPEN',
        entry_price=100.0, planned_entry=100.0, quantity_initial=1.0, quantity_remaining=1.0,
        margin_used=20.0, leverage=5, stop_loss=95.0, take_profit_1=110.0, take_profit_2=120.0,
        opened_at=NOW.isoformat(), setup_snapshot='{"setup_type": "TREND_PULLBACK", "setup_score": 80}',
    )

    fresh_db = DatabaseManager(db_path)
    fresh_db.create_tables()
    fresh_db.set_paper_trading_enabled(True)
    market = FakeMarketService()
    analysis_service = FakeAnalysisService()
    portfolio = VirtualPortfolio(fresh_db)
    risk_manager = RiskManager(fresh_db, portfolio)
    engine = PaperTradingEngine(fresh_db, portfolio, risk_manager, TradeSetupEngine(), market, analysis_service, lambda: ['BTCUSDT'])

    market.set_price('BTCUSDT', 111.0)
    engine.run_once(now=NOW)

    pos = dict(fresh_db.get_paper_position(position_id))
    assert pos['tp1_hit'] == 1
    assert pos['status'] == 'PARTIALLY_CLOSED'
    assert abs(pos['quantity_remaining'] - 0.5) < 1e-9


# -- fill-time margin re-validation (slippage vs. max_margin_percent) -------
#
# PositionSizeCalculator sizes `quantity` against the theoretical entry price.
# When the margin cap is the binding constraint, the approved quantity puts
# margin_required exactly AT the cap for that theoretical price. Adverse
# slippage on the actual fill then pushes the real notional/margin slightly
# past the cap unless the engine re-validates and shrinks quantity at fill
# time. These tests use a tight stop (1.5% away) so the margin cap — not the
# risk budget — is what PositionSizeCalculator's `min(...)` picks, exercising
# exactly that boundary.

MAX_MARGIN = DEFAULT_RISK_CONFIG.initial_virtual_balance * DEFAULT_RISK_CONFIG.max_margin_percent / 100
RISK_BUDGET = DEFAULT_RISK_CONFIG.initial_virtual_balance * DEFAULT_RISK_CONFIG.risk_per_trade_percent / 100


class ApprovingFixedQuantityRiskManager:
    """Duck-typed RiskManager double that always approves a caller-fixed
    `position_quantity`, bypassing PositionSizeCalculator entirely. Used to
    exercise PaperTradingEngine's OWN fill-time invariant re-check in
    isolation from the normal (already-safe-by-construction) sizing path."""

    def __init__(self, quantity: float) -> None:
        self.quantity = quantity

    def evaluate(self, symbol: str, setup: dict[str, Any], market_status: str = 'LIVE', now: Any = None) -> dict[str, Any]:
        direction = setup['direction']
        entry_price = setup['entry_zone_high'] if direction == 'LONG' else setup['entry_zone_low']
        return {
            'approved': True,
            'symbol': symbol,
            'direction': direction,
            'entry_price': entry_price,
            'stop_loss': setup['stop_loss'],
            'position_quantity': self.quantity,
        }


def test_long_fill_slippage_never_breaches_margin_cap(tmp_path: Path) -> None:
    setup = _long_setup(
        entry_zone_low=99.0, entry_zone_high=100.0, stop_loss=98.5,
        take_profit_1=105.0, take_profit_2=110.0,
        risk_reward_tp1=3.3, risk_reward_tp2=6.6,
    )
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 100.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 100.0)
    engine.run_once(now=NOW)

    positions = db.get_open_paper_positions()
    assert len(positions) == 1
    pos = dict(positions[0])
    # Adverse LONG-entry slippage pushes the fill above the quoted price, which
    # is exactly the direction that used to push margin_used past MAX_MARGIN.
    assert pos['entry_price'] > 100.0
    assert pos['margin_used'] <= MAX_MARGIN + 1e-6


def test_short_fill_slippage_stays_within_margin_cap(tmp_path: Path) -> None:
    setup = _short_setup(
        entry_zone_low=100.0, entry_zone_high=101.0, stop_loss=101.5,
        take_profit_1=95.0, take_profit_2=90.0,
        risk_reward_tp1=3.3, risk_reward_tp2=6.6,
    )
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 100.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'SHORT', 100.0)
    engine.run_once(now=NOW)

    positions = db.get_open_paper_positions()
    assert len(positions) == 1
    pos = dict(positions[0])
    # Adverse SHORT-entry slippage pushes the fill BELOW the quoted price, which
    # shrinks notional/margin for a fixed quantity — never needs a resize.
    assert pos['entry_price'] < 100.0
    assert pos['margin_used'] <= MAX_MARGIN + 1e-6


def test_quantity_is_reduced_when_slippage_would_breach_margin_cap(tmp_path: Path) -> None:
    setup = _long_setup(
        entry_zone_low=99.0, entry_zone_high=100.0, stop_loss=98.5,
        take_profit_1=105.0, take_profit_2=110.0,
        risk_reward_tp1=3.3, risk_reward_tp2=6.6,
    )
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    risk_manager = RiskManager(db, portfolio)
    planned = risk_manager.evaluate(symbol, setup, market_status='LIVE', now=NOW)
    planned_quantity = planned['position_quantity']
    assert planned_quantity > 0
    # By construction (margin cap binds for this stop distance), the planned
    # quantity prices margin_required exactly at MAX_MARGIN for the theoretical
    # entry price — any adverse slippage on top of it must force a shrink.
    assert planned['margin_required'] <= MAX_MARGIN + 1e-6

    market.set_price(symbol, 100.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 100.0)
    engine.run_once(now=NOW)

    pos = dict(db.get_open_paper_positions()[0])
    assert pos['quantity_initial'] < planned_quantity
    assert pos['margin_used'] <= MAX_MARGIN + 1e-6


def test_planned_loss_remains_within_risk_budget_after_resize(tmp_path: Path) -> None:
    setup = _long_setup(
        entry_zone_low=99.0, entry_zone_high=100.0, stop_loss=98.5,
        take_profit_1=105.0, take_profit_2=110.0,
        risk_reward_tp1=3.3, risk_reward_tp2=6.6,
    )
    db, market, analysis, portfolio, engine, symbol = _build(tmp_path, setup)
    market.set_price(symbol, 100.0)
    analysis.analysis[symbol] = _fake_analysis(symbol, 'LONG', 100.0)
    engine.run_once(now=NOW)

    pos = dict(db.get_open_paper_positions()[0])
    config = DEFAULT_RISK_CONFIG
    stop_distance_percent = abs(pos['entry_price'] - pos['stop_loss']) / pos['entry_price'] * 100
    loss_rate_percent = stop_distance_percent + config.estimated_fee_percent + config.estimated_slippage_percent
    planned_loss_at_stop = pos['quantity_initial'] * pos['entry_price'] * (loss_rate_percent / 100)
    assert planned_loss_at_stop <= RISK_BUDGET + 1e-6


def test_impossible_safe_fill_is_rejected(tmp_path: Path) -> None:
    """Even if an upstream sizing bug approved a wildly oversized quantity,
    PaperTradingEngine's own fill-time invariant re-check must refuse to open
    a position for which no quantity is simultaneously safe on margin AND
    within the per-trade risk budget (here: stop is 10% away, so shrinking
    quantity down to the margin cap still risks far more than risk_budget)."""
    setup = _long_setup(
        entry_zone_low=99.0, entry_zone_high=100.0, stop_loss=90.0,
        take_profit_1=115.0, take_profit_2=130.0,
    )
    db = _manager(tmp_path)
    db.set_paper_trading_enabled(True)
    market = FakeMarketService()
    analysis_service = FakeAnalysisService()
    setup_engine = FixedSetupEngine(setup)
    portfolio = VirtualPortfolio(db)
    fake_risk_manager = ApprovingFixedQuantityRiskManager(quantity=10.0)
    engine = PaperTradingEngine(db, portfolio, fake_risk_manager, setup_engine, market, analysis_service, lambda: [setup['symbol']])

    market.set_price(setup['symbol'], 100.0)
    analysis_service.analysis[setup['symbol']] = _fake_analysis(setup['symbol'], 'LONG', 100.0)
    engine.run_once(now=NOW)

    assert db.get_open_paper_positions() == []
