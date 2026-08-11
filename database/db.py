"""Database manager skeleton.

This manager is lightweight and does not create complex schemas yet.
"""
from typing import Optional


class DatabaseManager:
    """Simple database manager placeholder.

    Extend to add migrations, ORM bindings or connection pooling.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or ":memory:"
        self._conn = None

    def connect(self) -> None:
        """Open a connection (placeholder)."""
        # Intentionally not opening a real connection in scaffold
        self._conn = None

    def close(self) -> None:
        self._conn = None
