"""Position manager skeleton.

Manages open positions locally; does not place real orders in this scaffold.
"""
from typing import Any, Dict, List


class PositionManager:
    """Track and manage positions locally.

    This manager does not communicate with exchanges in the scaffold.
    """

    def __init__(self) -> None:
        self.positions: List[Dict[str, Any]] = []

    def add_position(self, pos: Dict[str, Any]) -> None:
        self.positions.append(pos)

    def close_position(self, pos_id: str) -> None:
        self.positions = [p for p in self.positions if p.get("id") != pos_id]

    def list_positions(self) -> List[Dict[str, Any]]:
        return list(self.positions)
