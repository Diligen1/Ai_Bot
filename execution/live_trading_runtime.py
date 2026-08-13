"""Live Trading Runtime — the ONLY automated multi-symbol loop that can ever
call `LiveTradingEngine.attempt_entry()`.

Fully separate from `trading/paper_trading_engine.py::PaperTradingEngine`
(virtual-only, never touches this module or execution/live_trading_engine.py).

Every hard limit — leverage<=5, margin<=10%, risk<=1%, max 3 open positions,
duplicate-symbol protection, Live Kill Switch, CRITICAL_UNPROTECTED_POSITION,
daily/weekly loss limits, loss streak — is already enforced inside
`LiveTradingEngine.attempt_entry()` / `LiveExecutionEngine.build_order_intent()`
/ `LiveRiskGuard`. This module NEVER re-implements any of that; its only
jobs are:
  1. Read the whitelist fresh from SQLite on every cycle (never cached).
  2. Build a setup per symbol using the existing MarketDataService /
     MarketAnalysisService / TradeSetupEngine — never duplicated logic.
  3. Call `attempt_entry()` ONLY when the setup status is actually READY.
  4. Guarantee one symbol's exception never stops the rest of the cycle.

If `LIVE_TRADING_ENABLED` is false (the default), `attempt_entry()` itself
refuses at its very first gate (STATUS_BLOCKED, reason LIVE_TRADING_DISABLED)
before it ever touches `trading_client` — this runtime still performs its
read-only market/analysis/setup steps every cycle (harmless, identical to
what every other Dashboard view already does), but no HTTP request capable
of placing an order is ever made while disabled.

Logging here is intentionally limited to symbol/status/result labels —
never an API key, secret, or a signed request/response payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Thread
from time import sleep
from typing import Any, Callable

from config.env import get_env
from execution.live_trading_engine import LiveTradingEngine
from market.setup_engine import TradeSetupEngine

DEFAULT_LIVE_ENGINE_INTERVAL_SECONDS = 20.0

READY_STATUS = 'READY'


def get_live_engine_interval_seconds() -> float:
    """Reads LIVE_ENGINE_INTERVAL_SECONDS (env/.env, see config/env.py),
    defaulting to 20s. Never a busy loop — always sleeps at least this long
    between cycles."""
    raw = get_env('LIVE_ENGINE_INTERVAL_SECONDS', str(DEFAULT_LIVE_ENGINE_INTERVAL_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIVE_ENGINE_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_LIVE_ENGINE_INTERVAL_SECONDS


@dataclass(frozen=True)
class ReadySetupInfo:
    symbol: str
    setup_type: str
    direction: str
    checked_at: str


class LiveTradingRuntime:
    """Background multi-symbol cycle. Dashboard-independent: `run.py` starts
    this the moment `app.dashboard_data` is imported, with no browser and no
    Dashboard interaction required — see requirement 9's read-only views,
    which only ever call the `get_last_*`/`is_running` getters below."""

    def __init__(
        self,
        live_trading_engine: LiveTradingEngine,
        setup_engine: TradeSetupEngine,
        market_service: Any,
        analysis_service: Any,
        symbols_provider: Callable[[], list[str]],
        interval_seconds: float | None = None,
    ) -> None:
        self.live_trading_engine = live_trading_engine
        self.setup_engine = setup_engine
        self.market_service = market_service
        self.analysis_service = analysis_service
        self.symbols_provider = symbols_provider
        self.interval_seconds = interval_seconds if interval_seconds is not None else get_live_engine_interval_seconds()
        self._stop = False
        self._started = False
        self._thread: Thread | None = None
        self._last_cycle_at: str | None = None
        self._last_symbols_checked: list[str] = []
        self._last_checked_symbol: str | None = None
        self._last_ready_setup: ReadySetupInfo | None = None
        self._last_cycle_errors: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Idempotent: a second call (e.g. a module re-imported by a test)
        never spawns a second background thread."""
        if self._started:
            return
        self._started = True
        self._stop = False
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._started = False

    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop:
            try:
                self.run_once()
            except Exception:
                pass  # a bad cycle must never kill the background loop
            sleep(self.interval_seconds)

    # -- main cycle ---------------------------------------------------------

    def run_once(self, now: datetime | None = None) -> None:
        """Runs exactly one synchronous cycle over every whitelist symbol.
        No sleeping, no threading — safe to call directly from tests."""
        now = now or datetime.now(timezone.utc)
        symbols = list(self.symbols_provider())
        print(f'LIVE CYCLE START symbols={len(symbols)}')

        checked: list[str] = []
        errors: dict[str, str] = {}
        for symbol in symbols:
            self._last_checked_symbol = symbol
            try:
                self._process_symbol(symbol, now)
                checked.append(symbol)
            except Exception as exc:
                # One symbol's failure (market data, analysis, Binance API,
                # anything) must never block the rest of this cycle.
                errors[symbol] = f'{type(exc).__name__}'
                print(f'LIVE CYCLE symbol={symbol} error={type(exc).__name__}')

        self._last_cycle_at = now.isoformat()
        self._last_symbols_checked = checked
        self._last_cycle_errors = errors
        print(f'LIVE CYCLE COMPLETED checked={len(checked)} errors={len(errors)}')

    def _process_symbol(self, symbol: str, now: datetime) -> None:
        market_data = self.market_service.get_symbol_data(symbol) or {}
        analysis = self.analysis_service.get_analysis(symbol) or {}
        setup = (
            self.setup_engine.build_setup(analysis, market_data)
            if analysis else {'setup_type': 'NO_SETUP', 'status': 'REJECTED'}
        )
        setup_status = setup.get('status', 'REJECTED')
        print(f'LIVE CYCLE symbol={symbol} setup_status={setup_status}')

        if setup_status != READY_STATUS:
            return  # NO_SETUP / WAITING / REJECTED / MISSED_ENTRY / etc. -> skip, never call attempt_entry

        self._last_ready_setup = ReadySetupInfo(
            symbol=symbol,
            setup_type=setup.get('setup_type', 'NO_SETUP'),
            direction=setup.get('direction', 'NEUTRAL'),
            checked_at=now.isoformat(),
        )

        result = self.live_trading_engine.attempt_entry(symbol, setup)
        print(f'LIVE CYCLE symbol={symbol} execution_result={result.status}')

    # -- status (Dashboard, read-only) -------------------------------------

    def is_live_trading_enabled(self) -> bool:
        return self.live_trading_engine.is_live_trading_enabled()

    def get_last_cycle_at(self) -> str | None:
        return self._last_cycle_at

    def get_last_symbols_checked(self) -> list[str]:
        return list(self._last_symbols_checked)

    def get_last_checked_symbol(self) -> str | None:
        return self._last_checked_symbol

    def get_last_ready_setup(self) -> ReadySetupInfo | None:
        return self._last_ready_setup

    def get_last_cycle_errors(self) -> dict[str, str]:
        return dict(self._last_cycle_errors)
