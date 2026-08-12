"""Configuration for technical market analysis."""
from __future__ import annotations

EMA_SHORT = 20
EMA_MEDIUM = 50
EMA_LONG = 200
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14
BB_PERIOD = 20
BB_STDDEV = 2.0
VOLUME_AVERAGE_PERIOD = 20
MIN_CANDLES_FOR_ANALYSIS = 50

VOLATILITY_THRESHOLDS = {
    'LOW': 0.8,
    'NORMAL': 1.8,
    'HIGH': 3.0,
    'EXTREME': 5.0,
}

BREAKOUT_VOLUME_RATIO = 1.2
BREAKOUT_BAND_WIDTH = 0.08
RANGE_BAND_WIDTH = 0.04
SWING_MIN_MOVE_PCT = 0.6
SWING_LEFT = 2
SWING_RIGHT = 2

TECHNICAL_SCORE_WEIGHTS = {
    'trend': 25,
    'ema_alignment': 15,
    'macd': 15,
    'structure': 15,
    'volume_ratio': 10,
    'volatility': 10,
    'market_regime': 10,
}

TRADE_CANDIDATE_MIN_SCORE = 45
VOLUME_RATIO_MIN = 0.7

SUPPORT_RESISTANCE_TOLERANCE_ATR = 0.45
ATR_BUFFER = 1.2
MINIMUM_RR_TP1 = 1.2
MINIMUM_RR_TP2 = 1.8
MAX_ENTRY_ZONE_WIDTH_ATR = 3.0
MIN_ENTRY_ZONE_WIDTH_ATR = 0.3
MAX_PRICE_DISTANCE_ATR = 2.0
SETUP_TTL_SECONDS = 3600

SETUP_SCORE_WEIGHTS = {
    'technical': 30,
    'entry': 20,
    'rr': 20,
    'alignment': 10,
    'structure': 10,
    'distance': 10,
}
