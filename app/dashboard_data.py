"""Dashboard data service backed by SQLite."""

from __future__ import annotations

from database.db import DatabaseManager

_db = DatabaseManager()
_db.create_tables()


def get_nav_items() -> list[dict[str, str]]:
    return [
        {'label': 'Dashboard', 'href': '/'},
        {'label': 'Trades', 'href': '/trades'},
        {'label': 'Analytics', 'href': '/analytics'},
        {'label': 'AI Brain', 'href': '/brain'},
        {'label': 'Symbols', 'href': '/symbols'},
    ]


def get_system_status() -> dict[str, str]:
    return {
        'bot_status': 'OFFLINE',
        'binance': 'NOT CONNECTED',
        'ai_analyzer': 'NOT CONNECTED',
        'trading_mode': 'TEST',
    }


def get_balance() -> dict[str, str]:
    stats = _db.get_dashboard_stats()
    return {
        'futures_balance': stats['futures_balance'],
        'spot_vault': stats['spot_vault'],
        'total_equity': stats['total_equity'],
    }


def get_today_stats() -> dict[str, str]:
    stats = _db.get_dashboard_stats()
    return {
        'pnl': stats['pnl'],
        'pnl_percent': stats['pnl_percent'],
        'trades': stats['trades'],
        'wins': stats['wins'],
        'losses': stats['losses'],
    }


def get_risk_control() -> dict[str, str]:
    return {
        'leverage': 'x5',
        'max_margin_per_trade': '10%',
        'risk_per_trade': '0.75%',
        'daily_drawdown_limit': '2.5%',
        'weekly_drawdown_limit': '5%',
        'consecutive_losses': '0 / 3',
        'kill_switch': 'OK',
    }


def get_open_positions() -> list[dict[str, str]]:
    positions = _db.get_open_trades()
    if not positions:
        return []
    return [dict(row) for row in positions]


def get_ai_market_analysis() -> dict[str, str]:
    return {
        'symbol': 'BTCUSDT',
        'final_score': '0 / 100',
        'decision': 'WAIT',
        'reason': 'No market data connected',
    }


def get_trades_history() -> list[dict[str, str]]:
    trades = _db.get_trade_history()
    return [dict(row) for row in trades]


def get_analytics_data() -> dict[str, str]:
    return _db.get_analytics_stats()


def get_whitelist_symbols() -> list[dict[str, str]]:
    return [dict(row) for row in _db.get_whitelist_symbols()]


def add_whitelist_symbol(symbol: str) -> int:
    return _db.add_whitelist_symbol(symbol)


def remove_whitelist_symbol(symbol: str) -> None:
    _db.remove_whitelist_symbol(symbol)


def get_brain_view() -> dict[str, str]:
    return {
        'symbol': 'BTCUSDT',
        'trend_4h': 'UNKNOWN',
        'trend_1h': 'UNKNOWN',
        'trend_15m': 'UNKNOWN',
        'rsi': '-',
        'volume': '-',
        'open_interest': '-',
        'funding': '-',
        'technical_score': '0',
        'historical_score': '0',
        'ai_score': '0',
        'final_score': '0 / 100',
        'decision': 'WAIT',
    }
