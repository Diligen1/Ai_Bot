"""Seed script for trading database."""
from datetime import datetime, timedelta
from database.db import DatabaseManager


def seed_test_trades(db: DatabaseManager) -> None:
    db.create_tables()
    if db.get_setting('test_seed_initialized') == 'true':
        return
    if len(db.get_trade_history()) >= 5:
        db.set_setting('test_seed_initialized', 'true')
        return

    now = datetime.utcnow()
    seed_trades = [
        {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'status': 'closed',
            'entry_price': 30000.0,
            'exit_price': 30150.0,
            'quantity': 0.01,
            'leverage': 5.0,
            'margin_used': 60.0,
            'realized_pnl': 15.0,
            'net_pnl': 12.0,
            'fees': 0.3,
            'funding': 0.0,
            'risk_reward': 1.5,
            'signal_score': 70.0,
            'ai_score': 65.0,
            'historical_score': 68.0,
            'market_regime': 'BULL',
            'entry_reason': 'Long breakout',
            'exit_reason': 'Profit target',
            'opened_at': (now - timedelta(days=5, hours=6)).isoformat(),
            'closed_at': (now - timedelta(days=5, hours=2)).isoformat(),
        },
        {
            'symbol': 'ETHUSDT',
            'side': 'SELL',
            'status': 'closed',
            'entry_price': 1900.0,
            'exit_price': 1850.0,
            'quantity': 0.2,
            'leverage': 5.0,
            'margin_used': 76.0,
            'realized_pnl': 10.0,
            'net_pnl': 8.0,
            'fees': 0.2,
            'funding': 0.0,
            'risk_reward': 1.4,
            'signal_score': 60.0,
            'ai_score': 55.0,
            'historical_score': 58.0,
            'market_regime': 'BEAR',
            'entry_reason': 'Short momentum',
            'exit_reason': 'Trend continuation',
            'opened_at': (now - timedelta(days=4, hours=8)).isoformat(),
            'closed_at': (now - timedelta(days=4, hours=3)).isoformat(),
        },
        {
            'symbol': 'SOLUSDT',
            'side': 'BUY',
            'status': 'closed',
            'entry_price': 22.0,
            'exit_price': 20.0,
            'quantity': 1.0,
            'leverage': 3.0,
            'margin_used': 7.33,
            'realized_pnl': -2.0,
            'net_pnl': -1.7,
            'fees': 0.05,
            'funding': 0.0,
            'risk_reward': 0.7,
            'signal_score': 52.0,
            'ai_score': 50.0,
            'historical_score': 51.0,
            'market_regime': 'SIDEWAYS',
            'entry_reason': 'Range buy',
            'exit_reason': 'Stop loss',
            'opened_at': (now - timedelta(days=3, hours=12)).isoformat(),
            'closed_at': (now - timedelta(days=3, hours=4)).isoformat(),
        },
        {
            'symbol': 'BTCUSDT',
            'side': 'SELL',
            'status': 'closed',
            'entry_price': 31000.0,
            'exit_price': 30700.0,
            'quantity': 0.005,
            'leverage': 5.0,
            'margin_used': 31.0,
            'realized_pnl': 15.0,
            'net_pnl': 12.7,
            'fees': 0.15,
            'funding': 0.0,
            'risk_reward': 1.8,
            'signal_score': 68.0,
            'ai_score': 64.0,
            'historical_score': 66.0,
            'market_regime': 'BEAR',
            'entry_reason': 'Short reversal',
            'exit_reason': 'Profit target',
            'opened_at': (now - timedelta(days=2, hours=10)).isoformat(),
            'closed_at': (now - timedelta(days=2, hours=1)).isoformat(),
        },
        {
            'symbol': 'ETHUSDT',
            'side': 'BUY',
            'status': 'closed',
            'entry_price': 1950.0,
            'exit_price': 1965.0,
            'quantity': 0.2,
            'leverage': 4.0,
            'margin_used': 97.5,
            'realized_pnl': -5.0,
            'net_pnl': -4.3,
            'fees': 0.2,
            'funding': 0.0,
            'risk_reward': 0.9,
            'signal_score': 58.0,
            'ai_score': 57.0,
            'historical_score': 58.0,
            'market_regime': 'BULL',
            'entry_reason': 'Long dip',
            'exit_reason': 'Stop loss',
            'opened_at': (now - timedelta(days=1, hours=12)).isoformat(),
            'closed_at': (now - timedelta(days=1, hours=2)).isoformat(),
        },
    ]
    for trade in seed_trades:
        db.add_trade(**trade)
    db.set_setting('test_seed_initialized', 'true')


if __name__ == '__main__':
    db = DatabaseManager()
    seed_test_trades(db)
    print('Seeded test trades into', db.db_path)
