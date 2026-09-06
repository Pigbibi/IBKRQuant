"""IBKR order submission adapters for platform-specific broker quirks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from application.account_new_risk_gate_support import (
    evaluate_cycle_new_risk_admission,
    is_account_new_risk_gate_enabled,
    new_risk_buy_prohibited,
)
from quant_platform_kit.common.models import ExecutionReport, OrderIntent
from quant_platform_kit.ibkr.execution import submit_order_intent as _submit_order_intent

DEFAULT_TIME_IN_FORCE = "DAY"


def _stock_factory_for_market(
    stock_factory: Callable[..., Any] | None,
    *,
    exchange: str,
    currency: str,
) -> Callable[..., Any]:
    def factory(symbol: str, _exchange: str = "SMART", _currency: str = "USD") -> Any:
        factory_impl = stock_factory
        if factory_impl is None:
            from ib_insync import Stock

            factory_impl = Stock
        return factory_impl(symbol, exchange, currency)

    return factory


def _intent_with_default_time_in_force(order_intent: OrderIntent) -> OrderIntent:
    if order_intent.time_in_force:
        return order_intent
    return replace(order_intent, time_in_force=DEFAULT_TIME_IN_FORCE)


def _market_order_factory_with_time_in_force(
    market_order_factory: Callable[..., Any] | None,
    *,
    time_in_force: str,
) -> Callable[..., Any]:
    def factory(side: str, quantity: float) -> Any:
        factory_impl = market_order_factory
        if factory_impl is None:
            from ib_insync import MarketOrder

            factory_impl = MarketOrder
        order = factory_impl(side, quantity)
        order.tif = time_in_force
        return order

    return factory


def probe_order_write_access(
    ib: Any,
    *,
    symbol: str,
    account_id: str,
    stock_factory: Callable[..., Any] | None = None,
    market_order_factory: Callable[..., Any] | None = None,
    stock_exchange: str = "SMART",
    stock_currency: str = "USD",
) -> Any:
    """Verify order-write access with an IBKR what-if order that cannot execute."""

    what_if_order = getattr(ib, "whatIfOrder", None)
    if not callable(what_if_order):
        raise RuntimeError("IBKR connection does not support what-if orders")

    normalized_symbol = str(symbol or "").strip().upper()
    normalized_account_id = str(account_id or "").strip()
    if not normalized_symbol or not normalized_account_id:
        raise ValueError("IBKR what-if probe requires a symbol and account_id")

    contract = _stock_factory_for_market(
        stock_factory,
        exchange=str(stock_exchange or "SMART").upper(),
        currency=str(stock_currency or "USD").upper(),
    )(normalized_symbol)
    order = _market_order_factory_with_time_in_force(
        market_order_factory,
        time_in_force=DEFAULT_TIME_IN_FORCE,
    )("BUY", 1)
    order.account = normalized_account_id
    order.whatIf = True
    order.transmit = True
    return what_if_order(contract, order)


def submit_order_intent(
    ib: Any,
    order_intent: OrderIntent,
    *,
    account_id: str | None = None,
    wait_seconds: float = 1.0,
    stock_factory: Callable[..., Any] | None = None,
    option_factory: Callable[..., Any] | None = None,
    combo_contract_factory: Callable[..., Any] | None = None,
    combo_leg_factory: Callable[..., Any] | None = None,
    market_order_factory: Callable[..., Any] | None = None,
    limit_order_factory: Callable[..., Any] | None = None,
    stock_exchange: str = "SMART",
    stock_currency: str = "USD",
) -> ExecutionReport:
    """Submit an IBKR order with explicit TIF to avoid account-preset rejections."""

    intent = _intent_with_default_time_in_force(order_intent)
    side_normalized = str(intent.side or "").strip().lower()
    if is_account_new_risk_gate_enabled() and side_normalized.startswith("buy"):
        admission = evaluate_cycle_new_risk_admission()
        if new_risk_buy_prohibited(admission):
            return ExecutionReport(
                symbol=str(intent.symbol or "").strip().upper(),
                side=side_normalized,
                quantity=float(intent.quantity or 0.0),
                status="rejected",
                raw_payload={
                    "detail": "account_new_risk_gate",
                    "reason_codes": list(admission.reason_codes),
                    "live_authority_granted": admission.live_authority_granted,
                },
            )
    return _submit_order_intent(
        ib,
        intent,
        account_id=account_id,
        wait_seconds=wait_seconds,
        stock_factory=_stock_factory_for_market(
            stock_factory,
            exchange=str(stock_exchange or "SMART").upper(),
            currency=str(stock_currency or "USD").upper(),
        ),
        option_factory=option_factory,
        combo_contract_factory=combo_contract_factory,
        combo_leg_factory=combo_leg_factory,
        market_order_factory=_market_order_factory_with_time_in_force(
            market_order_factory,
            time_in_force=intent.time_in_force or DEFAULT_TIME_IN_FORCE,
        ),
        limit_order_factory=limit_order_factory,
    )
