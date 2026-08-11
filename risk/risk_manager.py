"""RiskManager skeleton.

Responsible for position sizing, checks and risk limits. This scaffold must not open trades.
"""
from typing import Any, Dict


class RiskManager:
    """Simple risk manager skeleton.

    Add methods to evaluate risk for proposed trades. Currently no trading actions.
    """

    def __init__(self, settings: Dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    def assess(self, proposal: Dict[str, Any]) -> bool:
        """Assess whether a proposed trade meets risk criteria.

        `proposal` is a dict describing the intended trade. Returns True if allowed.
        Placeholder always returns False to prevent any trading.
        """
        return False

    def position_size(self, account_balance: float, risk_percent: float) -> float:
        """Compute position size given account and risk percent.

        Placeholder simple formula; does not execute trades.
        """
        return account_balance * (risk_percent / 100.0)
