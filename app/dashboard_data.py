"""Dashboard data service backed by SQLite."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import json

from config import analysis_settings as analysis_settings
from database.db import DatabaseManager
from market.analysis_engine import MarketAnalysisService
from market.market_data_service import MarketDataService
from market.setup_engine import TradeSetupEngine

_db = DatabaseManager()
_db.create_tables()

_whitelist_symbols = [row['symbol'] for row in _db.get_whitelist_symbols()]
_market_service = MarketDataService(_whitelist_symbols)
_market_analysis_service = MarketAnalysisService(_market_service)
_setup_engine = TradeSetupEngine()


def get_nav_items() -> list[dict[str, str]]:
    return [
        {'label': 'Dashboard', 'href': '/'},
        {'label': 'Market', 'href': '/market'},
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


def get_market_updated() -> str:
    symbol_data = get_market_data().get('BTCUSDT', {})
    updated = symbol_data.get('last_update')
    if hasattr(updated, 'strftime'):
        return updated.strftime('%d.%m.%Y %H:%M:%S')
    return str(updated or '-')


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


def format_timestamp(timestamp: str | None) -> str:
    if not timestamp:
        return '-'
    try:
        normalized = timestamp.replace('Z', '+00:00') if timestamp.endswith('Z') else timestamp
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime('%d.%m.%Y %H:%M:%S')
    except Exception:
        return str(timestamp)


def get_open_positions() -> list[dict[str, str]]:
    positions = _db.get_open_trades()
    if not positions:
        return []
    return [
        {
            **dict(row),
            'opened_at': format_timestamp(row['opened_at']),
        }
        for row in positions
    ]


def get_ai_market_analysis() -> dict[str, str]:
    analysis = _market_analysis_service.get_analysis('BTCUSDT')
    if not analysis:
        return {
            'symbol': 'BTCUSDT',
            'final_score': '0 / 100',
            'decision': 'ЖДАТЬ',
            'reason': 'Анализ пока не готов',
        }

    return {
        'symbol': analysis['symbol'],
        'final_score': f"{analysis['technical_score']} / 100",
        'decision': 'ЛОНГ' if analysis['direction'] == 'LONG' else 'ШОРТ' if analysis['direction'] == 'SHORT' else 'НЕЙТРАЛЬНО',
        'reason': ', '.join(analysis['reasons'][:2]) if analysis['reasons'] else 'Без явных показаний',
    }


def get_trades_history() -> list[dict[str, str]]:
    trades = _db.get_trade_history()
    return [
        {
            **dict(row),
            'opened_at': format_timestamp(row['opened_at']),
            'closed_at': format_timestamp(row['closed_at']),
        }
        for row in trades
    ]


def get_analytics_data() -> dict[str, str]:
    return _db.get_analytics_stats()


def get_whitelist_symbols() -> list[dict[str, str]]:
    return [dict(row) for row in _db.get_whitelist_symbols()]


def add_whitelist_symbol(symbol: str) -> int:
    if not _market_service.validate_symbol(symbol):
        raise ValueError(f"Symbol {symbol} is not valid for USD-M Futures")
    symbol_id = _db.add_whitelist_symbol(symbol)
    _market_service.set_symbols([row['symbol'] for row in _db.get_whitelist_symbols()])
    return symbol_id


def remove_whitelist_symbol(symbol: str) -> None:
    _db.remove_whitelist_symbol(symbol)
    _market_service.set_symbols([row['symbol'] for row in _db.get_whitelist_symbols()])


def _serialize_setup(setup: dict[str, Any]) -> dict[str, Any]:
    return {
        'symbol': setup.get('symbol', 'UNKNOWN'),
        'direction': setup.get('direction', 'NEUTRAL'),
        'setup_type': setup.get('setup_type', 'NO_SETUP'),
        'entry_zone_low': setup.get('entry_zone_low', 0.0),
        'entry_zone_high': setup.get('entry_zone_high', 0.0),
        'stop_loss': setup.get('stop_loss', 0.0),
        'take_profit_1': setup.get('take_profit_1', 0.0),
        'take_profit_2': setup.get('take_profit_2', 0.0),
        'risk_reward_tp1': setup.get('risk_reward_tp1', 0.0),
        'risk_reward_tp2': setup.get('risk_reward_tp2', 0.0),
        'invalidation_level': setup.get('invalidation_level', 0.0),
        'confidence': setup.get('confidence', 'LOW'),
        'technical_score': setup.get('technical_score', 0),
        'setup_score': setup.get('setup_score', 0),
        'status': setup.get('status', 'REJECTED'),
        'created_at': setup.get('created_at', ''),
        'expires_at': setup.get('expires_at', ''),
        'reasons': setup.get('reasons', []),
        'rejection_reasons': setup.get('rejection_reasons', []),
    }


def _persist_setup(symbol: str, setup: dict[str, Any]) -> None:
    try:
        _db.add_setup(
            symbol=symbol,
            direction=setup.get('direction', 'NEUTRAL'),
            setup_type=setup.get('setup_type', 'NO_SETUP'),
            entry_zone_low=setup.get('entry_zone_low'),
            entry_zone_high=setup.get('entry_zone_high'),
            stop_loss=setup.get('stop_loss'),
            take_profit_1=setup.get('take_profit_1'),
            take_profit_2=setup.get('take_profit_2'),
            risk_reward_tp1=setup.get('risk_reward_tp1'),
            risk_reward_tp2=setup.get('risk_reward_tp2'),
            invalidation_level=setup.get('invalidation_level'),
            confidence=setup.get('confidence', 'LOW'),
            technical_score=setup.get('technical_score'),
            setup_score=setup.get('setup_score'),
            status=setup.get('status', 'REJECTED'),
            created_at=setup.get('created_at'),
            expires_at=setup.get('expires_at'),
            rejection_reasons=json.dumps(setup.get('rejection_reasons', []), ensure_ascii=False),
            analysis_snapshot=json.dumps(setup.get('analysis_snapshot', {}), ensure_ascii=False),
        )
    except Exception:
        pass


def get_market_data() -> dict[str, dict[str, Any]]:
    raw = _market_service.get_all_data()
    result: dict[str, dict[str, Any]] = {}
    for symbol, data in raw.items():
        analysis = _market_analysis_service.get_analysis(symbol) or {}
        symbol_data = _market_service.get_symbol_data(symbol) or {}
        setup = _setup_engine.build_setup(analysis, symbol_data) if analysis else {'setup_type': 'NO_SETUP', 'status': 'REJECTED'}
        if analysis.get('trade_candidate'):
            _persist_setup(symbol, setup)
        result[symbol] = {
            'last_price': f"{data.get('last_price', '-')}",
            'mark_price': f"{data.get('mark_price', '-')}",
            'price_change_percent': f"{data.get('price_change_percent', '-')}",
            'volume': f"{data.get('volume', '-')}",
            'funding_rate': f"{data.get('funding_rate', '-')}",
            'open_interest': f"{data.get('open_interest', '-')}",
            'best_bid': f"{data.get('best_bid', '-')}",
            'best_ask': f"{data.get('best_ask', '-')}",
            'spread': f"{data.get('spread', '-')}",
            'status': data.get('status', 'STALE'),
            'last_update': data.get('last_update', '-'),
            'analysis': analysis,
            'trend_4h': analysis.get('analysis', {}).get('4h', {}).get('trend', 'UNKNOWN'),
            'trend_1h': analysis.get('analysis', {}).get('1h', {}).get('trend', 'UNKNOWN'),
            'trend_15m': analysis.get('analysis', {}).get('15m', {}).get('trend', 'UNKNOWN'),
            'market_regime': analysis.get('market_regime', 'UNKNOWN'),
            'direction': analysis.get('direction', 'NEUTRAL'),
            'technical_score': analysis.get('technical_score', 0),
            'trade_candidate': analysis.get('trade_candidate', False),
            'setup': _serialize_setup(setup),
        }
    return result


def get_market_symbols() -> list[str]:
    return [row['symbol'] for row in _db.get_whitelist_symbols()]


def get_brain_view(symbol: str | None = None) -> dict[str, Any]:
    symbol = symbol or 'BTCUSDT'
    analysis = _market_analysis_service.get_analysis(symbol) or {}
    market_data = _market_service.get_symbol_data(symbol) or {}
    setup = _setup_engine.build_setup(analysis, market_data) if analysis else {'setup_type': 'NO_SETUP', 'status': 'REJECTED', 'reasons': [], 'rejection_reasons': []}
    last_price = market_data.get('last_price')
    if analysis.get('trade_candidate'):
        _persist_setup(symbol, setup)
    return {
        'symbol': symbol,
        'current_price': f"{last_price}" if last_price is not None else '-',
        'market_regime': analysis.get('market_regime', 'UNKNOWN'),
        'direction': analysis.get('direction', 'NEUTRAL'),
        'technical_score': str(analysis.get('technical_score', 0)),
        'confidence': analysis.get('confidence', 'LOW'),
        'timeframe_alignment': analysis.get('timeframe_alignment', 'NEUTRAL'),
        'trade_candidate': 'ДА' if analysis.get('trade_candidate') else 'НЕТ',
        'reasons': analysis.get('reasons', []),
        'block_reasons': analysis.get('block_reasons', []),
        'analysis_4h': analysis.get('analysis', {}).get('4h', {}),
        'analysis_1h': analysis.get('analysis', {}).get('1h', {}),
        'analysis_15m': analysis.get('analysis', {}).get('15m', {}),
        'volume_current': analysis.get('volume_current', '-'),
        'volume_average': analysis.get('volume_average', '-'),
        'volume_ratio': analysis.get('volume_ratio', '-'),
        'atr': analysis.get('atr', '-'),
        'atr_percent': f"{analysis.get('atr_percent', '-'):.2f}" if isinstance(analysis.get('atr_percent'), float) else '-',
        'setup': _serialize_setup(setup),
    }
