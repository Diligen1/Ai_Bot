"""Centralized risk & runtime configuration for AI Futures Bot.

All values here apply to the VIRTUAL/PAPER trading simulation only.
No real Binance account, API keys, or execution are involved anywhere
in this project.
"""
from __future__ import annotations

from dataclasses import dataclass

# Virtual portfolio
INITIAL_VIRTUAL_BALANCE = 500.0

# Risk / leverage settings (do NOT execute real trades in this scaffold)
DEFAULT_LEVERAGE = 5
MAX_MARGIN_PERCENT = 10.0
RISK_PER_TRADE_PERCENT = 1.0
DAILY_LOSS_LIMIT_PERCENT = 3.0
WEEKLY_LOSS_LIMIT_PERCENT = 6.0
MAX_CONSECUTIVE_LOSSES = 3
MAX_OPEN_POSITIONS = 3
MINIMUM_RISK_REWARD = 1.5

# Rough, locally configured cost estimates used only to size paper positions.
# NOT fetched from Binance and NOT real fee/slippage rates.
ESTIMATED_FEE_PERCENT = 0.08
ESTIMATED_SLIPPAGE_PERCENT = 0.05

# Placeholder profit-split settings (Profit Split feature itself is not implemented)
PROFIT_TO_SPOT_PERCENT = 50
PROFIT_TO_FUTURES_PERCENT = 50

# Paper Trading Engine (virtual execution only, see trading/paper_trading_engine.py)
TP1_CLOSE_PERCENT = 50
TP2_CLOSE_PERCENT = 50
PAPER_ENGINE_INTERVAL_SECONDS = 20.0
STRATEGY_VERSION = "v1"

# Other runtime settings (placeholders)
LOG_LEVEL = "INFO"
DATA_DIR = "data"


@dataclass(frozen=True)
class RiskConfig:
    """Centralized, immutable risk configuration used by RiskManager/VirtualPortfolio."""

    initial_virtual_balance: float = INITIAL_VIRTUAL_BALANCE
    leverage: int = DEFAULT_LEVERAGE
    max_margin_percent: float = MAX_MARGIN_PERCENT
    risk_per_trade_percent: float = RISK_PER_TRADE_PERCENT
    daily_loss_limit_percent: float = DAILY_LOSS_LIMIT_PERCENT
    weekly_loss_limit_percent: float = WEEKLY_LOSS_LIMIT_PERCENT
    max_consecutive_losses: int = MAX_CONSECUTIVE_LOSSES
    max_open_positions: int = MAX_OPEN_POSITIONS
    minimum_risk_reward: float = MINIMUM_RISK_REWARD
    estimated_fee_percent: float = ESTIMATED_FEE_PERCENT
    estimated_slippage_percent: float = ESTIMATED_SLIPPAGE_PERCENT


DEFAULT_RISK_CONFIG = RiskConfig()
