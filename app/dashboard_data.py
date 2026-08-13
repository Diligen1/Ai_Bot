"""Dashboard data service backed by SQLite."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import json

from ai.analyzer import AIAnalyzer
from binance.account_client import BinanceAccountClient
from binance.account_connector import BinanceAccountConnector
from binance.symbol_rules import BinanceSymbolRules
from binance.trading_client import LiveTradingClient
from config import analysis_settings as analysis_settings
from config.env import get_env
from database.db import DatabaseManager
from execution.live_execution_engine import DRY_RUN_EXECUTION, LiveExecutionEngine
from execution.live_trading_engine import LiveTradingEngine
from execution.live_trading_runtime import LiveTradingRuntime
from execution.order_intent import OrderIntent
from market.analysis_engine import MarketAnalysisService
from market.market_data_service import MarketDataService
from market.setup_engine import TradeSetupEngine
from risk.live_risk_guard import LiveRiskGuard
from risk.risk_manager import RiskManager
from trading.paper_trading_engine import PaperTradingEngine
from trading.virtual_portfolio import VirtualPortfolio
from trading.virtual_spot_vault import VirtualSpotVault

_db = DatabaseManager()
_db.create_tables()

_whitelist_symbols = [row['symbol'] for row in _db.get_whitelist_symbols()]
_market_service = MarketDataService(_whitelist_symbols)
_market_analysis_service = MarketAnalysisService(_market_service)
_setup_engine = TradeSetupEngine()
_portfolio = VirtualPortfolio(_db)
_risk_manager = RiskManager(_db, _portfolio)
_spot_vault = VirtualSpotVault(_db)
_ai_analyzer = AIAnalyzer()
_binance_account = BinanceAccountConnector()
_live_risk_guard = LiveRiskGuard(_binance_account, _db)
_symbol_rules = BinanceSymbolRules()
_live_execution_engine = LiveExecutionEngine(_live_risk_guard, _symbol_rules, _db, _market_service)


def _build_binance_credentialed_client(factory):
    api_key = get_env('BINANCE_API_KEY')
    api_secret = get_env('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        return None
    return factory(api_key=api_key, api_secret=api_secret)


# V2 execution — LIVE_TRADING_ENABLED (env, default false) gates every real
# order inside LiveTradingEngine itself; constructing these clients only
# holds credentials in memory, it never sends a request on its own.
_account_client_for_leverage = _build_binance_credentialed_client(BinanceAccountClient)
_live_trading_client = _build_binance_credentialed_client(LiveTradingClient)
_live_trading_engine = LiveTradingEngine(_live_trading_client, _account_client_for_leverage, _live_risk_guard, _live_execution_engine, _db)


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
    """Virtual capital, derived only from PAPER trades (seed/demo history excluded)."""
    state = _portfolio.get_state()
    unrealized = sum(_unrealized_pnl_for(dict(row)) for row in _db.get_open_paper_positions())
    return {
        'starting_balance': f'{state.starting_balance:.2f} USDT',
        'current_equity': f'{state.current_equity:.2f} USDT',
        'realized_pnl': f'{state.realized_pnl:.2f} USDT',
        'unrealized_pnl': f'{unrealized:.2f} USDT',
        'reserved_margin': f'{state.reserved_margin:.2f} USDT',
        'available_equity': f'{state.available_equity:.2f} USDT',
    }


def get_today_stats() -> dict[str, str]:
    state = _portfolio.get_state()
    today = datetime.now(timezone.utc).date().isoformat()
    todays_trades = [
        row for row in _db.get_trade_history(source='paper')
        if row['closed_at'] and row['closed_at'].startswith(today)
    ]
    wins = sum(1 for row in todays_trades if (row['net_pnl'] or 0.0) > 0)
    losses = sum(1 for row in todays_trades if (row['net_pnl'] or 0.0) <= 0)
    pnl_percent = (state.daily_realized_pnl / state.day_start_equity * 100) if state.day_start_equity else 0.0
    return {
        'pnl': f'{state.daily_realized_pnl:.2f} USDT',
        'pnl_percent': f'{pnl_percent:.2f}%',
        'trades': str(len(todays_trades)),
        'wins': str(wins),
        'losses': str(losses),
    }


def get_risk_control() -> dict[str, str]:
    state = _portfolio.get_state()
    config = _risk_manager.config
    kill_switch_on = _portfolio.is_kill_switch_on()
    return {
        'leverage': f'x{config.leverage}',
        'max_margin_per_trade': f'{config.max_margin_percent:.0f}%',
        'risk_per_trade': f'{config.risk_per_trade_percent:.0f}%',
        'daily_drawdown_limit': f'{config.daily_loss_limit_percent:.0f}%',
        'weekly_drawdown_limit': f'{config.weekly_loss_limit_percent:.0f}%',
        'consecutive_losses': f'{state.consecutive_losses} / {config.max_consecutive_losses}',
        'open_positions_count': f'{state.open_positions_count} / {config.max_open_positions}',
        'kill_switch': 'ВКЛ' if kill_switch_on else 'ВЫКЛ',
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


def get_trades_history(source: str | None = None) -> list[dict[str, str]]:
    trades = _db.get_trade_history(source=source)
    result = []
    for row in trades:
        trade = dict(row)
        split = _spot_vault.get_split_for_trade(trade['id']) if trade.get('source') == 'paper' else None
        result.append({
            **trade,
            'opened_at': format_timestamp(trade['opened_at']),
            'closed_at': format_timestamp(trade['closed_at']),
            'profit_split_spot': f"{split['spot_amount']:.2f} USDT" if split else None,
            'profit_split_futures': f"{split['futures_amount']:.2f} USDT" if split else None,
        })
    return result


def get_analytics_data() -> dict[str, str]:
    """PAPER-only performance — seed/demo history must not be mixed in."""
    return _db.get_analytics_stats(source='paper')


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


_paper_engine = PaperTradingEngine(
    _db, _portfolio, _risk_manager, _setup_engine,
    _market_service, _market_analysis_service, get_market_symbols,
    spot_vault=_spot_vault,
)
_paper_engine.start()

# The ONLY automated caller of LiveTradingEngine.attempt_entry() in the
# project (see execution/live_trading_runtime.py) — fully separate from
# _paper_engine above. Starts unconditionally with the app (Dashboard-
# independent); LIVE_TRADING_ENABLED (env, default false) is re-checked
# by attempt_entry() itself on every single symbol, every cycle.
_live_trading_runtime = LiveTradingRuntime(
    _live_trading_engine, _setup_engine, _market_service,
    _market_analysis_service, get_market_symbols,
)
_live_trading_runtime.start()


def get_binance_account_view() -> dict[str, Any]:
    """REAL Binance futures account snapshot — READ-ONLY, never mixed with
    VirtualPortfolio/paper capital (see get_balance()/get_spot_vault_view(),
    which are entirely separate and never read this data)."""
    snapshot = _binance_account.get_snapshot()
    return {
        'status': snapshot.status,
        'enabled': _binance_account.is_enabled,
        'wallet_balance': f'{snapshot.wallet_balance:.2f} USDT',
        'available_balance': f'{snapshot.available_balance:.2f} USDT',
        'unrealized_pnl': f'{snapshot.unrealized_pnl:.2f} USDT',
        'error_message': snapshot.error_message,
        'positions': [
            {
                'symbol': position.symbol,
                'position_amt': f'{position.position_amt:.6f}',
                'entry_price': f'{position.entry_price:.4f}',
                'unrealized_pnl': f'{position.unrealized_pnl:.2f} USDT',
                'leverage': f'x{position.leverage}',
                'position_side': position.position_side,
            }
            for position in snapshot.positions
        ],
    }


def get_live_risk_view() -> dict[str, Any]:
    """Live Account Risk Guard — READ-ONLY risk math over the REAL Binance
    account (see risk/live_risk_guard.py). Never mixed with VirtualPortfolio;
    computation only, no order is ever placed from this data."""
    snapshot = _live_risk_guard.get_snapshot()
    return {
        'status': snapshot.status,
        'real_equity': f'{snapshot.real_equity:.2f} USDT',
        'available_balance': f'{snapshot.available_balance:.2f} USDT',
        'unrealized_pnl': f'{snapshot.unrealized_pnl:.2f} USDT',
        'max_allowed_margin': f'{snapshot.max_allowed_margin:.2f} USDT',
        'risk_budget': f'{snapshot.risk_budget:.2f} USDT',
        'open_positions': f'{snapshot.open_positions_count} / {snapshot.max_positions_limit}',
        'current_exposure': f'{snapshot.current_exposure:.2f} USDT',
        'kill_switch': 'ВКЛ' if snapshot.kill_switch_on else 'ВЫКЛ',
        'leverage_limit': f'x{snapshot.leverage_limit}',
        'error_message': snapshot.error_message,
    }


def _serialize_order_intent(intent: OrderIntent) -> dict[str, Any]:
    return {
        'status': intent.status,
        'symbol': intent.symbol,
        'side': intent.side,
        'position_side': intent.position_side,
        'quantity': f'{intent.quantity:.6f}',
        'entry_price': f'{intent.entry_price:.4f}',
        'stop_loss': f'{intent.stop_loss:.4f}',
        'take_profit_1': f'{intent.take_profit_1:.4f}',
        'take_profit_2': f'{intent.take_profit_2:.4f}',
        'leverage': f'x{intent.leverage}',
        'estimated_margin': f"{intent.risk_decision_snapshot['margin_required']:.2f} USDT" if intent.risk_decision_snapshot else '-',
        'planned_risk': f"{intent.risk_decision_snapshot['planned_loss']:.2f} USDT" if intent.risk_decision_snapshot else '-',
        'rejection_reasons': intent.rejection_reasons,
    }


def get_live_execution_view() -> dict[str, Any]:
    """Live Execution Engine V1 — DRY RUN only (execution/live_execution_engine.py).
    Builds (never sends) a PREVIEW OrderIntent for one representative symbol
    on each Dashboard render, exactly like get_ai_market_analysis() already
    does for BTCUSDT — this is NOT the live runtime (see
    execution/live_trading_runtime.py / get_live_trading_view() for the
    actual multi-symbol runtime state); it is only a manual, single-symbol
    dry-run preview, rebuilt fresh every page load."""
    whitelist = get_market_symbols()
    symbol = 'BTCUSDT' if 'BTCUSDT' in whitelist else (whitelist[0] if whitelist else None)

    view: dict[str, Any] = {'dry_run': DRY_RUN_EXECUTION, 'preview': True, 'intent': None}
    if symbol is None:
        return view

    analysis = _market_analysis_service.get_analysis(symbol) or {}
    market_data = _market_service.get_symbol_data(symbol) or {}
    setup = _setup_engine.build_setup(analysis, market_data) if analysis else {'setup_type': 'NO_SETUP', 'status': 'REJECTED'}

    intent = _live_execution_engine.build_order_intent(symbol, setup)
    view['intent'] = _serialize_order_intent(intent)
    return view


def _serialize_execution_result(result: Any) -> dict[str, Any]:
    return {
        'status': result.status,
        'symbol': result.symbol,
        'entry_order_id': result.entry_order_id,
        'sl_order_id': result.sl_order_id,
        'tp1_order_id': result.tp1_order_id,
        'tp2_order_id': result.tp2_order_id,
        'reasons': result.reasons,
        'error_message': result.error_message,
    }


def get_live_trading_view() -> dict[str, Any]:
    """Live Trading Runtime (execution/live_trading_runtime.py) — DISPLAY
    ONLY. This view only ever READS the runtime's own state; it never calls
    attempt_entry() or start()/stop() itself, and the Dashboard is never
    required for the runtime to run (see run.py -> app.dashboard_data module
    import -> _live_trading_runtime.start()). LIVE_TRADING_ENABLED can only
    be changed manually via .env — there is intentionally no toggle."""
    last_result = _live_trading_engine.get_last_execution_result()
    last_ready = _live_trading_runtime.get_last_ready_setup()
    last_cycle_at = _live_trading_runtime.get_last_cycle_at()
    return {
        'runtime_running': _live_trading_runtime.is_running(),
        'runtime_status_label': 'RUNNING' if _live_trading_runtime.is_running() else 'STOPPED',
        'enabled': _live_trading_engine.is_live_trading_enabled(),
        'status_label': 'ВКЛ' if _live_trading_engine.is_live_trading_enabled() else 'ВЫКЛ',
        'kill_switch': 'ВКЛ' if _live_trading_engine.is_live_kill_switch_on() else 'ВЫКЛ',
        # Read-only check — does NOT attempt recovery itself (that only ever
        # happens inside attempt_entry()); this is purely a status read.
        'has_unprotected_position': _live_trading_engine.has_unprotected_position(),
        'last_cycle_at': format_timestamp(last_cycle_at) if last_cycle_at else None,
        'symbols_checked': _live_trading_runtime.get_last_symbols_checked(),
        'symbols_checked_count': len(_live_trading_runtime.get_last_symbols_checked()),
        'last_checked_symbol': _live_trading_runtime.get_last_checked_symbol(),
        'last_ready_setup': {
            'symbol': last_ready.symbol,
            'setup_type': last_ready.setup_type,
            'direction': last_ready.direction,
            'checked_at': format_timestamp(last_ready.checked_at),
        } if last_ready else None,
        'last_execution_result': _serialize_execution_result(last_result) if last_result else None,
    }


def get_spot_vault_view() -> dict[str, Any]:
    """Trading/futures equity, Virtual Spot Vault balance, total virtual
    capital, and the most recent Profit Split (see trading/virtual_spot_vault.py)."""
    state = _portfolio.get_state()
    last_split = _spot_vault.get_last_split()
    view: dict[str, Any] = {
        'trading_equity': f'{state.current_equity:.2f} USDT',
        'spot_vault_balance': f'{state.spot_vault_balance:.2f} USDT',
        'total_equity': f'{state.total_equity:.2f} USDT',
        'last_split': None,
    }
    if last_split:
        spot_percent_used = last_split.get('spot_percent_used')
        futures_percent_used = last_split.get('futures_percent_used')
        view['last_split'] = {
            'symbol': last_split['symbol'],
            'profit': f"{last_split['profit']:.2f} USDT",
            'futures_amount': f"{last_split['futures_amount']:.2f} USDT",
            'spot_amount': f"{last_split['spot_amount']:.2f} USDT",
            'spot_percent_used': f"{spot_percent_used:.0f}%" if spot_percent_used is not None else '-',
            'futures_percent_used': f"{futures_percent_used:.0f}%" if futures_percent_used is not None else '-',
            'created_at': format_timestamp(last_split['created_at']),
        }
    return view


PROFIT_SPLIT_PRESETS = [(50.0, 50.0), (30.0, 70.0), (70.0, 30.0), (0.0, 100.0), (100.0, 0.0)]


def get_profit_split_settings_view() -> dict[str, Any]:
    """Current persistent Profit Split percentages plus quick-set presets
    (see database/db.py::get_profit_split_settings/set_profit_split_settings)."""
    spot_percent, futures_percent = _spot_vault.get_settings()
    return {
        'spot_percent': f'{spot_percent:.0f}%',
        'futures_percent': f'{futures_percent:.0f}%',
        'spot_percent_raw': spot_percent,
        'futures_percent_raw': futures_percent,
        'presets': [
            {'label': f'{int(spot)}/{int(futures)}', 'spot': spot, 'futures': futures}
            for spot, futures in PROFIT_SPLIT_PRESETS
        ],
    }


def set_profit_split_settings(spot_percent: float, futures_percent: float) -> None:
    """Raises ValueError (Russian message) on invalid input — see
    database/db.py::set_profit_split_settings. Only affects trades split
    AFTER this call; already-recorded splits are never recalculated."""
    _spot_vault.set_settings(spot_percent, futures_percent)


def get_paper_trading_status() -> dict[str, Any]:
    enabled = _db.get_paper_trading_enabled()
    return {'enabled': enabled, 'label': 'ВКЛ' if enabled else 'ВЫКЛ'}


def set_paper_trading_enabled(enabled: bool) -> None:
    _db.set_paper_trading_enabled(enabled)


def _unrealized_pnl_for(position: dict[str, Any]) -> float:
    market_data = _market_service.get_symbol_data(position['symbol']) or {}
    current_price = market_data.get('last_price')
    if not current_price:
        return 0.0
    return PaperTradingEngine.calculate_unrealized_pnl(position, current_price)


def get_open_paper_positions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _db.get_open_paper_positions():
        position = dict(row)
        market_data = _market_service.get_symbol_data(position['symbol']) or {}
        current_price = market_data.get('last_price')
        unrealized = _unrealized_pnl_for(position)
        try:
            snapshot = json.loads(position.get('setup_snapshot') or '{}')
        except (TypeError, ValueError):
            snapshot = {}
        result.append({
            'symbol': position['symbol'],
            'direction': position['direction'],
            'leverage': f"x{int(position['leverage'])}",
            'entry_price': f"{position['entry_price']:.4f}",
            'current_price': f"{current_price:.4f}" if current_price else '-',
            'quantity': f"{position['quantity_remaining']:.6f}",
            'margin_used': f"{position['margin_used']:.2f} USDT",
            'unrealized_pnl': f"{unrealized:.2f} USDT",
            'unrealized_pnl_sign': 'positive' if unrealized > 0 else 'negative' if unrealized < 0 else '',
            'stop_loss': f"{position['stop_loss']:.4f}",
            'take_profit_1': f"{position['take_profit_1']:.4f}",
            'tp1_hit': 'ДА' if position['tp1_hit'] else 'НЕТ',
            'take_profit_2': f"{position['take_profit_2']:.4f}",
            'setup_score': snapshot.get('setup_score', '-'),
            'opened_at': format_timestamp(position['opened_at']),
        })
    return result


def get_brain_view(symbol: str | None = None) -> dict[str, Any]:
    symbol = symbol or 'BTCUSDT'
    analysis = _market_analysis_service.get_analysis(symbol) or {}
    market_data = _market_service.get_symbol_data(symbol) or {}
    setup = _setup_engine.build_setup(analysis, market_data) if analysis else {'setup_type': 'NO_SETUP', 'status': 'REJECTED', 'reasons': [], 'rejection_reasons': []}
    last_price = market_data.get('last_price')
    if analysis.get('trade_candidate'):
        _persist_setup(symbol, setup)
    serialized_setup = _serialize_setup(setup)
    risk_decision = _risk_manager.evaluate(symbol, serialized_setup, market_status=analysis.get('status', 'STALE'))
    # Advisory only — read-only display data. AIAnalyzer never receives the
    # risk decision and RiskManager never receives the AI result, so an AI
    # CONFIRM can never override a RiskManager rejection (see ai/analyzer.py).
    ai_result = _ai_analyzer.analyze(symbol, analysis, setup)
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
        'setup': serialized_setup,
        'risk_decision': risk_decision,
        'ai_analysis': {
            'status': ai_result.status,
            'provider': ai_result.provider,
            'direction': ai_result.direction,
            'ai_score': ai_result.ai_score,
            'decision': ai_result.decision,
            'reasons': ai_result.reasons,
            'risk_flags': ai_result.risk_flags,
        },
    }
