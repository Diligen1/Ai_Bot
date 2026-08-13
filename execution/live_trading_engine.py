"""Live Execution Engine V2 — real Binance order placement, OFF by default.

`LiveTradingEngine.attempt_entry()` is the ONLY code path in this project
that can ever place a real order. It re-validates EVERY hard limit already
enforced elsewhere (RiskConfig: leverage<=5, margin<=10%, risk<=1%,
max 3 positions) plus several Live-only gates, and the very first gates
checked are `LIVE_TRADING_ENABLED` (env var, defaults False — see
config/env.py) and the separate LIVE Kill Switch (database/db.py
get_live_kill_switch/set_live_kill_switch — independent of the paper-trading
kill switch; see risk/live_risk_guard.py, which reads the SAME live switch).
If any gate fails, `trading_client` is never touched and no HTTP request
reaches Binance.

Execution state machine (per `client_order_id`, persisted in
database/db.py::live_execution_log): every attempt lands in exactly one of
four buckets, and idempotency behaves differently for each — "a log row
exists" is NOT by itself proof that an order was ever placed:

  TERMINAL SUCCESS (STATUS_PROTECTED)
    -> retry is refused; the stored order ids are just echoed back.

  RETRYABLE FAILURE (STATUS_ENTRY_FAILED)
    -> confirmed, with certainty, that no order/position resulted. A new
       attempt is allowed, but only after a full fresh risk check
       (attempt_entry() always rebuilds the OrderIntent from scratch before
       ever consulting the log).

  AMBIGUOUS (STATUS_ENTRY_TIMEOUT_UNCONFIRMED)
    -> the request may or may not have reached Binance. NEVER sends a new
       entry order. Instead reconciles: query the order by clientOrderId,
       and cross-check the actual Binance position for the symbol/side. Only
       a definitive answer moves the state forward; anything inconclusive
       (network error, unrecognised status) stays ambiguous for a later call.

  CRITICAL (STATUS_CRITICAL_UNPROTECTED_POSITION)
    -> a real position was confirmed to exist with no protective Stop Loss.
       ALL new entries are blocked project-wide (this is checked right after
       the LIVE_TRADING_ENABLED gate, ahead of even the Kill Switch and
       building an OrderIntent — the Kill Switch blocks NEW entries only, it
       must never block recovering an already-open unprotected position)
       until recovery places an SL or confirms the position no longer
       exists. The engine never closes the position itself and never opens
       a new one to "fix" this — see _resolve_unprotected_positions().

SL is CRITICAL: once an entry is confirmed filled (directly, or discovered
via reconciliation), a protective Stop Loss is placed immediately. Failure
to do so is never swallowed — it becomes STATUS_CRITICAL_UNPROTECTED_POSITION,
not a generic error, so it is impossible to lose track of.

TP1/TP2 follow the SL and reuse the setup's existing take-profit levels
(never invented here) — see execution/order_intent.py, itself built from the
already-existing TradeSetup. Closing orders always carry `reduceOnly`
(ONE-WAY) or the correct `positionSide` (Hedge Mode, where `reduceOnly` is
never sent — Binance rejects it there); either mechanism is Binance's own
guarantee that a closing order can reduce but never flip/increase a position.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from binance.account_client import BinanceAccountClient
from binance.trading_client import LiveTradingAPIError, LiveTradingClient, LiveTradingError, LiveTradingTimeout
from config.env import get_env
from config.settings import DEFAULT_RISK_CONFIG, RiskConfig, TP1_CLOSE_PERCENT
from database.db import DatabaseManager
from execution.live_execution_engine import LiveExecutionEngine
from execution.order_intent import OrderIntent, STATUS_READY

POSITION_MODE_ONE_WAY = 'ONE_WAY'
POSITION_MODE_HEDGE = 'HEDGE'

FILLED_STATUSES = {'FILLED', 'PARTIALLY_FILLED'}
TERMINAL_NO_FILL_STATUSES = {'CANCELED', 'REJECTED', 'EXPIRED'}
ORDER_NOT_FOUND_CODE = -2013
ORDER_NOT_FOUND = 'ORDER_NOT_FOUND'  # sentinel, distinct from None ("could not check")

STATUS_BLOCKED = 'BLOCKED'
STATUS_ALREADY_EXECUTED = 'ALREADY_EXECUTED'
STATUS_ENTRY_FAILED = 'ENTRY_FAILED'
STATUS_ENTRY_TIMEOUT_UNCONFIRMED = 'ENTRY_TIMEOUT_UNCONFIRMED'
STATUS_CRITICAL_UNPROTECTED_POSITION = 'CRITICAL_UNPROTECTED_POSITION'
STATUS_POSITION_CLOSED_EXTERNALLY = 'POSITION_CLOSED_EXTERNALLY'
STATUS_PROTECTED = 'PROTECTED'

# Kept only for readability at call sites / tests that want to assert on
# "this is a success" rather than spelling out {STATUS_PROTECTED}.
TERMINAL_SUCCESS_STATUSES = {STATUS_PROTECTED}
RETRYABLE_FAILURE_STATUSES = {STATUS_ENTRY_FAILED}
AMBIGUOUS_STATUSES = {STATUS_ENTRY_TIMEOUT_UNCONFIRMED}
CRITICAL_STATUSES = {STATUS_CRITICAL_UNPROTECTED_POSITION}
NO_RETRY_STATUSES = TERMINAL_SUCCESS_STATUSES | {STATUS_POSITION_CLOSED_EXTERNALLY}


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    execution_id: str | None = None
    symbol: str | None = None
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    tp1_order_id: str | None = None
    tp2_order_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    error_message: str | None = None


class LiveTradingEngine:
    def __init__(
        self,
        trading_client: LiveTradingClient | None,
        account_client: BinanceAccountClient | None,
        live_risk_guard: Any,
        execution_engine: LiveExecutionEngine,
        db: DatabaseManager,
        config: RiskConfig = DEFAULT_RISK_CONFIG,
    ) -> None:
        self.trading_client = trading_client
        self.account_client = account_client
        self.live_risk_guard = live_risk_guard
        self.execution_engine = execution_engine
        self.db = db
        self.config = config
        self._last_execution_result: ExecutionResult | None = None

    # -- status ---------------------------------------------------------

    @staticmethod
    def is_live_trading_enabled() -> bool:
        return (get_env('LIVE_TRADING_ENABLED', 'false') or '').strip().lower() == 'true'

    def is_live_kill_switch_on(self) -> bool:
        return self.db.get_live_kill_switch()

    def set_live_kill_switch(self, value: bool) -> None:
        self.db.set_live_kill_switch(value)

    def get_last_execution_result(self) -> ExecutionResult | None:
        return self._last_execution_result

    def has_unprotected_position(self) -> bool:
        """Read-only check for the Dashboard — does NOT attempt recovery
        (that only ever happens inside attempt_entry(), never as a side
        effect of a status read)."""
        return len(self.db.get_live_execution_logs_by_status(STATUS_CRITICAL_UNPROTECTED_POSITION)) > 0

    # -- entry ------------------------------------------------------------

    def attempt_entry(self, symbol: str, setup: dict[str, Any]) -> ExecutionResult:
        """Re-checks every gate immediately before acting — nothing here is
        cached from an earlier tick. Returns without ever calling
        `self.trading_client` unless every single gate passed."""
        if not self.is_live_trading_enabled():
            return self._blocked(symbol, ['LIVE_TRADING_DISABLED'])

        # A confirmed unprotected position blocks ALL new entries, project-wide,
        # and the engine keeps trying to resolve it on every call — see class
        # docstring. Checked BEFORE the kill switch on purpose: the kill switch
        # blocks NEW entries only, it must never prevent protecting a position
        # that already exists for real on the exchange.
        if self._resolve_unprotected_positions():
            return self._blocked(symbol, ['CRITICAL_UNPROTECTED_POSITION_ACTIVE'])

        if self.is_live_kill_switch_on():
            return self._blocked(symbol, ['LIVE_KILL_SWITCH_ON'])

        if self.trading_client is None or self.account_client is None:
            return self._blocked(symbol, ['BINANCE_TRADING_NOT_CONNECTED'])

        # Re-validates RiskGuard-approved, setup READY, market fresh, whitelist,
        # no duplicate symbol, max positions, margin<=10%, risk<=1%, tick/step/minNotional.
        intent = self.execution_engine.build_order_intent(symbol, setup)
        if intent.status != STATUS_READY:
            return self._blocked(symbol, intent.rejection_reasons or ['ORDER_INTENT_REJECTED'], intent=intent)

        current_leverage = self._get_current_symbol_leverage(symbol)
        if current_leverage is None:
            return self._blocked(symbol, ['LEVERAGE_UNKNOWN'], intent=intent)
        if current_leverage > self.config.leverage:
            return self._blocked(symbol, ['LEVERAGE_EXCEEDS_LIMIT'], intent=intent)

        position_mode = self._detect_position_mode()
        if position_mode is None:
            return self._blocked(symbol, ['UNKNOWN_POSITION_MODE'], intent=intent)

        setup_id = setup.get('id')
        client_order_id = self._client_order_id_for(setup_id, 'entry')
        existing_log = self.db.get_live_execution_log_by_client_order_id(client_order_id)

        if existing_log is None:
            return self._execute_entry(symbol, setup_id, intent, position_mode, client_order_id)

        return self._handle_existing_entry_log(dict(existing_log), symbol, setup_id, intent, position_mode, client_order_id)

    def _handle_existing_entry_log(
        self, log: dict[str, Any], symbol: str, setup_id: Any, intent: OrderIntent, position_mode: str, client_order_id: str,
    ) -> ExecutionResult:
        status = log['status']
        if status in NO_RETRY_STATUSES:
            return self._reconcile_existing(log)
        if status in RETRYABLE_FAILURE_STATUSES:
            # Confirmed no order/position resulted last time — a fresh attempt
            # is allowed, reusing the SAME client_order_id (the row is updated
            # in place; the UNIQUE constraint means we can never insert twice).
            return self._execute_entry(symbol, setup_id, intent, position_mode, client_order_id, reuse_row=log)
        # AMBIGUOUS (or CRITICAL, defensively — the top-level gate should have
        # already caught that case) -> always reconcile, never send a new order.
        return self._reconcile_uncertain_entry(log['execution_id'], symbol, setup_id, intent, position_mode, client_order_id)

    # -- gates helpers ----------------------------------------------------

    def _get_current_symbol_leverage(self, symbol: str) -> int | None:
        """Reads Binance's currently-configured leverage for `symbol` via the
        already-audited READ-ONLY BinanceAccountClient.get_position_risk()
        (which includes a leverage row for every symbol, even flat ones) —
        this engine never calls a leverage-change endpoint, so this is a
        hard fail-safe check, never a setter."""
        try:
            rows = self.account_client.get_position_risk()
        except Exception:
            return None
        for row in rows or []:
            if row.get('symbol') == symbol:
                try:
                    return int(float(row.get('leverage', 0) or 0))
                except (TypeError, ValueError):
                    return None
        return None

    def _get_real_position(self, symbol: str, position_side: str | None = None) -> dict[str, float] | None:
        """Reads the ACTUAL Binance position for `symbol` (and `position_side`
        in Hedge Mode, where a symbol can have separate LONG/SHORT rows) via
        the read-only BinanceAccountClient. Returns None when this can't be
        determined — callers must treat that as "unknown", never as "flat"."""
        if self.account_client is None:
            return None
        try:
            rows = self.account_client.get_position_risk()
        except Exception:
            return None
        matched = None
        for row in rows or []:
            if row.get('symbol') != symbol:
                continue
            row_position_side = row.get('positionSide', 'BOTH')
            if position_side is not None and row_position_side != position_side:
                continue
            matched = row
            break
        if matched is None:
            return None
        try:
            amount = float(matched.get('positionAmt', 0) or 0)
        except (TypeError, ValueError):
            return None
        return {'position_amt': amount}

    def _detect_position_mode(self) -> str | None:
        try:
            info = self.trading_client.get_position_mode()
        except LiveTradingError:
            return None
        dual = info.get('dualSidePosition') if isinstance(info, dict) else None
        if dual is True:
            return POSITION_MODE_HEDGE
        if dual is False:
            return POSITION_MODE_ONE_WAY
        return None

    @staticmethod
    def _resolve_position_side(intent_position_side: str, mode: str) -> str:
        if mode == POSITION_MODE_ONE_WAY:
            return 'BOTH'
        return intent_position_side  # 'LONG' / 'SHORT' in Hedge Mode

    @staticmethod
    def _reduce_only_flag(mode: str) -> bool | None:
        # Hedge Mode REJECTS the reduceOnly param outright; ONE-WAY mode
        # needs it on every closing order so it can never flip the position.
        return True if mode == POSITION_MODE_ONE_WAY else None

    @staticmethod
    def _client_order_id_for(setup_id: Any, label: str) -> str:
        return f'bot-{setup_id}-{label}'

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _classify_order_response(status: Any, executed_qty: float) -> str:
        """'FILLED' / 'PENDING' (still resting, e.g. NEW — ambiguous) /
        'TERMINAL_NO_FILL' (CANCELED/REJECTED/EXPIRED — confirmed no fill) /
        'UNKNOWN' (anything unrecognised — treated as ambiguous, never assumed safe)."""
        if status in FILLED_STATUSES and executed_qty > 0:
            return 'FILLED'
        if status == 'NEW':
            return 'PENDING'
        if status in TERMINAL_NO_FILL_STATUSES:
            return 'TERMINAL_NO_FILL'
        return 'UNKNOWN'

    # -- entry execution ----------------------------------------------------

    def _execute_entry(
        self, symbol: str, setup_id: Any, intent: OrderIntent, position_mode: str, client_order_id: str,
        reuse_row: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        # The ACTUAL Binance positionSide ('BOTH' in ONE-WAY mode, 'LONG'/
        # 'SHORT' in Hedge Mode) — never the conceptual intent.position_side —
        # is what gets persisted, since that's what a later get_position_risk()
        # row will actually report back during reconciliation/recovery.
        position_side = self._resolve_position_side(intent.position_side, position_mode)

        if reuse_row is not None:
            execution_id = reuse_row['execution_id']
            self.db.update_live_execution_log(execution_id, status='ENTRY_PENDING', error_code=None, error_message=None)
        else:
            execution_id = str(uuid.uuid4())
            inserted = self.db.add_live_execution_log(
                execution_id=execution_id, client_order_id=client_order_id, setup_id=setup_id,
                symbol=symbol, side=intent.side, quantity=intent.quantity,
                stop_loss=intent.stop_loss, position_side=position_side,
                status='ENTRY_PENDING', requested_at=self._now(),
            )
            if inserted is None:
                # Lost a race against another process using the same client_order_id
                # between the lookup above and this insert — reconcile, never duplicate.
                existing = self.db.get_live_execution_log_by_client_order_id(client_order_id)
                if existing is None:
                    return self._blocked(symbol, ['DUPLICATE_CLIENT_ORDER_ID'], intent=intent)
                return self._handle_existing_entry_log(dict(existing), symbol, setup_id, intent, position_mode, client_order_id)

        try:
            entry_response = self.trading_client.open_position(
                symbol=symbol, side=intent.side, position_side=position_side,
                quantity=intent.quantity, order_type='MARKET', client_order_id=client_order_id,
            )
        except LiveTradingTimeout as exc:
            self.db.update_live_execution_log(execution_id, status=STATUS_ENTRY_TIMEOUT_UNCONFIRMED, error_code='LiveTradingTimeout', error_message=str(exc))
            return self._reconcile_uncertain_entry(execution_id, symbol, setup_id, intent, position_mode, client_order_id)
        except LiveTradingError as exc:
            # A clean, confirmed API error before any order existed — retryable.
            self.db.update_live_execution_log(execution_id, status=STATUS_ENTRY_FAILED, error_code=type(exc).__name__, error_message=str(exc))
            return self._store_result(ExecutionResult(status=STATUS_ENTRY_FAILED, execution_id=execution_id, symbol=symbol, error_message=str(exc)))

        status = entry_response.get('status') if isinstance(entry_response, dict) else None
        try:
            executed_qty = float(entry_response.get('executedQty', 0) or 0) if isinstance(entry_response, dict) else 0.0
        except (TypeError, ValueError):
            executed_qty = 0.0
        classification = self._classify_order_response(status, executed_qty)

        if classification == 'FILLED':
            entry_order_id = str(entry_response.get('orderId', '') or '')
            return self._confirm_and_protect(execution_id, setup_id, symbol, intent.stop_loss, intent.side, position_mode, position_side, executed_qty, entry_order_id, intent.take_profit_1, intent.take_profit_2)
        if classification == 'TERMINAL_NO_FILL':
            self.db.update_live_execution_log(
                execution_id, status=STATUS_ENTRY_FAILED,
                entry_order_id=str(entry_response.get('orderId', '') or ''), error_code=f'ORDER_{status}',
            )
            return self._store_result(ExecutionResult(status=STATUS_ENTRY_FAILED, execution_id=execution_id, symbol=symbol, reasons=[f'ORDER_{status}']))
        # PENDING (order accepted but not yet filled) or UNKNOWN -> never assumed
        # safe, never assumed failed. Reconcile now and, if still unresolved,
        # on every future call — never send a second order in the meantime.
        self.db.update_live_execution_log(execution_id, status=STATUS_ENTRY_TIMEOUT_UNCONFIRMED, error_code=f'ORDER_{classification}')
        return self._reconcile_uncertain_entry(execution_id, symbol, setup_id, intent, position_mode, client_order_id)

    # -- reconciliation (AMBIGUOUS states) -----------------------------------

    def _reconcile_uncertain_entry(
        self, execution_id: str, symbol: str, setup_id: Any, intent: OrderIntent, position_mode: str, client_order_id: str,
    ) -> ExecutionResult:
        """Never sends a new entry order. Only a DEFINITIVE answer (order
        genuinely filled, or genuinely never existed AND no position exists)
        moves the state out of AMBIGUOUS."""
        row = self.db.get_live_execution_log(execution_id)
        row = dict(row) if row is not None else {}
        stored_side = row.get('side')
        stored_position_side = row.get('position_side')
        stored_stop_loss = row.get('stop_loss')
        fallback_side = intent.side if intent else stored_side
        fallback_stop_loss = intent.stop_loss if intent else stored_stop_loss
        fallback_position_side = stored_position_side or (self._resolve_position_side(intent.position_side, position_mode) if intent else None)
        fallback_tp1 = intent.take_profit_1 if intent else None
        fallback_tp2 = intent.take_profit_2 if intent else None

        queried = self._query_order_safely(symbol, client_order_id)

        if queried == ORDER_NOT_FOUND:
            position = self._get_real_position(symbol, fallback_position_side)
            if position is not None and position['position_amt'] == 0:
                self.db.update_live_execution_log(execution_id, status=STATUS_ENTRY_FAILED, error_code='ORDER_NOT_FOUND_NO_POSITION')
                return self._store_result(ExecutionResult(status=STATUS_ENTRY_FAILED, execution_id=execution_id, symbol=symbol, reasons=['ORDER_NOT_FOUND_NO_POSITION']))
            if position is not None and position['position_amt'] != 0:
                return self._confirm_and_protect(execution_id, setup_id, symbol, fallback_stop_loss, fallback_side, position_mode, fallback_position_side, abs(position['position_amt']), '', fallback_tp1, fallback_tp2)
            return self._stay_ambiguous(execution_id, symbol, 'POSITION_CHECK_UNAVAILABLE')

        if queried is None:
            return self._stay_ambiguous(execution_id, symbol, 'RECONCILIATION_UNAVAILABLE')

        order_status = queried.get('status')

        if order_status in FILLED_STATUSES:
            try:
                executed_qty = float(queried.get('executedQty', 0) or 0)
            except (TypeError, ValueError):
                executed_qty = 0.0
            if executed_qty > 0:
                entry_order_id = str(queried.get('orderId', '') or '')
                return self._confirm_and_protect(execution_id, setup_id, symbol, fallback_stop_loss, fallback_side, position_mode, fallback_position_side, executed_qty, entry_order_id, fallback_tp1, fallback_tp2)
            return self._stay_ambiguous(execution_id, symbol, 'FILL_UNVERIFIED')

        if order_status == 'NEW':
            return self._stay_ambiguous(execution_id, symbol, 'ORDER_STILL_PENDING')

        if order_status in TERMINAL_NO_FILL_STATUSES:
            position = self._get_real_position(symbol, fallback_position_side)
            if position is not None and position['position_amt'] == 0:
                self.db.update_live_execution_log(execution_id, status=STATUS_ENTRY_FAILED, error_code=f'ORDER_{order_status}')
                return self._store_result(ExecutionResult(status=STATUS_ENTRY_FAILED, execution_id=execution_id, symbol=symbol, reasons=[f'ORDER_{order_status}']))
            if position is not None and position['position_amt'] != 0:
                entry_order_id = str(queried.get('orderId', '') or '')
                return self._confirm_and_protect(execution_id, setup_id, symbol, fallback_stop_loss, fallback_side, position_mode, fallback_position_side, abs(position['position_amt']), entry_order_id, fallback_tp1, fallback_tp2)
            return self._stay_ambiguous(execution_id, symbol, 'POSITION_CHECK_UNAVAILABLE')

        return self._stay_ambiguous(execution_id, symbol, 'UNRECOGNISED_ORDER_STATUS')

    def _stay_ambiguous(self, execution_id: str, symbol: str, reason: str) -> ExecutionResult:
        self.db.update_live_execution_log(execution_id, status=STATUS_ENTRY_TIMEOUT_UNCONFIRMED, error_code=reason)
        return self._store_result(ExecutionResult(status=STATUS_ENTRY_TIMEOUT_UNCONFIRMED, execution_id=execution_id, symbol=symbol, reasons=[reason]))

    def _query_order_safely(self, symbol: str, client_order_id: str) -> dict[str, Any] | str | None:
        """Returns the order dict, the ORDER_NOT_FOUND sentinel (Binance
        confirmed no such order — code -2013), or None (inconclusive: some
        other error, e.g. network/timeout — never treated as "not found")."""
        try:
            return self.trading_client.get_order_status(symbol, client_order_id=client_order_id)
        except LiveTradingAPIError as exc:
            if exc.code == ORDER_NOT_FOUND_CODE:
                return ORDER_NOT_FOUND
            return None
        except LiveTradingError:
            return None

    # -- confirmation / protection --------------------------------------

    def _confirm_and_protect(
        self, execution_id: str, setup_id: Any, symbol: str, stop_loss: float | None, entry_side: str | None,
        position_mode: str, position_side: str | None, filled_qty: float, entry_order_id: str,
        take_profit_1: float | None = None, take_profit_2: float | None = None,
    ) -> ExecutionResult:
        self.db.update_live_execution_log(execution_id, status='ENTRY_CONFIRMED', entry_order_id=entry_order_id or None, confirmed_at=self._now())
        return self._protect_position(execution_id, setup_id, symbol, stop_loss, entry_side, position_mode, position_side, filled_qty, entry_order_id, take_profit_1, take_profit_2)

    def _protect_position(
        self, execution_id: str, setup_id: Any, symbol: str, stop_loss: float | None, entry_side: str | None,
        position_mode: str, position_side: str | None, filled_qty: float, entry_order_id: str,
        take_profit_1: float | None = None, take_profit_2: float | None = None,
    ) -> ExecutionResult:
        if not stop_loss or not entry_side or not position_side:
            self.db.update_live_execution_log(
                execution_id, status=STATUS_CRITICAL_UNPROTECTED_POSITION,
                error_code='MISSING_PROTECTION_CONTEXT', error_message='Entry confirmed but stop-loss price/side unavailable',
            )
            return self._store_result(ExecutionResult(
                status=STATUS_CRITICAL_UNPROTECTED_POSITION, execution_id=execution_id, symbol=symbol,
                entry_order_id=entry_order_id, error_message='Entry confirmed but stop-loss price/side unavailable',
            ))

        closing_side = 'SELL' if entry_side == 'BUY' else 'BUY'
        reduce_only = self._reduce_only_flag(position_mode)
        sl_client_order_id = self._client_order_id_for(setup_id, 'sl')

        try:
            sl_response = self.trading_client.place_stop_loss(
                symbol=symbol, side=closing_side, position_side=position_side,
                quantity=filled_qty, stop_price=stop_loss,
                reduce_only=reduce_only, client_order_id=sl_client_order_id,
            )
        except LiveTradingError as exc:
            # The SL request itself may have reached Binance despite the error —
            # check before declaring it definitively missing.
            queried_sl = self._query_order_safely(symbol, sl_client_order_id)
            if isinstance(queried_sl, dict) and queried_sl.get('status') in ({'NEW'} | FILLED_STATUSES):
                sl_order_id = str(queried_sl.get('orderId', '') or '')
                self.db.update_live_execution_log(execution_id, status='SL_PLACED', sl_order_id=sl_order_id)
            else:
                # CRITICAL: a confirmed entry with NO protective stop must be
                # surfaced loudly, never silently retried with a new entry.
                self.db.update_live_execution_log(
                    execution_id, status=STATUS_CRITICAL_UNPROTECTED_POSITION,
                    error_code=type(exc).__name__, error_message=f'Stop-loss placement failed after confirmed entry: {exc}',
                )
                return self._store_result(ExecutionResult(
                    status=STATUS_CRITICAL_UNPROTECTED_POSITION, execution_id=execution_id, symbol=symbol,
                    entry_order_id=entry_order_id, error_message=f'Stop-loss placement failed after confirmed entry: {exc}',
                ))
        else:
            sl_order_id = str(sl_response.get('orderId', '') or '')
            self.db.update_live_execution_log(execution_id, status='SL_PLACED', sl_order_id=sl_order_id)

        tp1_order_id = self._place_take_profit_leg(execution_id, setup_id, symbol, closing_side, position_side, reduce_only, filled_qty * (TP1_CLOSE_PERCENT / 100), take_profit_1, 'tp1')
        tp2_order_id = self._place_take_profit_leg(execution_id, setup_id, symbol, closing_side, position_side, reduce_only, filled_qty, take_profit_2, 'tp2')

        self.db.update_live_execution_log(execution_id, status=STATUS_PROTECTED, tp1_order_id=tp1_order_id, tp2_order_id=tp2_order_id)
        return self._store_result(ExecutionResult(
            status=STATUS_PROTECTED, execution_id=execution_id, symbol=symbol,
            entry_order_id=entry_order_id, sl_order_id=sl_order_id,
            tp1_order_id=tp1_order_id, tp2_order_id=tp2_order_id,
        ))

    def _place_take_profit_leg(self, execution_id, setup_id, symbol, closing_side, position_side, reduce_only, quantity, price, label) -> str | None:
        if not price or quantity <= 0:
            # No TP level known (e.g. protecting a position recovered from a
            # CRITICAL_UNPROTECTED_POSITION state, where only the SL price is
            # persisted) — best-effort/skipped. SL already protects the
            # position, which is the critical guarantee, not TP.
            return None
        client_order_id = self._client_order_id_for(setup_id, label)
        try:
            response = self.trading_client.place_take_profit(
                symbol=symbol, side=closing_side, position_side=position_side,
                quantity=quantity, stop_price=price, reduce_only=reduce_only, client_order_id=client_order_id,
            )
            return str(response.get('orderId', '') or '')
        except LiveTradingError as exc:
            # SL is already in place, so the position is protected even if a TP
            # leg fails — but this must not vanish silently from the audit log.
            self.db.update_live_execution_log(execution_id, error_code=f'{label.upper()}_FAILED', error_message=str(exc))
            return None

    def _reconcile_existing(self, existing_log: dict[str, Any]) -> ExecutionResult:
        return self._store_result(ExecutionResult(
            status=STATUS_ALREADY_EXECUTED,
            execution_id=existing_log.get('execution_id'),
            symbol=existing_log.get('symbol'),
            entry_order_id=existing_log.get('entry_order_id'),
            sl_order_id=existing_log.get('sl_order_id'),
            tp1_order_id=existing_log.get('tp1_order_id'),
            tp2_order_id=existing_log.get('tp2_order_id'),
            reasons=['ALREADY_EXECUTED_FOR_THIS_SETUP'],
        ))

    # -- CRITICAL_UNPROTECTED_POSITION recovery ------------------------------

    def _resolve_unprotected_positions(self) -> bool:
        """Attempts to protect every execution currently flagged
        CRITICAL_UNPROTECTED_POSITION. Returns True if at least one remains
        unresolved (which blocks all new entries — see attempt_entry())."""
        rows = self.db.get_live_execution_logs_by_status(STATUS_CRITICAL_UNPROTECTED_POSITION)
        still_unprotected = False
        for row in rows:
            if not self._try_protect_existing_position(dict(row)):
                still_unprotected = True
        return still_unprotected

    def _try_protect_existing_position(self, row: dict[str, Any]) -> bool:
        """Returns True once resolved: either genuinely protected with an SL,
        or the position is confirmed to no longer be open. Never closes the
        position itself and never invents a new entry — see requirement 3."""
        symbol = row['symbol']
        execution_id = row['execution_id']
        stored_position_side = row.get('position_side')

        if self.trading_client is None or self.account_client is None:
            return False

        position = self._get_real_position(symbol, stored_position_side)
        if position is None:
            return False  # can't verify -> stays critical, never guessed
        if position['position_amt'] == 0:
            self.db.update_live_execution_log(execution_id, status=STATUS_POSITION_CLOSED_EXTERNALLY)
            return True
        if row.get('sl_order_id'):
            self.db.update_live_execution_log(execution_id, status=STATUS_PROTECTED)
            return True

        stop_loss = row.get('stop_loss')
        side = row.get('side')
        if not stop_loss or not side:
            return False  # nothing to protect with -> stays critical

        closing_side = 'SELL' if side == 'BUY' else 'BUY'
        position_mode = self._detect_position_mode()
        if position_mode is None:
            return False
        resolved_position_side = stored_position_side or self._resolve_position_side('LONG' if side == 'BUY' else 'SHORT', position_mode)
        reduce_only = self._reduce_only_flag(position_mode)
        quantity = abs(position['position_amt'])
        sl_client_order_id = self._client_order_id_for(row.get('setup_id'), 'sl')

        try:
            sl_response = self.trading_client.place_stop_loss(
                symbol=symbol, side=closing_side, position_side=resolved_position_side,
                quantity=quantity, stop_price=stop_loss, reduce_only=reduce_only,
                client_order_id=sl_client_order_id,
            )
        except LiveTradingError as exc:
            queried_sl = self._query_order_safely(symbol, sl_client_order_id)
            if isinstance(queried_sl, dict) and queried_sl.get('status') in ({'NEW'} | FILLED_STATUSES):
                sl_order_id = str(queried_sl.get('orderId', '') or '')
                self.db.update_live_execution_log(execution_id, status=STATUS_PROTECTED, sl_order_id=sl_order_id)
                return True
            self.db.update_live_execution_log(execution_id, error_code=type(exc).__name__, error_message=f'SL recovery attempt failed: {exc}')
            return False

        sl_order_id = str(sl_response.get('orderId', '') or '')
        self.db.update_live_execution_log(execution_id, status=STATUS_PROTECTED, sl_order_id=sl_order_id)
        return True

    # -- cancellation (bot's own protective orders only) ---------------------

    def cancel_protective_orders(self, execution_id: str) -> list[str]:
        """Cancels ONLY order ids this engine itself logged for the given
        execution — never an arbitrary/unknown order id."""
        row = self.db.get_live_execution_log(execution_id)
        if row is None or self.trading_client is None:
            return []
        cancelled: list[str] = []
        symbol = row['symbol']
        for field_name in ('sl_order_id', 'tp1_order_id', 'tp2_order_id'):
            order_id = row[field_name]
            if not order_id:
                continue
            try:
                self.trading_client.cancel_order(symbol, order_id=order_id)
                cancelled.append(order_id)
            except LiveTradingError:
                continue
        return cancelled

    # -- misc -----------------------------------------------------------

    def _blocked(self, symbol: str, reasons: list[str], intent: OrderIntent | None = None) -> ExecutionResult:
        return self._store_result(ExecutionResult(status=STATUS_BLOCKED, symbol=symbol, reasons=reasons))

    def _store_result(self, result: ExecutionResult) -> ExecutionResult:
        self._last_execution_result = result
        return result
