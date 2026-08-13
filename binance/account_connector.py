"""BinanceAccountConnector — READ-ONLY orchestrator around BinanceAccountClient.

Disabled unless BOTH BINANCE_API_KEY and BINANCE_API_SECRET are present in
.env (see config/env.py). Any client failure (bad key, permission denied,
timeout, network/parse error) degrades to status=ERROR instead of raising —
the app must keep running either way. This connector is read-only display
data for the Dashboard only; it is never consumed by RiskManager,
PaperTradingEngine, or VirtualPortfolio, and never influences paper trading
in any way — real account data and virtual/paper capital are never mixed
into a single number (see app/dashboard_data.py::get_binance_account_view,
kept fully separate from get_balance()/get_spot_vault_view()).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from binance.account_client import BinanceAccountClient, BinanceAccountError
from config.env import get_env

STATUS_DISABLED = 'DISABLED'
STATUS_CONNECTED = 'CONNECTED'
STATUS_ERROR = 'ERROR'


@dataclass(frozen=True)
class BinancePosition:
    symbol: str
    position_amt: float
    entry_price: float
    unrealized_pnl: float
    leverage: int
    position_side: str


@dataclass(frozen=True)
class BinanceAccountSnapshot:
    status: str
    wallet_balance: float = 0.0
    available_balance: float = 0.0
    unrealized_pnl: float = 0.0
    can_trade: bool | None = None
    positions: list[BinancePosition] = field(default_factory=list)
    error_message: str | None = None


class BinanceAccountConnector:
    def __init__(self, client: BinanceAccountClient | None = None) -> None:
        """`client` override is for tests (inject a mock) — production code
        should call `BinanceAccountConnector()` with no arguments and let it
        read BINANCE_API_KEY/BINANCE_API_SECRET from .env."""
        self._client = client if client is not None else self._build_default_client()

    @staticmethod
    def _build_default_client() -> BinanceAccountClient | None:
        api_key = get_env('BINANCE_API_KEY')
        api_secret = get_env('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            return None
        return BinanceAccountClient(api_key=api_key, api_secret=api_secret)

    @property
    def is_enabled(self) -> bool:
        return self._client is not None

    def get_snapshot(self) -> BinanceAccountSnapshot:
        if not self.is_enabled:
            return BinanceAccountSnapshot(status=STATUS_DISABLED)

        try:
            account_info = self._client.get_account_info()
            positions_raw = self._client.get_position_risk()
        except BinanceAccountError as exc:
            return BinanceAccountSnapshot(status=STATUS_ERROR, error_message=str(exc))
        except Exception:
            # Any unexpected client bug must degrade, never crash the Dashboard.
            return BinanceAccountSnapshot(status=STATUS_ERROR, error_message='Unexpected error reading Binance account')

        return self._parse_snapshot(account_info, positions_raw)

    @staticmethod
    def _parse_snapshot(account_info: dict[str, Any], positions_raw: list[dict[str, Any]]) -> BinanceAccountSnapshot:
        try:
            wallet_balance = float(account_info.get('totalWalletBalance', 0.0) or 0.0)
            available_balance = float(account_info.get('availableBalance', 0.0) or 0.0)
            unrealized_pnl = float(account_info.get('totalUnrealizedProfit', 0.0) or 0.0)
        except (TypeError, ValueError):
            return BinanceAccountSnapshot(status=STATUS_ERROR, error_message='Unexpected Binance account response shape')
        can_trade = account_info.get('canTrade')

        positions: list[BinancePosition] = []
        for row in positions_raw or []:
            try:
                position_amt = float(row.get('positionAmt', 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if position_amt == 0.0:
                continue  # flat / no open position on this symbol
            try:
                positions.append(BinancePosition(
                    symbol=row.get('symbol', '-'),
                    position_amt=position_amt,
                    entry_price=float(row.get('entryPrice', 0.0) or 0.0),
                    unrealized_pnl=float(row.get('unRealizedProfit', row.get('unrealizedProfit', 0.0)) or 0.0),
                    leverage=int(float(row.get('leverage', 0) or 0)),
                    position_side=row.get('positionSide', 'BOTH'),
                ))
            except (TypeError, ValueError):
                continue

        return BinanceAccountSnapshot(
            status=STATUS_CONNECTED,
            wallet_balance=wallet_balance,
            available_balance=available_balance,
            unrealized_pnl=unrealized_pnl,
            can_trade=can_trade,
            positions=positions,
        )
