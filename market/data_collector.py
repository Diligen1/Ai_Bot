"""Market data collector skeleton.

No network or API calls are implemented in this scaffold.
"""
from typing import Any, Iterable


class DataCollector:
    """Collects market data for strategies and learning components.

    Methods are placeholders and do not perform real I/O.
    """

    def __init__(self) -> None:
        # initialize local buffers or data structures
        self._historical = []

    def fetch_historical(self, symbol: str, timeframe: str, limit: int = 100) -> Iterable[Any]:
        """Return historical bars for `symbol`.

        Placeholder: returns an empty iterable.
        """
        return self._historical

    def stream_market_data(self, symbol: str) -> Iterable[Any]:
        """Yield streaming market data for `symbol`.

        Placeholder generator that yields nothing.
        """
        if False:
            yield
        return
