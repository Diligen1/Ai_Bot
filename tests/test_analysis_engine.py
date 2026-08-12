import pytest

from market.analysis_engine import (
    IndicatorEngine,
    MarketStructureAnalyzer,
    TrendAnalyzer,
    VolatilityAnalyzer,
    MarketRegimeDetector,
    MultiTimeframeAnalyzer,
    TechnicalScoreEngine,
    TradeCandidateFilter,
)

SAMPLE_CANDLES = [
    {'open': 100, 'high': 105, 'low': 99, 'close': 102, 'volume': 1000},
    {'open': 102, 'high': 108, 'low': 101, 'close': 107, 'volume': 1200},
    {'open': 107, 'high': 110, 'low': 106, 'close': 108, 'volume': 1300},
    {'open': 108, 'high': 109, 'low': 103, 'close': 105, 'volume': 1100},
    {'open': 105, 'high': 107, 'low': 102, 'close': 106, 'volume': 1400},
    {'open': 106, 'high': 111, 'low': 105, 'close': 110, 'volume': 1500},
    {'open': 110, 'high': 115, 'low': 109, 'close': 114, 'volume': 1600},
    {'open': 114, 'high': 118, 'low': 113, 'close': 117, 'volume': 1700},
    {'open': 117, 'high': 119, 'low': 115, 'close': 118, 'volume': 1800},
    {'open': 118, 'high': 121, 'low': 117, 'close': 120, 'volume': 1900},
    {'open': 120, 'high': 122, 'low': 118, 'close': 121, 'volume': 2000},
    {'open': 121, 'high': 125, 'low': 120, 'close': 124, 'volume': 2100},
    {'open': 124, 'high': 127, 'low': 123, 'close': 126, 'volume': 2200},
    {'open': 126, 'high': 130, 'low': 125, 'close': 129, 'volume': 2300},
    {'open': 129, 'high': 133, 'low': 128, 'close': 132, 'volume': 2400},
    {'open': 132, 'high': 135, 'low': 131, 'close': 134, 'volume': 2500},
    {'open': 134, 'high': 138, 'low': 133, 'close': 137, 'volume': 2600},
    {'open': 137, 'high': 140, 'low': 136, 'close': 139, 'volume': 2700},
    {'open': 139, 'high': 142, 'low': 138, 'close': 141, 'volume': 2800},
    {'open': 141, 'high': 145, 'low': 140, 'close': 144, 'volume': 2900},
]


def test_indicator_engine_ema_rsi_macd_atr_bollinger_volume() -> None:
    analysis = IndicatorEngine().analyze(SAMPLE_CANDLES)
    assert analysis['ema20'] is not None
    assert analysis['ema50'] is None
    assert analysis['ema200'] is None
    assert analysis['rsi14'] is not None
    assert analysis['macd_line'] is None
    assert analysis['signal_line'] is None
    assert analysis['histogram'] is None
    assert analysis['atr14'] is not None
    assert analysis['middle'] is not None
    assert analysis['upper'] is not None
    assert analysis['lower'] is not None
    assert analysis['width'] is not None
    assert analysis['current'] == SAMPLE_CANDLES[-1]['volume']
    assert analysis['average'] is not None
    assert analysis['ratio'] is not None


def test_market_structure_analyzer() -> None:
    structure = MarketStructureAnalyzer().analyze(SAMPLE_CANDLES)
    assert structure['structure'] in {'BULLISH', 'BEARISH', 'RANGE', 'UNKNOWN'}
    assert isinstance(structure['swings'], list)


def test_market_structure_identify_swings_handles_local_peaks_and_troughs() -> None:
    candles = [
        {'open': 100, 'high': 101, 'low':  98, 'close': 100, 'volume': 100},
        {'open': 100, 'high': 103, 'low':  99, 'close': 102, 'volume': 100},
        {'open': 102, 'high': 110, 'low': 101, 'close': 109, 'volume': 100},
        {'open': 109, 'high': 105, 'low': 100, 'close': 101, 'volume': 100},
        {'open': 101, 'high': 108, 'low':  99, 'close': 107, 'volume': 100},
        {'open': 107, 'high': 112, 'low': 104, 'close': 111, 'volume': 100},
        {'open': 111, 'high': 109, 'low': 102, 'close': 103, 'volume': 100},
        {'open': 103, 'high': 114, 'low': 106, 'close': 113, 'volume': 100},
    ]
    swings = MarketStructureAnalyzer().identify_swings(candles)
    assert isinstance(swings, list)
    assert any(swing['type'] == 'high' for swing in swings)
    assert any(swing['type'] == 'low' for swing in swings)


def test_market_structure_bullish_sequence() -> None:
    swings = [
        {'type': 'high', 'index': 2, 'price': 110.0},
        {'type': 'low', 'index': 4, 'price': 102.0},
        {'type': 'high', 'index': 6, 'price': 115.0},
        {'type': 'low', 'index': 8, 'price': 106.0},
    ]
    assert MarketStructureAnalyzer().determine_structure(swings) == 'BULLISH'


def test_market_structure_bearish_sequence() -> None:
    swings = [
        {'type': 'high', 'index': 2, 'price': 200.0},
        {'type': 'low', 'index': 4, 'price': 180.0},
        {'type': 'high', 'index': 6, 'price': 195.0},
        {'type': 'low', 'index': 8, 'price': 175.0},
    ]
    assert MarketStructureAnalyzer().determine_structure(swings) == 'BEARISH'


def test_market_structure_range_sequence() -> None:
    swings = [
        {'type': 'high', 'index': 2, 'price': 105.0},
        {'type': 'low', 'index': 4, 'price': 98.0},
        {'type': 'high', 'index': 6, 'price': 103.0},
        {'type': 'low', 'index': 8, 'price': 99.0},
    ]
    assert MarketStructureAnalyzer().determine_structure(swings) == 'RANGE'


def test_market_structure_flat_prices() -> None:
    candles = [
        {'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 100}
        for _ in range(6)
    ]
    structure = MarketStructureAnalyzer().analyze(candles)
    assert structure['structure'] == 'UNKNOWN'


def test_market_structure_insufficient_candles() -> None:
    candles = [
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 100}
        for _ in range(3)
    ]
    structure = MarketStructureAnalyzer().analyze(candles)
    assert structure['structure'] == 'UNKNOWN'


def test_trend_analyzer_neutral_when_data_insufficient() -> None:
    trend = TrendAnalyzer().analyze({'ema20': None, 'ema50': None, 'ema200': None, 'histogram': None, 'macd_line': None, 'signal_line': None}, 'UNKNOWN')
    assert trend['trend'] == 'NEUTRAL'


def test_volatility_analyzer_levels() -> None:
    volatility = VolatilityAnalyzer().analyze(100.0, 1.0)
    assert volatility['volatility'] in {'LOW', 'NORMAL', 'HIGH', 'EXTREME'}
    assert volatility['atr_percent'] == pytest.approx(1.0)


def test_market_regime_detector_uncertain_small() -> None:
    regime = MarketRegimeDetector().detect('NEUTRAL', 'UNKNOWN', 0.5, 0.02, 0.5, 100.0, 0.0, 0.0)
    assert regime in {'RANGE', 'UNCERTAIN'}


def test_multi_timeframe_analyzer() -> None:
    assert MultiTimeframeAnalyzer().align({'4h': 'BULLISH', '1h': 'BULLISH', '15m': 'BULLISH'}) == 'STRONG_LONG'
    assert MultiTimeframeAnalyzer().align({'4h': 'BEARISH', '1h': 'BEARISH', '15m': 'BEARISH'}) == 'STRONG_SHORT'
    assert MultiTimeframeAnalyzer().align({'4h': 'BULLISH', '1h': 'BULLISH', '15m': 'BEARISH'}) == 'LONG_BIAS'
    assert MultiTimeframeAnalyzer().align({'4h': 'BULLISH', '1h': 'BEARISH', '15m': 'BEARISH'}) == 'CONFLICT'


def test_technical_score_engine_direction_and_confidence() -> None:
    score = TechnicalScoreEngine().calculate('BULLISH', 'BULLISH', True, 'BULLISH', 1.2, 'NORMAL', 'TREND_UP', 'STRONG_LONG')
    assert score['direction'] == 'LONG'
    assert score['technical_score'] >= 45
    assert score['confidence'] in {'LOW', 'MEDIUM', 'HIGH'}


def test_trade_candidate_filter_blocks_stale_or_low_score() -> None:
    result = TradeCandidateFilter().evaluate('STALE', 20, 'NEUTRAL', 'CONFLICT', 0.5, 'EXTREME', 'UNKNOWN', 'UNCERTAIN', True)
    assert result['trade_candidate'] is False
    assert 'STALE_DATA' in result['block_reasons']
    assert 'WEAK_TECHNICAL_SCORE' in result['block_reasons']
