"""Basic settings for AI Futures Bot.

This file contains constants and lightweight helpers for configuration.
No secrets or API keys are stored here.
"""

# Risk / leverage settings (do NOT execute real trades in this scaffold)
DEFAULT_LEVERAGE = 5
MAX_MARGIN_PERCENT = 10
MAX_RISK_PERCENT = 0.75
MAX_DAILY_DRAWDOWN_PERCENT = 2.5
MAX_WEEKLY_DRAWDOWN_PERCENT = 5
PROFIT_TO_SPOT_PERCENT = 50
PROFIT_TO_FUTURES_PERCENT = 50

# Other runtime settings (placeholders)
LOG_LEVEL = "INFO"
DATA_DIR = "data"
