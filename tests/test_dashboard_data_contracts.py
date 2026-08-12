from pathlib import Path

import app.dashboard_data as dashboard_data
from app.dashboard_data import _serialize_setup
from database.db import DatabaseManager


def test_serialize_setup_empty_dict_defaults() -> None:
    result = _serialize_setup({})
    assert result['setup_type'] == 'NO_SETUP'
    assert result['status'] == 'REJECTED'
    assert result['direction'] == 'NEUTRAL'
    assert result['reasons'] == []
    assert result['rejection_reasons'] == []


def test_serialize_setup_preserves_provided_values() -> None:
    setup = {
        'symbol': 'ETHUSDT', 'direction': 'SHORT', 'setup_type': 'BREAKOUT_RETEST',
        'status': 'READY', 'setup_score': 77,
    }
    result = _serialize_setup(setup)
    assert result['symbol'] == 'ETHUSDT'
    assert result['direction'] == 'SHORT'
    assert result['setup_type'] == 'BREAKOUT_RETEST'
    assert result['status'] == 'READY'
    assert result['setup_score'] == 77


def test_persist_setup_writes_to_database(tmp_path: Path, monkeypatch) -> None:
    tmp_db = DatabaseManager(str(tmp_path / 'trading.db'))
    tmp_db.create_tables()
    monkeypatch.setattr(dashboard_data, '_db', tmp_db)

    setup = {
        'direction': 'LONG', 'setup_type': 'TREND_PULLBACK', 'status': 'READY',
        'entry_zone_low': 100.0, 'entry_zone_high': 102.0, 'stop_loss': 95.0,
        'take_profit_1': 110.0, 'take_profit_2': 115.0,
        'risk_reward_tp1': 2.0, 'risk_reward_tp2': 3.0, 'invalidation_level': 95.0,
        'confidence': 'HIGH', 'technical_score': 70, 'setup_score': 80,
        'created_at': '2026-01-01T00:00:00+00:00', 'expires_at': '2026-01-01T01:00:00+00:00',
        'rejection_reasons': [], 'analysis_snapshot': {},
    }
    dashboard_data._persist_setup('BTCUSDT', setup)

    recent = tmp_db.get_recent_setups()
    assert len(recent) == 1
    assert recent[0]['symbol'] == 'BTCUSDT'
    assert recent[0]['direction'] == 'LONG'
    assert recent[0]['status'] == 'READY'
