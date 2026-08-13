"""Live Execution Engine V1 — DRY RUN ONLY.

Builds an OrderIntent (execution/order_intent.py) describing what a REAL
Binance Futures order would look like, from an already-built TradeSetup and
REAL account risk state (risk/live_risk_guard.py). `DRY_RUN_EXECUTION` is
always True in V1: this module never makes an HTTP request to any Binance
TRADING endpoint (no POST /order, no leverage change, no cancel, no
transfer/withdraw) — the only network-capable objects it holds are
`LiveRiskGuard` (wraps the READ-ONLY BinanceAccountConnector) and
`BinanceSymbolRules` (wraps the PUBLIC BinancePublicClient). Neither one
has, or could have, a method that places an order. See
tests/test_live_execution_engine.py::test_dry_run_never_sends_order and
test_engine_has_no_order_sending_methods.

An OrderIntent is only ever READY when ALL of the following hold:
  - the Binance account snapshot is CONNECTED and Kill Switch is OFF
  - the TradeSetup status is READY (zone touched, not chased)
  - market data for the symbol is fresh
  - the symbol is in the whitelist
  - there is no existing REAL open position on the symbol
  - the number of open REAL positions is below the configured maximum
  - after BinanceSymbolRules-correct rounding, quantity/min-qty/notional/
    margin/planned-loss all still satisfy the same RiskConfig limits used
    everywhere else in this project
Otherwise it is REJECTED with `rejection_reasons` explaining why. Either
way, DRY RUN means the result is only ever printed/displayed — never sent.

VirtualPortfolio/paper positions are never read here — real account state
comes exclusively from LiveRiskGuard, matching the same separation already
enforced there (see risk/live_risk_guard.py).
"""
from __future__ import annotations

from typing import Any

from binance.symbol_rules import BinanceSymbolRules
from config.settings import DEFAULT_RISK_CONFIG, RiskConfig
from database.db import DatabaseManager
from execution.order_intent import OrderIntent, STATUS_READY, STATUS_REJECTED
from risk.live_risk_guard import LiveRiskGuard
from risk.risk_manager import PositionSizeCalculator

DRY_RUN_EXECUTION = True

NOT_READY_SETUP_STATUSES = {'WAITING', 'INVALIDATED', 'EXPIRED', 'REJECTED', 'MISSED_ENTRY'}


class LiveExecutionEngine:
    def __init__(
        self,
        live_risk_guard: LiveRiskGuard,
        symbol_rules: BinanceSymbolRules,
        db: DatabaseManager,
        market_service: Any,
        config: RiskConfig = DEFAULT_RISK_CONFIG,
    ) -> None:
        self.live_risk_guard = live_risk_guard
        self.symbol_rules = symbol_rules
        self.db = db
        self.market_service = market_service
        self.config = config
        self._sizer = PositionSizeCalculator(config)
        self._last_intent: OrderIntent | None = None

    def get_last_intent(self) -> OrderIntent | None:
        return self._last_intent

    def build_order_intent(self, symbol: str, setup: dict[str, Any]) -> OrderIntent:
        """Evaluates every gate; ALWAYS returns an OrderIntent (READY or
        REJECTED), NEVER sends anything. DRY_RUN_EXECUTION is checked again
        right before the DRY RUN report is printed, as a final belt-and-
        braces guard even though nothing here is capable of sending orders."""
        reasons: list[str] = []
        direction = setup.get('direction')

        risk_snapshot = self.live_risk_guard.get_snapshot()
        if risk_snapshot.status != 'CONNECTED':
            reasons.append('BINANCE_NOT_CONNECTED')
        if risk_snapshot.kill_switch_on:
            reasons.append('KILL_SWITCH_ON')

        setup_status = setup.get('status')
        if not setup.get('setup_type') or setup.get('setup_type') == 'NO_SETUP' or setup_status in NOT_READY_SETUP_STATUSES or setup_status != 'READY':
            reasons.append('SETUP_NOT_READY')

        if not self.market_service.is_data_fresh(symbol):
            reasons.append('STALE_MARKET_DATA')

        whitelist = {row['symbol'] for row in self.db.get_whitelist_symbols()}
        if symbol not in whitelist:
            reasons.append('SYMBOL_NOT_WHITELISTED')

        if risk_snapshot.status == 'CONNECTED':
            if any(position.symbol == symbol for position in risk_snapshot.positions):
                reasons.append('DUPLICATE_LIVE_POSITION')
            if risk_snapshot.open_positions_count >= risk_snapshot.max_positions_limit:
                reasons.append('MAX_OPEN_POSITIONS')

        entry_zone_low = setup.get('entry_zone_low')
        entry_zone_high = setup.get('entry_zone_high')
        stop_loss = setup.get('stop_loss')
        take_profit_1 = setup.get('take_profit_1') or 0.0
        take_profit_2 = setup.get('take_profit_2') or 0.0
        valid_setup_prices = (
            direction in ('LONG', 'SHORT')
            and entry_zone_low and entry_zone_high and stop_loss
            and entry_zone_low > 0 and entry_zone_high > 0 and stop_loss > 0
        )
        if not valid_setup_prices:
            reasons.append('INVALID_SETUP_PRICES')

        # Only spend a public exchangeInfo lookup + do sizing math once the
        # cheap structural gates above are all clean.
        if reasons:
            return self._finalize(symbol, setup, direction, reasons)

        rules = self.symbol_rules.get_rules(symbol)
        if rules is None:
            reasons.append('SYMBOL_RULES_UNAVAILABLE')
        elif rules.status != 'TRADING':
            reasons.append('SYMBOL_NOT_TRADING')
        if reasons:
            return self._finalize(symbol, setup, direction, reasons)

        entry_price = entry_zone_high if direction == 'LONG' else entry_zone_low
        sizing = self._sizer.calculate(entry_price=entry_price, stop_loss=stop_loss, current_equity=risk_snapshot.real_equity)

        rounded_price = float(rules.round_price(entry_price))
        rounded_stop = float(rules.round_price(stop_loss))
        rounded_tp1 = float(rules.round_price(take_profit_1)) if take_profit_1 else 0.0
        rounded_tp2 = float(rules.round_price(take_profit_2)) if take_profit_2 else 0.0
        rounded_quantity = float(rules.round_quantity(sizing['quantity']))

        if rounded_quantity <= 0:
            reasons.append('QUANTITY_ROUNDS_TO_ZERO')
        elif rounded_quantity < float(rules.min_qty):
            reasons.append('QUANTITY_BELOW_MIN_QTY')

        notional = rounded_quantity * rounded_price
        if notional < float(rules.min_notional):
            reasons.append('NOTIONAL_BELOW_MINIMUM')

        max_margin = risk_snapshot.real_equity * self.config.max_margin_percent / 100
        margin_required = notional / self.config.leverage if self.config.leverage else 0.0
        if margin_required > max_margin + 1e-9:
            reasons.append('MARGIN_EXCEEDS_LIMIT')

        stop_distance = abs(rounded_price - rounded_stop) if rounded_price and rounded_stop else 0.0
        stop_distance_percent = (stop_distance / rounded_price * 100) if rounded_price else 0.0
        loss_rate_percent = stop_distance_percent + self.config.estimated_fee_percent + self.config.estimated_slippage_percent
        planned_loss = notional * (loss_rate_percent / 100)
        risk_budget = risk_snapshot.real_equity * self.config.risk_per_trade_percent / 100
        if planned_loss > risk_budget + 1e-9:
            reasons.append('PLANNED_LOSS_EXCEEDS_BUDGET')

        rounded = {
            'quantity': rounded_quantity,
            'entry_price': rounded_price,
            'stop_loss': rounded_stop,
            'take_profit_1': rounded_tp1,
            'take_profit_2': rounded_tp2,
        }
        risk_decision_snapshot = {
            'real_equity': risk_snapshot.real_equity,
            'max_margin': max_margin,
            'margin_required': margin_required,
            'risk_budget': risk_budget,
            'planned_loss': planned_loss,
            'notional': notional,
        }
        return self._finalize(symbol, setup, direction, reasons, rounded, risk_decision_snapshot)

    def _finalize(
        self,
        symbol: str,
        setup: dict[str, Any],
        direction: str | None,
        reasons: list[str],
        rounded: dict[str, float] | None = None,
        risk_decision_snapshot: dict[str, Any] | None = None,
    ) -> OrderIntent:
        rounded = rounded or {}
        side = 'BUY' if direction == 'LONG' else 'SELL' if direction == 'SHORT' else '-'
        position_side = direction if direction in ('LONG', 'SHORT') else '-'

        intent = OrderIntent(
            status=STATUS_REJECTED if reasons else STATUS_READY,
            symbol=symbol,
            side=side,
            position_side=position_side,
            order_type='LIMIT',
            quantity=rounded.get('quantity', 0.0),
            entry_price=rounded.get('entry_price', 0.0),
            stop_loss=rounded.get('stop_loss', 0.0),
            take_profit_1=rounded.get('take_profit_1', 0.0),
            take_profit_2=rounded.get('take_profit_2', 0.0),
            leverage=self.config.leverage,
            reduce_only=False,
            setup_id=setup.get('id'),
            risk_decision_snapshot=risk_decision_snapshot or {},
            rejection_reasons=reasons,
        )
        self._last_intent = intent
        if intent.status == STATUS_READY:
            self._print_dry_run_report(intent)
        return intent

    @staticmethod
    def _print_dry_run_report(intent: OrderIntent) -> None:
        assert DRY_RUN_EXECUTION is True, 'Live Execution Engine V1 must never leave DRY RUN'
        print('ORDER INTENT READY')
        print(f'symbol: {intent.symbol}')
        print(f'side: {intent.side}')
        print(f'quantity: {intent.quantity}')
        print(f'entry: {intent.entry_price}')
        print(f'SL: {intent.stop_loss}')
        print(f'TP1: {intent.take_profit_1}')
        print(f'TP2: {intent.take_profit_2}')
        print(f'leverage: x{intent.leverage}')
        print(f"estimated margin: {intent.risk_decision_snapshot.get('margin_required')}")
        print(f"planned risk: {intent.risk_decision_snapshot.get('planned_loss')}")
        print('Ордер НЕ отправлен на Binance (DRY_RUN_EXECUTION=True).')
