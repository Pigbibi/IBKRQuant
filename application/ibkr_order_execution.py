"""IBKR order submission adapters for platform-specific broker quirks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from quant_platform_kit.common.models import ExecutionReport, OrderIntent
from quant_platform_kit.ibkr.execution import submit_order_intent as _submit_order_intent

from application.broker_reconciliation import (
    IBKRReconciliationReadError,
    build_canonical_order_key,
)

DEFAULT_TIME_IN_FORCE = "DAY"
_ORDER_IDENTITY_REQUIRED_STATUSES = frozenset(
    {
        "ApiPending",
        "ApiPendingSubmit",
        "Filled",
        "Partial",
        "PartiallyFilled",
        "PendingSubmit",
        "PreSubmitted",
        "Submitted",
    }
)


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
    report = _submit_order_intent(
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
    raw_payload = dict(report.raw_payload or {})
    if report.broker_order_id is None and report.status not in _ORDER_IDENTITY_REQUIRED_STATUSES:
        return report
    try:
        order_key = build_canonical_order_key(
            account_id=raw_payload.get("account_id"),
            client_id=getattr(getattr(ib, "client", None), "clientId", None),
            order_id=report.broker_order_id,
            perm_id=raw_payload.get("perm_id"),
        )
    except IBKRReconciliationReadError:
        return replace(
            report,
            status="ReconciliationRequired",
            raw_payload={
                **raw_payload,
                "broker_status": report.status,
                "reconciliation_outcome": "order_identity_unavailable",
            },
        )
    return replace(report, raw_payload={**raw_payload, "order_key": order_key})
