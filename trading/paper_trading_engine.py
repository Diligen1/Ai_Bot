"""Paper Trading Engine V1 — virtual execution only.

Turns an approved RiskDecision into a tracked, fully virtual position:
entry only when the setup's entry zone is actually touched (READY status,
computed by TradeSetupEngine — never chased), partial TP1/TP2 exits, stop
loss, fees/slippage, and restart-safe persistence. No Binance account, no
real orders, no AI — this module never imports a private/account client,
only the public-data services already used elsewhere in the project.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Thread
from time import sleep
from typing import TYPE_CHECKING, Any, Callable

from config.settings import (
    DEFAULT_RISK_CONFIG,
    PAPER_ENGINE_INTERVAL_SECONDS,
    RiskConfig,
    STRATEGY_VERSION,
    TP1_CLOSE_PERCENT,
    TP2_CLOSE_PERCENT,
)
from database.db import DatabaseManager
from market.setup_engine import TradeSetupEngine
from trading.virtual_portfolio import VirtualPortfolio
from trading.virtual_spot_vault import VirtualSpotVault

if TYPE_CHECKING:
    # Import-time only: risk.risk_manager imports trading.virtual_portfolio, so a
    # real (runtime) import here would be circular. RiskManager is only ever used
    # as a type hint — the engine receives an already-constructed instance.
    from risk.risk_manager import RiskManager


@dataclass(frozen=True)
class PaperFill:
    symbol: str
    direction: str
    planned_entry: float | None
    fill_price: float
    quantity: float
    notional: float
    margin_used: float
    leverage: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    fees_open: float
    slippage_open: float
    opened_at: str
    setup_id: int | None
    risk_decision_snapshot: dict[str, Any]


class PaperTradingEngine:
    """Manages the full lifecycle of virtual paper positions.

    The background loop always runs and always manages already-open
    positions (SL/TP, unrealized PnL) regardless of the enable switch or
    Kill Switch — only *opening a new position* is gated by
    `paper_trading_enabled`. This matches the requirement that existing
    positions keep being risk-managed even while new entries are blocked.
    """

    def __init__(
        self,
        db: DatabaseManager,
        portfolio: VirtualPortfolio,
        risk_manager: RiskManager,
        setup_engine: TradeSetupEngine,
        market_service: Any,
        analysis_service: Any,
        symbols_provider: Callable[[], list[str]],
        config: RiskConfig = DEFAULT_RISK_CONFIG,
        interval_seconds: float = PAPER_ENGINE_INTERVAL_SECONDS,
        spot_vault: VirtualSpotVault | None = None,
    ) -> None:
        self.db = db
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.setup_engine = setup_engine
        self.market_service = market_service
        self.analysis_service = analysis_service
        self.symbols_provider = symbols_provider
        self.config = config
        self.interval_seconds = interval_seconds
        self.spot_vault = spot_vault or VirtualSpotVault(db)
        self._stop = False
        self._started = False
        self._thread: Thread | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
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

    def _loop(self) -> None:
        while not self._stop:
            try:
                self.run_once()
            except Exception:
                pass  # a bad tick must never kill the background loop
            sleep(self.interval_seconds)

    # -- main tick --------------------------------------------------------

    def run_once(self, now: datetime | None = None) -> None:
        """Runs exactly one synchronous tick. No sleeping, no threading — safe to call directly from tests."""
        now = now or datetime.now(timezone.utc)
        for symbol in self.symbols_provider():
            try:
                self._process_symbol(symbol, now)
            except Exception:
                pass  # one bad symbol must never block the rest of this tick

    def _process_symbol(self, symbol: str, now: datetime) -> None:
        existing = self.db.get_open_paper_positions(symbol=symbol)
        if existing:
            self._manage_position(dict(existing[0]), now)
        else:
            self._attempt_entry(symbol, now)

    # -- entry --------------------------------------------------------------

    def _attempt_entry(self, symbol: str, now: datetime) -> None:
        if not self.market_service.is_data_fresh(symbol):
            return
        market_data = self.market_service.get_symbol_data(symbol) or {}
        analysis = self.analysis_service.get_analysis(symbol) or {}
        if not analysis:
            return

        setup = self.setup_engine.build_setup(analysis, market_data)
        market_status = analysis.get('status', 'STALE')
        risk_decision = self.risk_manager.evaluate(symbol, setup, market_status=market_status, now=now)

        if not self.db.get_paper_trading_enabled():
            return
        if setup.get('status') != 'READY':
            return
        if not risk_decision.get('approved'):
            return

        self._execute_fill(symbol, setup, risk_decision, market_data, now)

    def _execute_fill(
        self,
        symbol: str,
        setup: dict[str, Any],
        risk_decision: dict[str, Any],
        market_data: dict[str, Any],
        now: datetime,
    ) -> PaperFill | None:
        current_price = market_data.get('last_price')
        if current_price is None or current_price <= 0:
            return None

        direction = setup['direction']
        quantity = risk_decision.get('position_quantity') or 0.0
        if quantity <= 0:
            return None

        stop_loss = setup['stop_loss']
        take_profit_1 = setup['take_profit_1']
        take_profit_2 = setup['take_profit_2']

        fill_price = self._apply_slippage(current_price, direction, is_entry=True)
        leverage = self.config.leverage

        # RiskManager.evaluate() sized `quantity` against the theoretical entry
        # price. Re-validate the hard invariants against the ACTUAL slippage-
        # adjusted fill_price before ever writing a position — margin_used can
        # otherwise creep past max_margin by up to the slippage amount.
        state = self.portfolio.get_state(now=now)
        current_equity = state.current_equity
        max_margin = current_equity * self.config.max_margin_percent / 100
        risk_budget = current_equity * self.config.risk_per_trade_percent / 100

        notional = quantity * fill_price
        margin_used = notional / leverage if leverage else 0.0

        if leverage and margin_used > max_margin + 1e-9:
            max_quantity_by_margin = (max_margin * leverage) / fill_price if fill_price else 0.0
            quantity = min(quantity, max_quantity_by_margin)
            notional = quantity * fill_price
            margin_used = notional / leverage if leverage else 0.0

        fees_open = notional * self.config.estimated_fee_percent / 100
        slippage_open = abs(fill_price - current_price) * quantity

        stop_distance = abs(fill_price - stop_loss)
        stop_distance_percent = (stop_distance / fill_price * 100) if fill_price else 0.0
        loss_rate_percent = stop_distance_percent + self.config.estimated_fee_percent + self.config.estimated_slippage_percent
        planned_loss_at_stop = notional * (loss_rate_percent / 100)

        safe_to_open = (
            quantity > 0
            and margin_used <= max_margin + 1e-9
            and planned_loss_at_stop <= risk_budget + 1e-9
            and leverage <= self.config.leverage
        )
        if not safe_to_open:
            return None

        opened_at = now.isoformat()

        setup_id = self.db.add_setup(
            symbol=symbol,
            direction=direction,
            setup_type=setup.get('setup_type', 'NO_SETUP'),
            entry_zone_low=setup.get('entry_zone_low'),
            entry_zone_high=setup.get('entry_zone_high'),
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            risk_reward_tp1=setup.get('risk_reward_tp1'),
            risk_reward_tp2=setup.get('risk_reward_tp2'),
            invalidation_level=setup.get('invalidation_level'),
            confidence=setup.get('confidence', 'LOW'),
            technical_score=setup.get('technical_score'),
            setup_score=setup.get('setup_score'),
            status=setup.get('status'),
            created_at=setup.get('created_at'),
            expires_at=setup.get('expires_at'),
            rejection_reasons=json.dumps(setup.get('rejection_reasons', []), ensure_ascii=False),
            analysis_snapshot=json.dumps(setup.get('analysis_snapshot', {}), ensure_ascii=False, default=str),
            execution_status='EXECUTED',
        )

        session_id = self.db.get_or_create_active_session(current_equity, started_at=opened_at)

        self.db.add_paper_position(
            session_id=session_id,
            symbol=symbol,
            direction=direction,
            status='OPEN',
            entry_price=fill_price,
            planned_entry=risk_decision.get('entry_price'),
            quantity_initial=quantity,
            quantity_remaining=quantity,
            margin_used=margin_used,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            opened_at=opened_at,
            setup_id=setup_id,
            strategy_version=STRATEGY_VERSION,
            setup_snapshot=json.dumps(setup, ensure_ascii=False, default=str),
            risk_decision_snapshot=json.dumps(risk_decision, ensure_ascii=False, default=str),
            fees_total=fees_open,
        )

        return PaperFill(
            symbol=symbol,
            direction=direction,
            planned_entry=risk_decision.get('entry_price'),
            fill_price=fill_price,
            quantity=quantity,
            notional=notional,
            margin_used=margin_used,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            fees_open=fees_open,
            slippage_open=slippage_open,
            opened_at=opened_at,
            setup_id=setup_id,
            risk_decision_snapshot=risk_decision,
        )

    # -- position management ------------------------------------------------

    def _manage_position(self, position: dict[str, Any], now: datetime) -> None:
        symbol = position['symbol']
        market_data = self.market_service.get_symbol_data(symbol) or {}
        current_price = market_data.get('last_price')
        if current_price is None or current_price <= 0:
            return

        direction = position['direction']
        stop_loss = position['stop_loss']
        tp1 = position['take_profit_1']
        tp2 = position['take_profit_2']
        tp1_hit = bool(position['tp1_hit'])
        next_tp = tp2 if tp1_hit else tp1

        candle = self._latest_closed_candle(market_data)
        ambiguous = self._is_intrabar_ambiguous(direction, stop_loss, next_tp, candle)
        sl_hit = ambiguous or self._is_stop_hit(direction, current_price, stop_loss)

        if sl_hit:
            self._close_leg(position, current_price, position['quantity_remaining'], 'STOP_LOSS', now)
            return

        remaining = position['quantity_remaining']
        if not tp1_hit and self._is_tp_hit(direction, current_price, tp1):
            close_qty = min(remaining, position['quantity_initial'] * (TP1_CLOSE_PERCENT / 100))
            self._close_leg(position, current_price, close_qty, 'TP1', now)
            refreshed = self.db.get_paper_position(position['id'])
            if refreshed is None:
                return
            position = dict(refreshed)
            remaining = position['quantity_remaining']

        if remaining > 0 and self._is_tp_hit(direction, current_price, tp2):
            self._close_leg(position, current_price, remaining, 'TP2', now)

    def _close_leg(
        self,
        position: dict[str, Any],
        current_price: float,
        quantity: float,
        reason: str,
        now: datetime,
    ) -> None:
        if quantity <= 0:
            return
        direction = position['direction']
        entry_price = position['entry_price']
        exit_price = self._apply_slippage(current_price, direction, is_entry=False)

        if direction == 'LONG':
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity

        notional_closed = quantity * exit_price
        fees_close = notional_closed * self.config.estimated_fee_percent / 100
        quantity_initial = position['quantity_initial'] or 1.0
        original_fees_open = quantity_initial * entry_price * self.config.estimated_fee_percent / 100
        fees_open_allocated = original_fees_open * (quantity / quantity_initial)
        net_pnl = gross_pnl - fees_close - fees_open_allocated

        closed_at = now.isoformat()

        try:
            snapshot = json.loads(position.get('setup_snapshot') or '{}')
        except (TypeError, ValueError):
            snapshot = {}

        trade_id = self.db.add_trade(
            symbol=position['symbol'],
            side='BUY' if direction == 'LONG' else 'SELL',
            status='closed',
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            leverage=position['leverage'],
            margin_used=position['margin_used'] * (quantity / quantity_initial),
            stop_loss=position['stop_loss'],
            take_profit=position['take_profit_1'] if reason == 'TP1' else position['take_profit_2'],
            realized_pnl=gross_pnl,
            net_pnl=net_pnl,
            fees=fees_close + fees_open_allocated,
            funding=0.0,
            entry_reason=snapshot.get('setup_type'),
            exit_reason=reason,
            opened_at=position['opened_at'],
            closed_at=closed_at,
            source='paper',
            position_id=position['id'],
            setup_id=position.get('setup_id'),
            strategy_version=position.get('strategy_version') or STRATEGY_VERSION,
            setup_score=snapshot.get('setup_score'),
        )

        self.spot_vault.process_closed_trade(
            {'id': trade_id, 'source': 'paper', 'symbol': position['symbol'], 'net_pnl': net_pnl},
            now=now,
        )

        new_remaining = max(0.0, position['quantity_remaining'] - quantity)
        updates: dict[str, Any] = {
            'quantity_remaining': new_remaining,
            'realized_pnl': position['realized_pnl'] + net_pnl,
            'fees_total': position['fees_total'] + fees_close,
        }
        if reason == 'TP1':
            updates['tp1_hit'] = 1
            updates['status'] = 'PARTIALLY_CLOSED' if new_remaining > 1e-12 else 'CLOSED'
        elif reason == 'TP2':
            updates['tp2_hit'] = 1
            updates['status'] = 'CLOSED'
        else:
            updates['status'] = 'STOPPED'

        if updates['status'] in ('CLOSED', 'STOPPED'):
            updates['closed_at'] = closed_at

        self.db.update_paper_position(position['id'], **updates)

    # -- pricing helpers ------------------------------------------------

    def _apply_slippage(self, price: float, direction: str, is_entry: bool) -> float:
        """Adverse (never favorable) price adjustment: LONG buys/SHORT sells on entry,
        LONG sells/SHORT buys on exit — the fill is always slightly worse than quoted."""
        slip = self.config.estimated_slippage_percent / 100
        buying = (direction == 'LONG') == is_entry
        return price * (1 + slip) if buying else price * (1 - slip)

    @staticmethod
    def _is_stop_hit(direction: str, price: float, stop_loss: float) -> bool:
        return price <= stop_loss if direction == 'LONG' else price >= stop_loss

    @staticmethod
    def _is_tp_hit(direction: str, price: float, target: float) -> bool:
        return price >= target if direction == 'LONG' else price <= target

    @staticmethod
    def _latest_closed_candle(market_data: dict[str, Any]) -> dict[str, Any] | None:
        candles = market_data.get('candles_15m') or []
        if len(candles) < 2:
            return None
        return candles[-2]

    @staticmethod
    def _is_intrabar_ambiguous(
        direction: str,
        stop_loss: float,
        tp_target: float,
        candle: dict[str, Any] | None,
    ) -> bool:
        """True when a single 15m candle's [low, high] range touched both the stop
        and the relevant take-profit — we can't know which happened first from OHLC
        alone, so the caller must treat this as a stop-loss (conservative)."""
        if not candle:
            return False
        low = candle.get('low')
        high = candle.get('high')
        if low is None or high is None:
            return False
        if direction == 'LONG':
            sl_touched = low <= stop_loss
            tp_touched = high >= tp_target
        else:
            sl_touched = high >= stop_loss
            tp_touched = low <= tp_target
        return sl_touched and tp_touched

    @staticmethod
    def calculate_unrealized_pnl(position: dict[str, Any], current_price: float) -> float:
        quantity = position['quantity_remaining']
        if position['direction'] == 'LONG':
            return (current_price - position['entry_price']) * quantity
        return (position['entry_price'] - current_price) * quantity
