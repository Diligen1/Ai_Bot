import pytest

from market.setup_engine import TradeSetupEngine, SupportResistanceAnalyzer, SetupQualityFilter


def make_candles(base: float, count: int, step: float = 1.0) -> list[dict[str, float]]:
    candles = []
    for i in range(count):
        price = base + i * step
        candles.append({'open': price - 0.5, 'high': price + 1.0, 'low': price - 1.0, 'close': price, 'volume': 1000.0})
    return candles


def make_analysis(direction: str, market_regime: str, trade_candidate: bool = True, status: str = 'LIVE') -> dict[str, object]:
    return {
        'symbol': 'BTCUSDT',
        'direction': direction,
        'market_regime': market_regime,
        'technical_score': 60,
        'confidence': 'MEDIUM',
        'timeframe_alignment': 'STRONG_LONG' if direction == 'LONG' else 'STRONG_SHORT',
        'trade_candidate': trade_candidate,
        'status': status,
        'analysis': {
            '15m': {
                'indicators': {
                    'ema20': 102.0,
                    'ema50': 100.0,
                    'atr14': 2.0,
                    'upper': 110.0,
                    'lower': 95.0,
                    'ratio': 1.1,
                },
                'trend': 'BULLISH' if direction == 'LONG' else 'BEARISH',
                'structure': 'BULLISH' if direction == 'LONG' else 'BEARISH',
                'volatility': 'NORMAL',
                'market_regime': market_regime,
            },
            '1h': {'trend': 'BULLISH' if direction == 'LONG' else 'BEARISH', 'structure': 'BULLISH' if direction == 'LONG' else 'BEARISH'},
            '4h': {'trend': 'BULLISH' if direction == 'LONG' else 'BEARISH', 'structure': 'BULLISH' if direction == 'LONG' else 'BEARISH', 'market_regime': market_regime},
        },
        'last_price': 103.0 if direction == 'LONG' else 97.0,
        'atr': 2.0,
    }


def make_symbol_data(candles: list[dict[str, float]]) -> dict[str, object]:
    return {'candles_15m': candles}


def test_support_resistance_clusters_levels() -> None:
    candles = make_candles(100.0, 20)
    sr = SupportResistanceAnalyzer()
    levels = sr.analyze(candles, 2.0)
    assert 'supports' in levels and 'resistances' in levels
    assert isinstance(levels['supports'], list)
    assert isinstance(levels['resistances'], list)


def test_trade_setup_long_pullback() -> None:
    engine = TradeSetupEngine()
    analysis = make_analysis('LONG', 'TREND_UP')
    symbol_data = make_symbol_data(make_candles(100.0, 40, 0.5))
    setup = engine.build_setup(analysis, symbol_data)

    assert setup['setup_type'] == 'TREND_PULLBACK'
    assert setup['direction'] == 'LONG'
    assert setup['entry_zone_high'] >= setup['entry_zone_low']
    assert setup['stop_loss'] < setup['entry_zone_low']
    assert setup['take_profit_1'] > setup['entry_zone_high']
    assert setup['risk_reward_tp1'] >= 1.2
    assert setup['status'] in {'WAITING', 'READY', 'REJECTED'}


def test_trade_setup_short_momentum_continuation() -> None:
    engine = TradeSetupEngine()
    analysis = make_analysis('SHORT', 'BREAKOUT_DOWN')
    symbol_data = make_symbol_data(make_candles(100.0, 40, -0.5))
    setup = engine.build_setup(analysis, symbol_data)

    assert setup['setup_type'] in {'BREAKOUT_RETEST', 'MOMENTUM_CONTINUATION'}
    assert setup['direction'] == 'SHORT'
    assert setup['entry_zone_high'] >= setup['entry_zone_low']
    assert setup['stop_loss'] > setup['entry_zone_high']
    assert setup['take_profit_1'] < setup['entry_zone_low']
    assert setup['risk_reward_tp1'] >= 1.2


def test_trade_setup_rejected_missing_candidate() -> None:
    engine = TradeSetupEngine()
    analysis = make_analysis('LONG', 'TREND_UP', trade_candidate=False)
    symbol_data = make_symbol_data(make_candles(100.0, 40, 0.5))
    setup = engine.build_setup(analysis, symbol_data)

    assert setup['setup_type'] == 'NO_SETUP'
    assert 'TRADE_CANDIDATE_FALSE' in setup['rejection_reasons']
    assert setup['status'] == 'REJECTED'


def test_trade_setup_rejected_extreme_volatility() -> None:
    engine = TradeSetupEngine()
    analysis = make_analysis('LONG', 'TREND_UP')
    analysis['analysis']['15m']['volatility'] = 'EXTREME'
    analysis['market_regime'] = 'TREND_UP'
    symbol_data = make_symbol_data(make_candles(100.0, 40, 0.5))
    setup = engine.build_setup(analysis, symbol_data)

    assert setup['status'] == 'REJECTED'
    assert 'EXTREME_VOLATILITY' in setup['rejection_reasons']


def test_setup_quality_filter_invalid_entry_zone() -> None:
    filter_ = SetupQualityFilter()
    setup = {
        'entry_zone_low': 100.0,
        'entry_zone_high': 99.0,
        'stop_loss': 98.0,
        'take_profit_1': 105.0,
        'take_profit_2': 110.0,
        'risk_reward_tp1': 1.0,
        'risk_reward_tp2': 2.0,
        'direction': 'LONG',
    }
    result = filter_.validate(setup, {'status': 'LIVE', 'trade_candidate': True}, 101.0, 2.0)
    assert not result['valid']
    assert 'INVALID_ENTRY_ZONE' in result['reasons']


def test_trade_setup_missed_entry() -> None:
    engine = TradeSetupEngine()
    analysis = make_analysis('LONG', 'TREND_UP')
    symbol_data = make_symbol_data(make_candles(100.0, 40, 0.5))
    setup = engine.build_setup(analysis, symbol_data)
    if setup['status'] == 'REJECTED':
        return
    entry_high = setup['entry_zone_high']
    price = entry_high + 10.0
    filter_result = SetupQualityFilter().validate(setup, analysis, price, 2.0)
    assert not filter_result['valid']
    assert 'MISSED_ENTRY' in filter_result['reasons']
