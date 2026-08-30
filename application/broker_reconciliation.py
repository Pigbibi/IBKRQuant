"""Read-only IBKR observations used by frozen-live reconciliation.

The module deliberately does not decide whether a baseline can resume.  It
only reads the broker surfaces that a higher-level reconciliation contract must
compare with a durable local ledger: configured account identity, positions,
cash, active orders, and recent executions.  Callers must hash the returned
normalised values locally and must never log or return them from an HTTP route.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class IBKRReconciliationReadError(RuntimeError):
    """Raised when a required read-only broker surface cannot be reconciled."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _number(value: object, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise IBKRReconciliationReadError(f"IBKR reconciliation is missing {field_name}.") from exc


def normalize_account_ids(account_ids: Iterable[str] | str | None) -> tuple[str, ...]:
    if account_ids is None:
        return ()
    candidates = (account_ids,) if isinstance(account_ids, str) else tuple(account_ids)
    normalized = tuple(dict.fromkeys(_text(account_id) for account_id in candidates if _text(account_id)))
    if not normalized:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires one or more configured account ids."
        )
    return normalized


def _account_in_scope(account_id: object, *, selected_account_ids: tuple[str, ...], surface: str) -> bool:
    normalized = _text(account_id)
    if not normalized:
        raise IBKRReconciliationReadError(
            f"IBKR reconciliation received an unscoped {surface} record."
        )
    return normalized in selected_account_ids


def _safe_contract_fields(contract: Any) -> dict[str, object]:
    return {
        "con_id": _text(getattr(contract, "conId", "")),
        "symbol": _text(getattr(contract, "symbol", "")).upper(),
        "local_symbol": _text(getattr(contract, "localSymbol", "")),
        "sec_type": _text(getattr(contract, "secType", "")).upper(),
        "currency": _text(getattr(contract, "currency", "")).upper(),
    }


def _normalise_position(position: Any) -> dict[str, object]:
    return {
        "symbol": _text(getattr(position, "symbol", "")).upper(),
        "quantity": _number(getattr(position, "quantity", None), field_name="position quantity"),
        "average_cost": _number(
            getattr(position, "average_cost", None), field_name="position average cost"
        ),
        "currency": _text(getattr(position, "currency", "")).upper(),
    }


def _normalise_option_position(
    position: Mapping[str, object],
    *,
    selected_account_ids: tuple[str, ...],
) -> dict[str, object]:
    account_id = _text(position.get("account_id"))
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="option position"):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation received an out-of-scope option position."
        )
    return {
        "account": account_id,
        "underlier": _text(position.get("underlier")).upper(),
        "local_symbol": _text(position.get("local_symbol")),
        "expiration": _text(position.get("expiration")),
        "right": _text(position.get("right")).upper(),
        "strike": _number(position.get("strike"), field_name="option strike"),
        "quantity": _number(position.get("quantity"), field_name="option quantity"),
        "average_cost": _number(position.get("average_cost"), field_name="option average cost"),
        "currency": _text(position.get("currency")).upper(),
    }


def _normalise_cash_balance(value: Mapping[str, object], *, selected_account_ids: tuple[str, ...]) -> dict[str, object]:
    account_id = _text(value.get("account_id"))
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="cash"):
        raise IBKRReconciliationReadError("IBKR reconciliation received an out-of-scope cash record.")
    numeric_tags = {
        _text(tag): _number(number, field_name=f"cash tag {_text(tag)}")
        for tag, number in value.items()
        if _text(tag) not in {"account_id", "currency"}
    }
    return {
        "account": account_id,
        "currency": _text(value.get("currency")).upper(),
        "tags": dict(sorted(numeric_tags.items())),
    }


def _open_trade_account(trade: Any) -> object:
    order = getattr(trade, "order", None)
    return getattr(order, "account", None) or getattr(trade, "account", None)


def _normalise_open_trade(trade: Any, *, selected_account_ids: tuple[str, ...]) -> dict[str, object] | None:
    account_id = _open_trade_account(trade)
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="open order"):
        return None
    order = getattr(trade, "order", trade)
    status = getattr(trade, "orderStatus", None)
    contract = getattr(trade, "contract", None)
    if contract is None:
        raise IBKRReconciliationReadError("IBKR reconciliation received an open order without a contract.")
    return {
        "account": _text(account_id),
        "contract": _safe_contract_fields(contract),
        "perm_id": _text(getattr(order, "permId", "")),
        "action": _text(getattr(order, "action", "")).upper(),
        "order_type": _text(getattr(order, "orderType", "")).upper(),
        "total_quantity": _number(
            getattr(order, "totalQuantity", None), field_name="open order total quantity"
        ),
        "limit_price": _number(getattr(order, "lmtPrice", 0.0), field_name="open order limit price"),
        "aux_price": _number(getattr(order, "auxPrice", 0.0), field_name="open order aux price"),
        "status": _text(getattr(status, "status", "")),
        "filled": _number(getattr(status, "filled", 0.0), field_name="open order filled quantity"),
        "remaining": _number(
            getattr(status, "remaining", 0.0), field_name="open order remaining quantity"
        ),
    }


def _execution_account(fill: Any) -> object:
    execution = getattr(fill, "execution", None)
    account_id = getattr(execution, "acctNumber", None)
    if _text(account_id):
        return account_id
    order = getattr(getattr(fill, "trade", None), "order", None)
    return getattr(order, "account", None)


def _normalise_execution(fill: Any, *, selected_account_ids: tuple[str, ...]) -> dict[str, object] | None:
    account_id = _execution_account(fill)
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="execution"):
        return None
    execution = getattr(fill, "execution", None)
    contract = getattr(fill, "contract", None)
    if execution is None or contract is None:
        raise IBKRReconciliationReadError("IBKR reconciliation received an incomplete execution record.")
    return {
        "account": _text(account_id),
        "contract": _safe_contract_fields(contract),
        "execution_id": _text(getattr(execution, "execId", "")),
        "order_id": _text(getattr(execution, "orderId", "")),
        "time": _text(getattr(execution, "time", "")),
        "side": _text(getattr(execution, "side", "")).upper(),
        "shares": _number(getattr(execution, "shares", None), field_name="execution shares"),
        "price": _number(getattr(execution, "price", None), field_name="execution price"),
    }


def _sorted_records(records: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return sorted((dict(record) for record in records), key=lambda record: repr(sorted(record.items())))


def _load_all_open_trades(ib: Any) -> tuple[Any, ...]:
    request_all_open_orders = getattr(ib, "reqAllOpenOrders", None)
    open_trades = getattr(ib, "openTrades", None)
    if not callable(request_all_open_orders) or not callable(open_trades):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires read-only all-open-orders support."
        )
    try:
        request_all_open_orders()
        return tuple(open_trades() or ())
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load all active orders."
        ) from exc


def _load_recent_executions(ib: Any) -> tuple[Any, ...]:
    request_executions = getattr(ib, "reqExecutions", None)
    if not callable(request_executions):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires read-only recent-executions support."
        )
    try:
        return tuple(request_executions() or ())
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load recent executions."
        ) from exc


@dataclass(frozen=True)
class IBKRReconciliationObservations:
    """Sensitive, in-memory broker observations; never serialize to responses."""

    account_scope: Mapping[str, object]
    account_identity_match: bool
    positions: tuple[Mapping[str, object], ...]
    cash: tuple[Mapping[str, object], ...]
    open_orders: tuple[Mapping[str, object], ...]
    recent_executions: tuple[Mapping[str, object], ...]


def collect_read_only_reconciliation_observations(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None,
    fetch_portfolio_snapshot: Callable[..., Any],
    market_currency: str,
    cash_only_execution: bool,
) -> IBKRReconciliationObservations:
    """Read and normalise all broker surfaces needed by a continuity check.

    ``fetch_portfolio_snapshot`` is injected so this adapter uses the existing
    market-aware platform portfolio reader.  This keeps reconciliation scoped
    to exactly the account/currency semantics used by execution.
    """

    selected_account_ids = normalize_account_ids(account_ids)
    managed_accounts_fn = getattr(ib, "managedAccounts", None)
    if not callable(managed_accounts_fn):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires a managed-accounts identity response."
        )
    try:
        managed_accounts = {_text(account_id) for account_id in (managed_accounts_fn() or ()) if _text(account_id)}
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load managed-account identity."
        ) from exc
    identity_match = set(selected_account_ids).issubset(managed_accounts)
    snapshot = fetch_portfolio_snapshot(
        ib,
        account_ids=selected_account_ids,
        currency=market_currency,
        cash_only_execution=cash_only_execution,
    )
    metadata = getattr(snapshot, "metadata", {})
    if not isinstance(metadata, Mapping):
        raise IBKRReconciliationReadError("IBKR reconciliation portfolio snapshot metadata is invalid.")
    cash_balances = metadata.get("cash_balances")
    if not isinstance(cash_balances, (list, tuple)):
        raise IBKRReconciliationReadError("IBKR reconciliation is missing scoped cash balances.")
    option_positions = metadata.get("option_positions") or ()
    if not isinstance(option_positions, (list, tuple)):
        raise IBKRReconciliationReadError("IBKR reconciliation option-position metadata is invalid.")

    if any(not isinstance(option_position, Mapping) for option_position in option_positions):
        raise IBKRReconciliationReadError("IBKR reconciliation option-position metadata is invalid.")
    if any(not isinstance(value, Mapping) for value in cash_balances):
        raise IBKRReconciliationReadError("IBKR reconciliation cash-balance metadata is invalid.")
    positions = [_normalise_position(position) for position in (getattr(snapshot, "positions", ()) or ())]
    positions.extend(
        _normalise_option_position(
            option_position,
            selected_account_ids=selected_account_ids,
        )
        for option_position in option_positions
    )
    open_orders = [
        record
        for trade in _load_all_open_trades(ib)
        if (record := _normalise_open_trade(trade, selected_account_ids=selected_account_ids)) is not None
    ]
    executions = [
        record
        for fill in _load_recent_executions(ib)
        if (record := _normalise_execution(fill, selected_account_ids=selected_account_ids)) is not None
    ]
    return IBKRReconciliationObservations(
        account_scope={"account_ids": sorted(selected_account_ids)},
        account_identity_match=identity_match,
        positions=tuple(_sorted_records(positions)),
        cash=tuple(
            _sorted_records(
                _normalise_cash_balance(value, selected_account_ids=selected_account_ids)
                for value in cash_balances
            )
        ),
        open_orders=tuple(_sorted_records(open_orders)),
        recent_executions=tuple(_sorted_records(executions)),
    )


__all__ = [
    "IBKRReconciliationObservations",
    "IBKRReconciliationReadError",
    "collect_read_only_reconciliation_observations",
    "normalize_account_ids",
]
