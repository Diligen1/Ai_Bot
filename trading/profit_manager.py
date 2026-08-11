"""Profit manager skeleton.

Responsible for profit allocation logic (placeholder).
"""
from typing import Any, Dict


class ProfitManager:
    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def allocate(self, profit_amount: float) -> Dict[str, float]:
        """Return allocation split between spot and futures (percentages).

        Uses settings placeholders; returns both allocations as amounts.
        """
        # Placeholder: split equally
        half = profit_amount / 2.0
        return {"spot": half, "futures": half}
