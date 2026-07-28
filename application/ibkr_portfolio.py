"""IBKR portfolio snapshot helpers with market-currency awareness."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from quant_platform_kit.common.models import PortfolioSnapshot, Position

MARKET_CURRENCY_CASH_TAG_PRIORITY = (
    "$LEDGER-CashBalance",
    "$LEDGER-TotalCashBalance",
    "CashBalance",
    "TotalCashBalance",
    "SettledCash",
    "TotalCashValue",
)


class IBKRPortfolioSnapshotUnavailableError(RuntimeError):
    """Raised when IBKR did not return enough account data for safe execution."""


def _normalize_account_ids(account_ids: Iterable[str] | str | None) -> tuple[str, ...]:
    if account_ids is None:
        return ()
    if isinstance(account_ids, str):
        candidates = [account_ids]
    else:
        candidates = list(account_ids)
    normalized = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            normalized.append(text)
    return tuple(dict.fromkeys(normalized))


def _matches_account(account_id: str | None, selected_account_ids: tuple[str, ...]) -> bool:
    if not selected_account_ids:
        return True
    return str(account_id or "").strip() in selected_account_ids


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cash_value_for_currency(
    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]],
    *,
    currency: str,
    tag_priority: tuple[str, ...] = MARKET_CURRENCY_CASH_TAG_PRIORITY,
) -> float | None:
    total = 0.0
    matched = False
    market_currency = str(currency or "").strip().upper()
    for (_account_id, value_currency), tag_values in values_by_account_currency.items():
        if value_currency != market_currency:
            continue
        for tag in tag_priority:
            if tag in tag_values:
                total += float(tag_values[tag])
                matched = True
                break
    return total if matched else None


def fetch_portfolio_snapshot(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None = None,
    wait_seconds: float = 1.0,
    currency: str = "USD",
    cash_only_execution: bool = True,
) -> PortfolioSnapshot:
    """Fetch stock positions and account values for the configured trading currency.

    QuantPlatformKit's default IBKR helper is USD-oriented.  Keeping this small
    adapter local lets the platform run US and HK services without changing the
    shared package release line.
    """

    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    ib.reqPositions()
    if wait_seconds:
        import time as time_module

        time_module.sleep(wait_seconds)

    positions = []
    option_positions = []
    for raw_position in ib.positions():
        account_id = str(getattr(raw_position, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        if raw_position.position == 0:
            continue
        contract = raw_position.contract
        quantity = float(raw_position.position)
        average_cost = float(raw_position.avgCost)
        contract_currency = str(getattr(contract, "currency", "") or "").strip().upper()
        if not contract_currency:
            contract_currency = market_currency
        if str(getattr(contract, "secType", "") or "").strip().upper() == "OPT":
            option_positions.append(
                {
                    "underlier": str(getattr(contract, "symbol", "") or "").strip().upper(),
                    "local_symbol": str(getattr(contract, "localSymbol", "") or "").strip(),
                    "expiration": str(
                        getattr(contract, "lastTradeDateOrContractMonth", "") or ""
                    ).strip(),
                    "right": str(getattr(contract, "right", "") or "").strip().upper(),
                    "strike": float(getattr(contract, "strike", 0.0) or 0.0),
                    "quantity": quantity,
                    "average_cost": average_cost,
                    "cost_basis": abs(quantity * average_cost),
                    "account_id": account_id,
                    "currency": contract_currency,
                }
            )
            continue
        positions.append(
            Position(
                symbol=str(getattr(contract, "symbol", "") or "").strip().upper(),
                quantity=quantity,
                market_value=quantity * average_cost,
                average_cost=average_cost,
                currency=contract_currency,
            )
        )

    total_equity = 0.0
    available_funds = None
    matched_account_value_count = 0
    matched_market_currency_value_count = 0
    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]] = {}
    for account_value in ib.accountValues():
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        matched_account_value_count += 1
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if value_currency:
            tag_values = values_by_account_currency.setdefault((account_id, value_currency), {})
            numeric_value = _as_float(getattr(account_value, "value", None))
            if numeric_value is not None:
                tag_values[str(getattr(account_value, "tag", "") or "").strip()] = numeric_value
        if value_currency != market_currency:
            continue
        matched_market_currency_value_count += 1
        if account_value.tag == "NetLiquidation":
            total_equity += float(account_value.value)
        elif account_value.tag == "AvailableFunds":
            value = float(account_value.value)
            available_funds = value if available_funds is None else available_funds + value

    market_currency_cash = _cash_value_for_currency(
        values_by_account_currency,
        currency=market_currency,
    )
    if selected_account_ids and matched_account_value_count == 0:
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR returned no account values for the configured account selection."
        )
    if selected_account_ids and matched_market_currency_value_count == 0:
        raise IBKRPortfolioSnapshotUnavailableError(
            f"IBKR returned no {market_currency} account values for the configured account selection."
        )
    if cash_only_execution and not positions and market_currency_cash is None:
        raise IBKRPortfolioSnapshotUnavailableError(
            f"IBKR cash-only snapshot is missing the {market_currency} cash balance and positions."
        )
    if cash_only_execution:
        buying_power = float(market_currency_cash or 0.0) if market_currency_cash is not None else 0.0
        position_market_values = {
            str(position.symbol).strip().upper(): float(position.market_value)
            for position in positions
        }
        from us_equity_strategies.cash_only_equity import compute_strategy_total_equity

        total_equity = compute_strategy_total_equity(
            position_market_values,
            float(market_currency_cash or 0.0) if market_currency_cash is not None else 0.0,
        )
    else:
        buying_power = float(available_funds or 0.0) if available_funds is not None else (
            float(market_currency_cash or 0.0) if market_currency_cash is not None else 0.0
        )

    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=total_equity,
        buying_power=buying_power,
        positions=tuple(positions),
        metadata={
            "account_ids": selected_account_ids,
            "option_positions": tuple(option_positions),
            "currency": market_currency,
            "market_currency_cash": market_currency_cash,
            "available_funds": available_funds,
            "cash_only_execution": cash_only_execution,
            "cash_balances": tuple(
                {
                    "account_id": account_id,
                    "currency": currency,
                    **tag_values,
                }
                for (account_id, currency), tag_values in sorted(
                    values_by_account_currency.items(),
                    key=lambda item: ((item[0][0] or ""), item[0][1]),
                )
            ),
        },
    )
