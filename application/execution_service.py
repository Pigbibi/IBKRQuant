"""Order execution helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from application.paper_execution_admission import evaluate_ibkr_paper_execution_admission
try:
    from quant_platform_kit.common.cash_sweep import should_sell_cash_sweep_to_fund_whole_share_buy
except ImportError:  # pragma: no cover - compatibility with older pinned shared wheels
    def should_sell_cash_sweep_to_fund_whole_share_buy(
        max_quantity,
        cash_sweep_price,
        base_buying_power,
        funding_needs,
    ):
        if max_quantity <= 0:
            return False
        sweep_price = float(cash_sweep_price or 0.0)
        if sweep_price <= 0.0:
            return False
        current_buying_power = max(0.0, float(base_buying_power or 0.0))
        sweep_capacity = float(max_quantity) * sweep_price
        if sweep_capacity <= 0.0:
            return False

        for underweight_value, ask_price in funding_needs:
            _ = underweight_value
            quote_price = float(ask_price or 0.0)
            if quote_price <= 0.0:
                continue
            if current_buying_power >= quote_price:
                return False
            if current_buying_power + sweep_capacity >= quote_price:
                return True
        return False
try:
    from quant_platform_kit.common.small_account_compatibility import (
        apply_small_account_cash_compatibility,
        build_small_account_allocation_drift_notes,
        format_small_account_allocation_drift_notes,
        format_small_account_cash_substitution_notes,
    )
except ImportError:  # pragma: no cover - compatibility with older pinned shared wheels
    @dataclass(frozen=True)
    class _SmallAccountCashCompatibilityResult:
        targets: dict[str, float]
        whole_share_substituted_symbols: tuple[str, ...]
        safe_haven_cash_substituted_symbols: tuple[str, ...]
        cash_substitution_notes: tuple[dict[str, Any], ...]

    def _project_unbuyable_value_targets_to_cash(
        target_values,
        prices,
        *,
        candidate_symbols=None,
        quantity_step=1.0,
    ):
        adjusted = {
            str(symbol or "").strip().upper(): float(value or 0.0)
            for symbol, value in dict(target_values or {}).items()
        }
        step = max(0.0, float(quantity_step or 0.0))
        if step <= 0.0:
            return adjusted, ()
        normalized_candidates = (
            tuple(adjusted)
            if candidate_symbols is None
            else tuple(dict.fromkeys(str(symbol or "").strip().upper() for symbol in candidate_symbols))
        )
        normalized_prices = {
            str(symbol or "").strip().upper(): float(price or 0.0)
            for symbol, price in dict(prices or {}).items()
        }
        substituted = []
        for symbol in normalized_candidates:
            target_value = max(0.0, float(adjusted.get(symbol, 0.0) or 0.0))
            price = max(0.0, float(normalized_prices.get(symbol, 0.0) or 0.0))
            if price > 0.0 and 0.0 < target_value < (price * step):
                adjusted[symbol] = 0.0
                substituted.append(symbol)
        return adjusted, tuple(dict.fromkeys(substituted))

    def apply_small_account_cash_compatibility(
        target_values,
        prices,
        *,
        candidate_symbols=None,
        safe_haven_cash_symbols=(),
        quantity_step=1.0,
        cash_substitute_limit_usd=2000.0,
    ):
        adjusted_targets, substituted = _project_unbuyable_value_targets_to_cash(
            target_values,
            prices,
            candidate_symbols=candidate_symbols,
            quantity_step=quantity_step,
        )
        normalized_candidates = (
            tuple(adjusted_targets)
            if candidate_symbols is None
            else tuple(dict.fromkeys(str(symbol or "").strip().upper() for symbol in candidate_symbols))
        )
        remaining_non_safe_targets = [
            symbol
            for symbol in normalized_candidates
            if float(adjusted_targets.get(str(symbol or "").strip().upper(), 0.0) or 0.0) > 0.0
        ]
        safe_haven_symbols = tuple(
            dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in safe_haven_cash_symbols
                if str(symbol or "").strip()
            )
        )
        safe_haven_substituted = []
        if (
            substituted
            and not remaining_non_safe_targets
            and _positive_target_total(adjusted_targets) <= max(0.0, float(cash_substitute_limit_usd or 0.0))
        ):
            for symbol in safe_haven_symbols:
                if float(adjusted_targets.get(symbol, 0.0) or 0.0) > 0.0:
                    adjusted_targets[symbol] = 0.0
                    safe_haven_substituted.append(symbol)
        normalized_targets = {
            str(symbol or "").strip().upper(): float(value or 0.0)
            for symbol, value in dict(target_values or {}).items()
        }
        normalized_prices = {
            str(symbol or "").strip().upper(): float(price or 0.0)
            for symbol, price in dict(prices or {}).items()
        }
        notes = []
        if safe_haven_substituted:
            for symbol in substituted:
                target_value = max(0.0, float(normalized_targets.get(symbol, 0.0) or 0.0))
                price = max(0.0, float(normalized_prices.get(symbol, 0.0) or 0.0))
                if target_value <= 0.0 or price <= 0.0:
                    continue
                notes.append(
                    {
                        "symbol": symbol,
                        "target_value": target_value,
                        "price": price,
                        "cash_symbols": tuple(safe_haven_substituted),
                    }
                )
        return _SmallAccountCashCompatibilityResult(
            targets=adjusted_targets,
            whole_share_substituted_symbols=substituted,
            safe_haven_cash_substituted_symbols=tuple(safe_haven_substituted),
            cash_substitution_notes=tuple(notes),
        )

    def format_small_account_cash_substitution_notes(
        notes,
        *,
        translator,
        wrapper_key="buy_deferred",
        detail_key="buy_deferred_small_account_cash_substitution",
        cash_label_key="cash_label",
        symbol_suffix=".US",
    ):
        messages = []
        seen_keys = set()
        for note in tuple(notes or ()):
            if not isinstance(note, Mapping):
                continue
            symbol = str(note.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            target_value = max(0.0, float(note.get("target_value") or 0.0))
            price = max(0.0, float(note.get("price") or 0.0))
            if target_value <= 0.0 or price <= 0.0:
                continue
            cash_symbols = tuple(
                dict.fromkeys(
                    str(cash_symbol or "").strip().upper()
                    for cash_symbol in tuple(note.get("cash_symbols") or ())
                    if str(cash_symbol or "").strip()
                )
            )
            cash_symbols_text = ", ".join(f"{cash_symbol}{symbol_suffix}" for cash_symbol in cash_symbols)
            if not cash_symbols_text:
                cash_symbols_text = translator(cash_label_key)
            note_key = (symbol, f"{target_value:.2f}", cash_symbols_text)
            if note_key in seen_keys:
                continue
            seen_keys.add(note_key)
            detail = translator(
                detail_key,
                symbol=f"{symbol}{symbol_suffix}",
                diff=f"{target_value:.2f}",
                price=f"{price:.2f}",
                cash_symbols=cash_symbols_text,
            )
            messages.append(translator(wrapper_key, detail=detail))
        return tuple(messages)

    def build_small_account_allocation_drift_notes(**_kwargs):
        return ()

    def format_small_account_allocation_drift_notes(_notes, *, translator, **_kwargs):
        return ()
from quant_platform_kit.common.quantity import (
    floor_to_quantity_step,
    format_quantity,
    normalize_order_quantity,
)


def get_market_prices(
    ib,
    symbols,
    *,
    fetch_quote_snapshots,
    quote_recorder=None,
):
    """Fetch market prices for multiple symbols in one pass."""
    quotes = fetch_quote_snapshots(ib, symbols)
    if quote_recorder is not None:
        for symbol, quote in quotes.items():
            quote_recorder(symbol, quote)
    return {symbol: quote.last_price for symbol, quote in quotes.items()}


def _serialize_quote_snapshot(snapshot, *, symbol: str | None = None) -> dict:
    return {
        "symbol": str(getattr(snapshot, "symbol", "") or symbol or "").strip().upper(),
        "as_of": str(getattr(snapshot, "as_of", "") or ""),
        "last_price": float(getattr(snapshot, "last_price", 0.0) or 0.0),
        "bid_price": getattr(snapshot, "bid_price", None),
        "ask_price": getattr(snapshot, "ask_price", None),
        "currency": str(getattr(snapshot, "currency", "") or "").strip(),
    }


def check_order_submitted(report, *, translator):
    """Check if order was accepted. DAY orders auto-expire at close if not filled."""
    order_id = report.broker_order_id
    status = report.status

    if status == "Filled":
        return (
            True,
            translator(
                "order_filled",
                symbol=report.symbol,
                side=report.side,
                qty=format_quantity(report.filled_quantity or report.quantity),
                price=f"{float(report.average_fill_price or 0.0):.2f}",
                order_id=order_id,
            ),
        )
    if status in {"PartiallyFilled", "Partial"}:
        return (
            True,
            translator(
                "order_partial",
                symbol=report.symbol,
                side=report.side,
                executed=format_quantity(report.filled_quantity or 0),
                qty=format_quantity(report.quantity or 0),
                price=f"{float(report.average_fill_price or 0.0):.2f}",
                order_id=order_id,
            ),
        )
    if status in {"PendingSubmit", "ApiPending", "ApiPendingSubmit", "Submitted", "PreSubmitted"}:
        return True, f"✅ {translator('submitted', order_id=order_id, status=status)}"
    return False, f"❌ {translator('failed', reason=status)}"


_FILLED_ORDER_STATUSES = frozenset({"Filled"})
_PARTIALLY_FILLED_ORDER_STATUSES = frozenset({"PartiallyFilled", "Partial"})
_PENDING_ORDER_STATUSES = frozenset(
    {"PendingSubmit", "ApiPending", "ApiPendingSubmit", "Submitted", "PreSubmitted"}
)


def _record_order_outcome(
    execution_summary: dict,
    order_payload: dict,
    *,
    status: object,
    option_order: bool = False,
) -> str:
    """Record the broker state without treating a non-terminal state as an execution.

    IBKR can acknowledge an order and reject or cancel it later. A report captured
    while the order is still pending is useful for reconciliation, but it must not
    make the rebalance appear complete.
    """
    normalized_status = str(status or "").strip()
    prefix = "option_orders" if option_order else "orders"
    if normalized_status in _FILLED_ORDER_STATUSES:
        execution_summary[f"{prefix}_filled"].append(order_payload)
        return "filled"
    if normalized_status in _PARTIALLY_FILLED_ORDER_STATUSES:
        execution_summary[f"{prefix}_partially_filled"].append(order_payload)
        return "partially_filled"
    if normalized_status in _PENDING_ORDER_STATUSES:
        execution_summary[f"{prefix}_pending"].append(order_payload)
        return "pending"
    execution_summary[f"{prefix}_skipped"].append(
        {**order_payload, "reason": normalized_status or "submit_failed"}
    )
    failure_prefix = "option_submit_failed" if option_order else "submit_failed"
    execution_summary["skipped_reasons"].append(
        f"{failure_prefix}:{order_payload.get('symbol')}:{normalized_status or 'unknown'}"
    )
    return "failed"


def _normalize_account_ids(account_ids=None) -> tuple[str, ...]:
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


def _resolve_order_account_id(account_ids=None) -> str | None:
    normalized = _normalize_account_ids(account_ids)
    if len(normalized) > 1:
        raise ValueError(
            "IBKR live order routing requires a single account_id per runtime service; "
            f"got {len(normalized)} account_ids."
        )
    return normalized[0] if normalized else None


MARKET_CURRENCY_CASH_TAG_PRIORITY = (
    "$LEDGER-CashBalance",
    "$LEDGER-TotalCashBalance",
    "CashBalance",
    "TotalCashBalance",
    "SettledCash",
)


def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cash_value_for_currency(account_values, *, currency: str, account_ids=None) -> float | None:
    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]] = {}
    for account_value in account_values or ():
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if not value_currency:
            continue
        value = _coerce_float(getattr(account_value, "value", None))
        if value is None:
            continue
        tag = str(getattr(account_value, "tag", "") or "").strip()
        values_by_account_currency.setdefault((account_id, value_currency), {})[tag] = value

    total = 0.0
    matched = False
    for (_account_id, value_currency), tag_values in values_by_account_currency.items():
        if value_currency != market_currency:
            continue
        for tag in MARKET_CURRENCY_CASH_TAG_PRIORITY:
            if tag in tag_values:
                total += float(tag_values[tag])
                matched = True
                break
    return total if matched else None


def _available_funds_for_currency(account_values, *, currency: str, account_ids=None) -> float | None:
    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    total = 0.0
    matched = False
    for account_value in account_values or ():
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if value_currency != market_currency:
            continue
        if str(getattr(account_value, "tag", "") or "").strip() == "AvailableFunds":
            value = _coerce_float(getattr(account_value, "value", None))
            if value is not None:
                total += float(value)
                matched = True
    return total if matched else None


def get_available_buying_power(
    ib,
    fallback_buying_power,
    *,
    account_ids=None,
    currency="USD",
    cash_only_execution=True,
):
    selected_account_ids = _normalize_account_ids(account_ids)
    account_values = list(ib.accountValues() or ())
    if not cash_only_execution:
        available_funds = _available_funds_for_currency(
            account_values,
            currency=currency,
            account_ids=selected_account_ids,
        )
        if available_funds is not None:
            return max(0.0, float(available_funds))
        return max(0.0, float(fallback_buying_power or 0.0))
    currency_cash = _cash_value_for_currency(
        account_values,
        currency=currency,
        account_ids=selected_account_ids,
    )
    if currency_cash is not None:
        return max(0.0, float(currency_cash))
    return max(0.0, float(fallback_buying_power or 0.0))


def _iter_open_orders(ib) -> list[Any]:
    open_trades = getattr(ib, "openTrades", None)
    if callable(open_trades):
        return list(open_trades() or [])
    open_orders = getattr(ib, "openOrders", None)
    if callable(open_orders):
        return list(open_orders() or [])
    return []


def _extract_open_order_symbol(order_like: Any) -> str | None:
    contract = getattr(order_like, "contract", None)
    if contract is None and hasattr(order_like, "order"):
        contract = getattr(order_like.order, "contract", None)
    symbol = getattr(contract, "symbol", None)
    if symbol is None and hasattr(order_like, "symbol"):
        symbol = getattr(order_like, "symbol")
    symbol_text = str(symbol or "").strip().upper()
    return symbol_text or None


def _extract_open_order_status(order_like: Any) -> str:
    order_status = getattr(order_like, "orderStatus", None)
    status = getattr(order_status, "status", None)
    if status is None:
        status = getattr(order_like, "status", None)
    return str(status or "").strip()


def _extract_open_order_account(order_like: Any) -> str | None:
    order = getattr(order_like, "order", None)
    for candidate in (
        getattr(order, "account", None),
        getattr(order_like, "account", None),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def _collect_pending_symbols(ib, symbols: set[str], *, account_ids=None) -> tuple[str, ...]:
    selected_account_ids = _normalize_account_ids(account_ids)
    pending = []
    for order_like in _iter_open_orders(ib):
        if not _matches_account(_extract_open_order_account(order_like), selected_account_ids):
            continue
        status = _extract_open_order_status(order_like)
        if status in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
            continue
        symbol = _extract_open_order_symbol(order_like)
        if symbol and symbol in symbols:
            pending.append(symbol)
    return tuple(sorted(dict.fromkeys(pending)))


def _iter_fills(ib) -> list[Any]:
    fills = getattr(ib, "fills", None)
    if callable(fills):
        return list(fills() or [])
    return []


def _extract_fill_symbol(fill_like: Any) -> str | None:
    contract = getattr(fill_like, "contract", None)
    symbol = getattr(contract, "symbol", None)
    symbol_text = str(symbol or "").strip().upper()
    return symbol_text or None


def _extract_fill_account(fill_like: Any) -> str | None:
    execution = getattr(fill_like, "execution", None)
    for candidate in (
        getattr(execution, "acctNumber", None),
        getattr(execution, "account", None),
        getattr(fill_like, "account", None),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def _normalize_date_like(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    ts = pd.Timestamp(value)
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    else:
        ts = ts.tz_localize(None)
    return ts.normalize().date().isoformat()


def _extract_fill_date(fill_like: Any) -> str | None:
    execution = getattr(fill_like, "execution", None)
    for candidate in (
        getattr(execution, "time", None),
        getattr(fill_like, "time", None),
    ):
        normalized = _normalize_date_like(candidate)
        if normalized is not None:
            return normalized
    return None


def _collect_same_day_filled_symbols(
    ib,
    symbols: set[str],
    trade_date: str | None,
    *,
    account_ids=None,
) -> tuple[str, ...]:
    if not trade_date:
        return ()
    selected_account_ids = _normalize_account_ids(account_ids)
    matched = []
    for fill_like in _iter_fills(ib):
        if not _matches_account(_extract_fill_account(fill_like), selected_account_ids):
            continue
        symbol = _extract_fill_symbol(fill_like)
        if not symbol or symbol not in symbols:
            continue
        fill_date = _extract_fill_date(fill_like)
        if fill_date == trade_date:
            matched.append(symbol)
    return tuple(sorted(dict.fromkeys(matched)))


def _round_weight(value: float) -> float:
    return round(float(value or 0.0), 8)


def _build_target_hash(target_weights: dict[str, float]) -> str:
    payload = [[str(symbol), _round_weight(weight)] for symbol, weight in sorted(target_weights.items())]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _sanitize_token(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "none"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    return safe or "none"


def _display_text(value: Any, *, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _resolve_weight_allocation(signal_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(signal_metadata or {})
    allocation = dict(metadata.get("allocation") or {})
    if not allocation:
        raise ValueError("IBKR execution requires signal_metadata.allocation")
    if allocation.get("target_mode") != "weight":
        raise ValueError("IBKR execution requires allocation.target_mode=weight")
    return {
        "strategy_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("strategy_symbols", ())),
        "risk_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("risk_symbols", ())),
        "income_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("income_symbols", ())),
        "safe_haven_symbols": tuple(
            str(symbol).strip().upper() for symbol in allocation.get("safe_haven_symbols", ())
        ),
        "targets": {
            str(symbol).strip().upper(): float(weight)
            for symbol, weight in dict(allocation.get("targets") or {}).items()
        },
    }


def _normalize_option_order_intents(signal_metadata: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    metadata = dict(signal_metadata or {})
    raw_payload = metadata.get("option_order_intents")
    if isinstance(raw_payload, dict):
        raw_intents = raw_payload.get("intents")
    else:
        raw_intents = raw_payload
    intents = []
    for intent in raw_intents or ():
        if isinstance(intent, dict):
            intents.append(dict(intent))
    return tuple(intents)


def _option_intent_underliers(option_intents: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    underliers = []
    for intent in option_intents:
        underlier = str(intent.get("underlier") or intent.get("symbol") or "").strip().upper()
        if underlier:
            underliers.append(underlier)
    return tuple(sorted(dict.fromkeys(underliers)))


def _is_executable_option_intent(intent: dict[str, Any]) -> bool:
    intent_type = str(intent.get("intent_type") or "").strip()
    action = str(intent.get("action") or "").strip().lower()
    if (
        str(intent.get("asset_class") or "").strip().lower() == "option"
        and intent_type == "single_leg_option"
        and action in {"buy_to_open", "sell_to_close"}
    ):
        return True
    return (
        str(intent.get("asset_class") or "").strip().lower() == "option"
        and intent_type == "multi_leg_option"
        and action == "sell_to_open_put_credit_spread"
    )


def _has_executable_option_plan(option_intents: tuple[dict[str, Any], ...]) -> bool:
    return any(_is_executable_option_intent(intent) for intent in option_intents)


def _format_option_intent_symbol(intent: dict[str, Any]) -> str:
    underlier = str(intent.get("underlier") or intent.get("symbol") or "").strip().upper()
    if str(intent.get("intent_type") or "").strip() == "multi_leg_option":
        expiration = str(intent.get("expiration") or "").strip()
        return f"{underlier} {expiration} PCS".strip()
    right = str(intent.get("right") or "").strip().upper()
    expiration = str(intent.get("expiration") or "").strip()
    strike = intent.get("strike")
    if underlier and right and expiration and strike not in {None, ""}:
        return f"{underlier} {expiration} {float(strike):g}{right}"
    return underlier or "<option>"


def _record_unsupported_option_intents(
    execution_summary: dict[str, Any],
    option_intents: tuple[dict[str, Any], ...],
) -> None:
    for intent in option_intents:
        if _is_executable_option_intent(intent):
            continue
        symbol = _format_option_intent_symbol(intent)
        reason = "unsupported_option_intent_type"
        execution_summary["option_orders_skipped"].append(
            {
                "symbol": symbol,
                "action": str(intent.get("action") or ""),
                "intent_type": str(intent.get("intent_type") or ""),
                "reason": reason,
            }
        )
        execution_summary["skipped_reasons"].append(f"{reason}:{symbol}")


def _build_single_leg_option_order_intent(
    order_intent_cls,
    intent: dict[str, Any],
    *,
    account_id: str | None,
):
    action = str(intent.get("action") or "").strip().lower()
    side = "buy" if action.startswith("buy") else "sell"
    return order_intent_cls(
        symbol=str(intent.get("underlier") or intent.get("symbol") or "").strip().upper(),
        side=side,
        quantity=float(intent.get("quantity") or 0.0),
        order_type=str(intent.get("order_type") or "limit").strip().lower(),
        limit_price=(
            float(intent["limit_price"])
            if intent.get("limit_price") not in {None, ""}
            else None
        ),
        time_in_force=str(intent.get("time_in_force") or "DAY").strip().upper(),
        account_id=account_id,
        metadata={
            **intent,
            "asset_class": "option",
            "intent_type": "single_leg_option",
            "security_type": "OPT",
        },
    )


def _build_multi_leg_option_order_intent(
    order_intent_cls,
    intent: dict[str, Any],
    *,
    account_id: str | None,
):
    return order_intent_cls(
        symbol=str(intent.get("underlier") or intent.get("symbol") or "").strip().upper(),
        side="sell",
        quantity=float(intent.get("quantity") or 0.0),
        order_type=str(intent.get("order_type") or "limit").strip().lower(),
        limit_price=(
            float(intent["limit_price"])
            if intent.get("limit_price") not in {None, ""}
            else None
        ),
        time_in_force=str(intent.get("time_in_force") or "DAY").strip().upper(),
        account_id=account_id,
        metadata={
            **intent,
            "asset_class": "option",
            "intent_type": "multi_leg_option",
            "security_type": "BAG",
        },
    )


def _execute_option_order_intents(
    ib,
    option_intents: tuple[dict[str, Any], ...],
    *,
    submit_order_intent,
    order_intent_cls,
    translator,
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    dry_run_only: bool,
    order_account_id: str | None,
    buying_power: float,
) -> float:
    if not option_intents:
        return buying_power
    _record_unsupported_option_intents(execution_summary, option_intents)
    for intent in option_intents:
        if not _is_executable_option_intent(intent):
            continue
        symbol = _format_option_intent_symbol(intent)
        quantity = float(intent.get("quantity") or 0.0)
        if quantity <= 0.0:
            execution_summary["option_orders_skipped"].append(
                {"symbol": symbol, "action": intent.get("action"), "reason": "quantity_zero"}
            )
            continue
        limit_price = float(intent.get("limit_price") or 0.0)
        multiplier = float(intent.get("contract_multiplier") or 100.0)
        action = str(intent.get("action") or "").strip().lower()
        intent_type = str(intent.get("intent_type") or "").strip()
        estimated_notional = float(intent.get("max_notional_usd") or (quantity * limit_price * multiplier))
        max_loss = float(intent.get("max_loss_usd") or 0.0)
        if action.startswith("buy") and estimated_notional > max(0.0, buying_power * 0.95):
            execution_summary["option_orders_skipped"].append(
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "reason": "insufficient_buying_power",
                }
            )
            execution_summary["skipped_reasons"].append(f"option_insufficient_buying_power:{symbol}")
            continue
        if intent_type == "multi_leg_option" and max_loss > max(0.0, buying_power * 0.95):
            execution_summary["option_orders_skipped"].append(
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "reason": "insufficient_buying_power_for_defined_risk",
                }
            )
            execution_summary["skipped_reasons"].append(f"option_insufficient_buying_power:{symbol}")
            continue
        payload = {
            "symbol": symbol,
            "underlier": str(intent.get("underlier") or "").strip().upper(),
            "action": action,
            "quantity": quantity,
            "limit_price": limit_price,
            "status": "dry_run" if dry_run_only else "pending",
        }
        if dry_run_only:
            execution_summary["option_orders_submitted"].append(payload)
            trade_logs.append(f"DRY_RUN option {action} {symbol} {format_quantity(quantity)} @{limit_price:.2f}")
            if action.startswith("buy"):
                buying_power -= estimated_notional
            elif intent_type == "multi_leg_option":
                buying_power -= max_loss
            continue
        if intent_type == "multi_leg_option":
            order_intent = _build_multi_leg_option_order_intent(
                order_intent_cls,
                intent,
                account_id=order_account_id,
            )
        else:
            order_intent = _build_single_leg_option_order_intent(
                order_intent_cls,
                intent,
                account_id=order_account_id,
            )
        report = submit_order_intent(ib, order_intent)
        _, status_msg = check_order_submitted(report, translator=translator)
        status = str(getattr(report, "status", "") or "")
        order_payload = {
            **payload,
            "status": status,
            "broker_order_id": getattr(report, "broker_order_id", None),
        }
        outcome = _record_order_outcome(
            execution_summary,
            order_payload,
            status=status,
            option_order=True,
        )
        trade_logs.append(f"option {action} {symbol} {format_quantity(quantity)} @{limit_price:.2f} {status_msg}")
        if outcome != "failed" and action.startswith("buy"):
            buying_power -= estimated_notional
        elif outcome != "failed" and intent_type == "multi_leg_option":
            buying_power -= max_loss
    return buying_power


def _apply_snapshot_price_fallbacks(
    prices: dict[str, float],
    symbols,
    *,
    dry_run_only: bool,
    snapshot_price_fallbacks: dict[str, float] | None,
) -> tuple[dict[str, float], tuple[str, ...]]:
    del dry_run_only
    if not snapshot_price_fallbacks:
        return dict(prices), ()
    resolved = dict(prices)
    fallback_symbols: list[str] = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        try:
            existing_price = float(resolved.get(normalized, 0.0) or 0.0)
        except (TypeError, ValueError):
            existing_price = 0.0
        if existing_price > 0.0:
            resolved[normalized] = existing_price
            continue
        fallback_price = snapshot_price_fallbacks.get(normalized)
        if fallback_price and float(fallback_price) > 0:
            resolved[normalized] = float(fallback_price)
            fallback_symbols.append(normalized)
    return resolved, tuple(fallback_symbols)


def _normalize_price_fallbacks(signal_metadata: dict[str, Any] | None) -> dict[str, float]:
    metadata = dict(signal_metadata or {})
    raw_fallbacks = {}
    for key in ("dry_run_price_fallbacks", "price_fallbacks"):
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            raw_fallbacks.update(candidate)
    normalized: dict[str, float] = {}
    for symbol, price in raw_fallbacks.items():
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            continue
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            continue
        if numeric_price > 0.0:
            normalized[normalized_symbol] = numeric_price
    return normalized


def _format_symbol_preview(symbols: tuple[str, ...], *, limit: int = 3) -> str:
    if not symbols:
        return ""
    shown = [str(symbol).strip().upper() for symbol in symbols[:limit]]
    remaining = len(symbols) - len(shown)
    if remaining > 0:
        shown.append(f"+{remaining}")
    return ",".join(shown)


def _resolve_execution_mode(*, dry_run_only: bool, execution_mode: str | None = None) -> str:
    if dry_run_only:
        return "dry_run"
    normalized = str(execution_mode or "paper").strip().lower().replace("-", "_")
    aliases = {
        "dryrun": "dry_run",
        "dry_run_only": "dry_run",
        "paper_trading": "paper",
        "live_trading": "live",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized == "dry_run":
        raise ValueError("execution_mode=dry_run requires dry_run_only=True")
    if normalized not in {"paper", "live"}:
        raise ValueError("execution_mode must be paper or live")
    return normalized


def _resolve_execution_lock_path(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    trade_date: str | None,
    snapshot_date: str | None,
    execution_mode: str,
    execution_lock_dir: str | Path | None,
) -> Path:
    lock_dir = Path(execution_lock_dir) if execution_lock_dir else Path(tempfile.gettempdir()) / "ibkr_execution_locks"
    scope = "__".join(
        [
            _sanitize_token(account_group or "default"),
            _sanitize_token(service_name or "service"),
            _sanitize_token(strategy_profile or "unknown"),
            _sanitize_token(execution_mode),
            _sanitize_token(trade_date),
            _sanitize_token(snapshot_date or "no_snapshot"),
        ]
    )
    return lock_dir / f"{scope}.json"


def _read_execution_lock(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _try_create_execution_lock(lock_path: Path, payload: dict[str, Any]) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return True
    except FileExistsError:
        return False


def _build_execution_lock_payload(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    account_ids: tuple[str, ...] | list[str] | None,
    trade_date: str | None,
    snapshot_date: str | None,
    target_hash: str,
    execution_mode: str,
) -> dict[str, Any]:
    return {
        "strategy_profile": strategy_profile,
        "account_group": account_group,
        "service_name": service_name,
        "account_ids": list(account_ids or ()),
        "trade_date": trade_date,
        "snapshot_date": snapshot_date,
        "mode": execution_mode,
        "target_hash": target_hash,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    }


def _format_target_lines(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
    *,
    translator,
) -> list[str]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    target_lines = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        delta = target_weight - current_weight.get(symbol, 0.0)
        target_lines.append(
            translator(
                "target_diff",
                symbol=symbol,
                current=f"{current_weight.get(symbol, 0.0):.1%}",
                target=f"{target_weight:.1%}",
                delta=f"{delta:.1%}",
            )
        )
    return target_lines


def _build_target_diff_rows(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
) -> list[dict[str, float | str]]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    rows = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        current_value = current_weight.get(symbol, 0.0)
        rows.append(
            {
                "symbol": symbol,
                "current_weight": current_value,
                "target_weight": float(target_weight),
                "delta_weight": float(target_weight - current_value),
            }
        )
    return rows


def _floor_order_quantity(quantity, *, quantity_step):
    return normalize_order_quantity(floor_to_quantity_step(quantity, quantity_step))


def _investable_buying_power(buying_power: float, reserved_cash: float) -> float:
    return max(0.0, float(buying_power or 0.0) - max(0.0, float(reserved_cash or 0.0)))


def _estimated_buy_order_cost(
    *,
    buy_symbols,
    current_mv,
    target_mv,
    threshold,
    investable_buying_power,
    prices,
    limit_buy_premium,
    limit_buy_premium_by_symbol,
    quantity_step,
    minimum_order_notional,
) -> float:
    total_cost = 0.0
    for symbol in buy_symbols:
        current = float(current_mv.get(symbol, 0.0) or 0.0)
        target = float(target_mv.get(symbol, 0.0) or 0.0)
        if current >= target - threshold:
            continue
        buy_value = min(target - current, investable_buying_power)
        price = prices.get(symbol)
        if not price or buy_value < minimum_order_notional:
            continue
        limit_price = _limit_buy_price(symbol, price, limit_buy_premium, limit_buy_premium_by_symbol)
        qty = _floor_order_quantity(
            buy_value / limit_price,
            quantity_step=quantity_step,
        )
        if qty <= 0:
            continue
        total_cost += float(qty) * float(limit_price)
    return total_cost


def _rotation_guard_should_block_buys(
    *,
    pending_sell_release_symbols,
    buy_needed_symbols,
    current_mv,
    target_mv,
    threshold,
    investable_buying_power,
    prices,
    limit_buy_premium,
    limit_buy_premium_by_symbol,
    quantity_step,
    minimum_order_notional,
) -> bool:
    if not pending_sell_release_symbols or not buy_needed_symbols:
        return False
    estimated_cost = _estimated_buy_order_cost(
        buy_symbols=buy_needed_symbols,
        current_mv=current_mv,
        target_mv=target_mv,
        threshold=threshold,
        investable_buying_power=investable_buying_power,
        prices=prices,
        limit_buy_premium=limit_buy_premium,
        limit_buy_premium_by_symbol=limit_buy_premium_by_symbol,
        quantity_step=quantity_step,
        minimum_order_notional=minimum_order_notional,
    )
    return estimated_cost > investable_buying_power


def _sell_order_quantity(
    *,
    current_value,
    target_value,
    price,
    position_quantity,
    quantity_step,
):
    held_quantity = max(0.0, float(position_quantity or 0.0))
    if held_quantity <= 0.0:
        return 0

    target = max(0.0, float(target_value or 0.0))
    if target <= 0.0:
        return _floor_order_quantity(held_quantity, quantity_step=quantity_step)

    sell_value = max(0.0, float(current_value or 0.0) - target)
    if sell_value <= 0.0 or float(price or 0.0) <= 0.0:
        return 0
    return _floor_order_quantity(
        min(sell_value / float(price), held_quantity),
        quantity_step=quantity_step,
    )


def _limit_buy_premium_for_symbol(symbol, default_premium, premium_by_symbol=None) -> float:
    normalized_symbol = str(symbol or "").strip().upper()
    try:
        fallback = float(default_premium)
    except (TypeError, ValueError):
        fallback = 1.005
    if not isinstance(premium_by_symbol, dict):
        return fallback
    raw_value = premium_by_symbol.get(normalized_symbol)
    if raw_value is None:
        return fallback
    try:
        premium = float(raw_value)
    except (TypeError, ValueError):
        return fallback
    return premium if premium > 0.0 else fallback


def _limit_buy_price(symbol, price, default_premium, premium_by_symbol=None) -> float:
    return round(
        float(price) * _limit_buy_premium_for_symbol(symbol, default_premium, premium_by_symbol),
        2,
    )


DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD = 1000.0
SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD = 2000.0
SMALL_ACCOUNT_EXISTING_WHOLE_SHARE_RETENTION_SYMBOLS = frozenset({"TQQQ", "SOXL"})
_SMALL_ACCOUNT_RETENTION_MIN_TARGET_SHARE_RATIO_DEFAULT = 0.85
SMALL_ACCOUNT_EXISTING_WHOLE_SHARE_RETENTION_MIN_TARGET_SHARE_RATIO_BY_SYMBOL = {
    "SOXX": 0.90,
}
SMALL_ACCOUNT_WHOLE_SHARE_BOOTSTRAP_MIN_TARGET_SHARE_RATIO_BY_SYMBOL = {
    "TQQQ": 0.90,
    "SOXL": 0.90,
    "SOXX": 0.90,
}


def _positive_target_total(targets: dict[str, Any]) -> float:
    total = 0.0
    for value in dict(targets or {}).values():
        try:
            total += max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _apply_safe_haven_cash_substitution_to_weights(
    target_weights: dict[str, float],
    *,
    safe_haven_symbols: tuple[str, ...],
    investable: float,
    threshold_usd: float,
) -> tuple[dict[str, float], tuple[str, ...]]:
    threshold = max(0.0, float(threshold_usd or 0.0))
    adjusted = {
        str(symbol).strip().upper(): float(weight or 0.0)
        for symbol, weight in dict(target_weights or {}).items()
    }
    if threshold <= 0.0:
        return adjusted, ()

    substituted: list[str] = []
    for symbol in safe_haven_symbols:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            continue
        target_notional = max(0.0, float(investable or 0.0) * float(adjusted.get(normalized, 0.0) or 0.0))
        if 0.0 < target_notional < threshold:
            adjusted[normalized] = 0.0
            substituted.append(normalized)
    return adjusted, tuple(dict.fromkeys(substituted))


def _should_retain_existing_whole_share(symbol, *, target_value, price, quantity=0.0) -> bool:
    """Decide whether an existing whole-share position should be retained.

    Universal rule: if the account already holds this symbol (>0 shares) and the
    strategy wants to keep a meaningful fraction of a share (target >= 85% of 1-share
    price), retain the position.  This prevents the sell-then-fail-to-rebuy cycle for
    small accounts where target < 1 share but still close to it.

    Genuine reductions (target << 1 share) are NOT blocked — the sell proceeds.
    The hardcoded lists act as overrides for symbols that need a different threshold.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    held = float(quantity or 0.0)
    target = float(target_value or 0.0)
    quote_price = max(0.0, float(price or 0.0))

    # Universal: held + positive target + target close to 1-share price → retain
    if held > 0.0 and target > 0.0 and quote_price > 0.0:
        if target >= quote_price * _SMALL_ACCOUNT_RETENTION_MIN_TARGET_SHARE_RATIO_DEFAULT:
            return True

    # Legacy whitelist — unconditional retention (safety net)
    if normalized_symbol in SMALL_ACCOUNT_EXISTING_WHOLE_SHARE_RETENTION_SYMBOLS:
        return True

    # Legacy per-symbol ratio-based retention (override / tighter threshold)
    min_target_share_ratio = (
        SMALL_ACCOUNT_EXISTING_WHOLE_SHARE_RETENTION_MIN_TARGET_SHARE_RATIO_BY_SYMBOL.get(normalized_symbol)
    )
    if min_target_share_ratio is None:
        return False
    if quote_price <= 0.0:
        return False
    return target >= quote_price * float(min_target_share_ratio)


def _should_bootstrap_whole_share_buy(symbol, *, target_value, limit_price) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    min_target_share_ratio = (
        SMALL_ACCOUNT_WHOLE_SHARE_BOOTSTRAP_MIN_TARGET_SHARE_RATIO_BY_SYMBOL.get(normalized_symbol)
    )
    if min_target_share_ratio is None:
        return False
    effective_limit_price = max(0.0, float(limit_price or 0.0))
    if effective_limit_price <= 0.0:
        return False
    return max(0.0, float(target_value or 0.0)) >= effective_limit_price * float(min_target_share_ratio)


def _should_top_up_existing_whole_share_buy(symbol, *, target_gap_value, limit_price, quantity=0.0) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol not in SMALL_ACCOUNT_WHOLE_SHARE_BOOTSTRAP_MIN_TARGET_SHARE_RATIO_BY_SYMBOL:
        return False
    if max(0.0, float(quantity or 0.0)) <= 0.0:
        return False
    effective_limit_price = max(0.0, float(limit_price or 0.0))
    if effective_limit_price <= 0.0:
        return False
    return max(0.0, float(target_gap_value or 0.0)) >= (
        effective_limit_price * float(_SMALL_ACCOUNT_RETENTION_MIN_TARGET_SHARE_RATIO_DEFAULT)
    )


def _planned_buy_order_quantity(
    symbol,
    *,
    buy_value,
    limit_price,
    quantity_step,
    investable_buying_power,
    held_quantity=0.0,
) -> tuple[float, bool]:
    effective_limit_price = max(0.0, float(limit_price or 0.0))
    qty = (
        _floor_order_quantity(float(buy_value or 0.0) / effective_limit_price, quantity_step=quantity_step)
        if effective_limit_price > 0.0
        else 0.0
    )
    forced_whole_share = False
    if (
        qty <= 0
        and effective_limit_price > 0.0
        and float(investable_buying_power or 0.0) >= effective_limit_price
        and (
            _should_top_up_existing_whole_share_buy(
                symbol,
                target_gap_value=buy_value,
                limit_price=effective_limit_price,
                quantity=held_quantity,
            )
            or _should_bootstrap_whole_share_buy(
                symbol,
                target_value=buy_value,
                limit_price=effective_limit_price,
            )
        )
    ):
        qty = _floor_order_quantity(1.0, quantity_step=quantity_step)
        forced_whole_share = qty > 0
    return qty, forced_whole_share


def _projected_sell_release_value_for_report(report, *, fallback_price=0.0, fallback_quantity=0.0) -> float:
    status = str(getattr(report, "status", "") or "")
    if status not in {"Filled", "PartiallyFilled", "Partial"}:
        return 0.0
    filled_quantity = float(getattr(report, "filled_quantity", 0.0) or 0.0)
    if status == "Filled" and filled_quantity <= 0.0:
        filled_quantity = float(getattr(report, "quantity", 0.0) or fallback_quantity or 0.0)
    if filled_quantity <= 0.0:
        return 0.0
    fill_price = float(getattr(report, "average_fill_price", 0.0) or 0.0)
    if fill_price <= 0.0:
        fill_price = max(0.0, float(fallback_price or 0.0))
    if fill_price <= 0.0:
        return 0.0
    return filled_quantity * fill_price


def _format_symbol_with_suffix(symbol, *, suffix=".US") -> str:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return normalized
    if "." in normalized:
        return normalized
    normalized_suffix = str(suffix or "").strip().upper()
    return f"{normalized}{normalized_suffix}" if normalized_suffix else normalized


def _format_small_account_whole_share_bootstrap_notes(
    symbols,
    *,
    translator,
    symbol_suffix=".US",
) -> tuple[str, ...]:
    normalized_symbols = tuple(
        dict.fromkeys(
            _format_symbol_with_suffix(symbol, suffix=symbol_suffix)
            for symbol in tuple(symbols or ())
            if str(symbol or "").strip()
        )
    )
    if not normalized_symbols:
        return ()
    try:
        message = translator(
            "buy_lifted_small_account_whole_share",
            symbols=", ".join(normalized_symbols),
        )
    except Exception:
        message = ""
    if not message or message == "buy_lifted_small_account_whole_share":
        message = (
            f"ℹ️ [买入说明] {', '.join(normalized_symbols)} 目标金额接近 1 股；"
            "小账户整数股兼容，本轮允许按 1 股下单"
        )
    return (message,)


def _finalize_result(trade_logs, execution_summary, *, return_summary: bool):
    if return_summary:
        return trade_logs, execution_summary
    return trade_logs


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    fetch_quote_snapshots,
    submit_order_intent,
    order_intent_cls,
    translator,
    strategy_symbols=None,
    signal_metadata=None,
    acquire_execution_claim=None,
    strategy_profile=None,
    account_group=None,
    service_name=None,
    account_ids=None,
    dry_run_only=False,
    execution_mode=None,
    cash_reserve_ratio,
    rebalance_threshold_ratio,
    limit_buy_premium,
    sell_settle_delay_sec,
    cash_reserve_floor_usd=0.0,
    quantity_step=1.0,
    min_order_notional=50.0,
    safe_haven_cash_substitute_threshold_usd=DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD,
    market_currency="USD",
    limit_buy_premium_by_symbol=None,
    execution_lock_dir=None,
    return_summary=False,
    cash_only_execution=True,
    paper_execution_admission_enabled=False,
    runtime_release_receipt=None,
    expected_strategy_release=None,
):
    """Execute trades to reach target weights."""
    del target_weights
    if not dry_run_only:
        delegate_submit = submit_order_intent

        def submit_claimed_order(ib, order_intent):
            if acquire_execution_claim is None or not acquire_execution_claim():
                raise RuntimeError("IBKR execution claim required; refusing broker submission")
            return delegate_submit(ib, order_intent)

        submit_order_intent = submit_claimed_order
    signal_metadata = signal_metadata or {}
    allocation = _resolve_weight_allocation(signal_metadata)
    target_weights = dict(allocation["targets"])
    option_order_intents = _normalize_option_order_intents(signal_metadata)
    option_underliers = _option_intent_underliers(option_order_intents)
    has_executable_option_plan = _has_executable_option_plan(option_order_intents)
    strategy_symbols = tuple(allocation["strategy_symbols"])
    trade_date = str(signal_metadata.get("trade_date") or "").strip() or None
    snapshot_date = _normalize_date_like(signal_metadata.get("snapshot_as_of"))
    resolved_execution_mode = _resolve_execution_mode(
        dry_run_only=dry_run_only,
        execution_mode=execution_mode,
    )
    safe_haven_symbols = tuple(allocation["safe_haven_symbols"])
    safe_haven_symbol = safe_haven_symbols[0] if safe_haven_symbols else None
    equity = account_values.get("equity", 0)
    normalized_account_ids = _normalize_account_ids(account_ids)
    order_account_id = _resolve_order_account_id(normalized_account_ids)
    quote_snapshots_by_symbol: dict[str, dict] = {}

    def record_quote_snapshot(symbol, snapshot) -> None:
        payload = _serialize_quote_snapshot(snapshot, symbol=symbol)
        symbol = payload.get("symbol")
        if symbol:
            quote_snapshots_by_symbol[symbol] = payload

    execution_summary = {
        "mode": resolved_execution_mode,
        "strategy_profile": strategy_profile,
        "account_group": account_group,
        "account_ids": list(normalized_account_ids),
        "order_account_id": order_account_id,
        "service_name": service_name,
        "trade_date": trade_date,
        "snapshot_as_of": snapshot_date,
        "safe_haven_symbol": safe_haven_symbol,
        "market_currency": str(market_currency or "USD").strip().upper(),
        "target_stock_weight": signal_metadata.get("target_stock_weight"),
        "realized_stock_weight": signal_metadata.get("realized_stock_weight"),
        "target_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "realized_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "orders_submitted": [],
        "orders_pending": [],
        "orders_filled": [],
        "orders_partially_filled": [],
        "orders_skipped": [],
        "option_order_intent_count": len(option_order_intents),
        "option_order_underliers": list(option_underliers),
        "option_orders_submitted": [],
        "option_orders_pending": [],
        "option_orders_filled": [],
        "option_orders_partially_filled": [],
        "option_orders_skipped": [],
        "skipped_reasons": [],
        "target_vs_current": [],
        "execution_status": "not_started",
        "no_op_reason": None,
        "cash_reserve_dollars": 0.0,
        "safe_haven_cash_substitute_threshold_usd": float(
            max(0.0, float(safe_haven_cash_substitute_threshold_usd or 0.0))
        ),
        "safe_haven_cash_substituted_symbols": [],
        "small_account_whole_share_substituted_symbols": [],
        "small_account_safe_haven_cash_substituted_symbols": [],
        "small_account_whole_share_cash_notes": [],
        "small_account_allocation_drift_notes": [],
        "small_account_buy_blocked": False,
        "small_account_buy_block_reason": None,
        "residual_cash_estimate": float(account_values.get("buying_power", 0.0) or 0.0),
        "projected_sell_release_value": 0.0,
        "current_stock_weight": 0.0,
        "current_safe_haven_weight": 0.0,
        "price_source_mode": "market_quote",
        "snapshot_price_fallback_used": False,
        "snapshot_price_fallback_symbols": [],
        "snapshot_price_fallback_count": 0,
        "lock_path": None,
        "paper_execution_admission": {},
    }
    equity = float(account_values.get("equity", 0) or 0.0)
    cash_only_deleverage_mode = bool(signal_metadata.get("cash_only_deleverage_mode"))
    if equity <= 0:
        gross_positions = sum(
            float(details.get("quantity") or 0.0) * float(details.get("avg_cost") or 0.0)
            for details in dict(positions or {}).values()
        )
        if cash_only_execution and gross_positions > 0.0:
            equity = gross_positions
            cash_only_deleverage_mode = True
        else:
            execution_summary["execution_status"] = "blocked"
            execution_summary["no_op_reason"] = "no_equity"
            return _finalize_result([translator("no_equity")], execution_summary, return_summary=return_summary)
    execution_summary["cash_only_deleverage_mode"] = cash_only_deleverage_mode

    reserved = max(
        float(equity) * float(cash_reserve_ratio or 0.0),
        max(0.0, float(cash_reserve_floor_usd or 0.0)),
    )
    investable = equity - reserved
    target_weights, substituted_safe_haven_symbols = _apply_safe_haven_cash_substitution_to_weights(
        target_weights,
        safe_haven_symbols=safe_haven_symbols,
        investable=investable,
        threshold_usd=safe_haven_cash_substitute_threshold_usd,
    )
    execution_summary["safe_haven_cash_substituted_symbols"] = list(substituted_safe_haven_symbols)
    if safe_haven_symbols:
        execution_summary["realized_safe_haven_weight"] = float(
            sum(
                float(target_weights.get(str(symbol or "").strip().upper(), 0.0) or 0.0)
                for symbol in safe_haven_symbols
            )
        )
    threshold = equity * rebalance_threshold_ratio
    order_quantity_step = float(quantity_step or 1.0)
    minimum_order_notional = max(0.0, float(min_order_notional or 0.0))
    execution_summary["cash_reserve_dollars"] = float(reserved)

    all_symbols = set(target_weights.keys()) | set(positions.keys())
    if strategy_symbols:
        all_symbols = all_symbols & set(strategy_symbols)

    snapshot_price_fallbacks = _normalize_price_fallbacks(signal_metadata)
    price_fallback_source = str(signal_metadata.get("price_fallback_source") or "").strip() or (
        "snapshot_close" if signal_metadata.get("dry_run_price_fallbacks") else "close"
    )
    prices = get_market_prices(
        ib,
        all_symbols,
        fetch_quote_snapshots=fetch_quote_snapshots,
        quote_recorder=record_quote_snapshot,
    )
    prices, snapshot_price_fallback_symbols = _apply_snapshot_price_fallbacks(
        prices,
        all_symbols,
        dry_run_only=dry_run_only,
        snapshot_price_fallbacks=snapshot_price_fallbacks,
    )
    execution_summary["snapshot_price_fallback_used"] = bool(snapshot_price_fallback_symbols)
    execution_summary["snapshot_price_fallback_symbols"] = list(snapshot_price_fallback_symbols)
    execution_summary["snapshot_price_fallback_count"] = len(snapshot_price_fallback_symbols)
    execution_summary["price_fallback_used"] = bool(snapshot_price_fallback_symbols)
    execution_summary["price_fallback_symbols"] = list(snapshot_price_fallback_symbols)
    execution_summary["price_fallback_count"] = len(snapshot_price_fallback_symbols)
    execution_summary["price_fallback_source"] = price_fallback_source if snapshot_price_fallback_symbols else None
    execution_summary["quote_snapshot"] = {
        "quotes": list(quote_snapshots_by_symbol.values()),
    }
    if snapshot_price_fallback_symbols:
        execution_summary["price_source_mode"] = f"mixed_market_quote_{price_fallback_source}"

    current_mv = {}
    for symbol in all_symbols:
        qty = positions.get(symbol, {}).get("quantity", 0)
        price = prices.get(symbol, 0)
        current_mv[symbol] = qty * price
    current_quantities = {
        symbol: float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)
        for symbol in all_symbols
    }

    target_mv = {symbol: investable * weight for symbol, weight in target_weights.items()}
    drift_target_symbols = tuple(
        dict.fromkeys(
            str(symbol or "").strip().upper()
            for symbol in tuple(allocation["risk_symbols"]) + tuple(allocation["income_symbols"])
            if str(symbol or "").strip()
        )
    )
    if not drift_target_symbols:
        drift_target_symbols = tuple(
            symbol for symbol in target_mv if str(symbol or "").strip().upper() not in safe_haven_symbols
        )
    small_account_reference_target_mv = {
        symbol: target_mv.get(symbol, 0.0)
        for symbol in drift_target_symbols
        if symbol in target_mv
    }
    small_account_candidate_symbols = tuple(
        dict.fromkeys(
            str(symbol or "").strip().upper()
            for symbol in tuple(allocation["risk_symbols"]) + tuple(allocation["income_symbols"])
            if str(symbol or "").strip()
        )
    )
    if not small_account_candidate_symbols:
        small_account_candidate_symbols = tuple(
            str(symbol or "").strip().upper()
            for symbol in target_mv
            if str(symbol or "").strip().upper() not in safe_haven_symbols
        )
    small_account_retained_symbols = []
    small_account_bootstrap_symbols = []
    for symbol in small_account_candidate_symbols:
        target_value = max(0.0, float(target_mv.get(symbol, 0.0) or 0.0))
        price = max(0.0, float(prices.get(symbol, 0.0) or 0.0))
        limit_price = (
            _limit_buy_price(symbol, price, limit_buy_premium, limit_buy_premium_by_symbol)
            if price > 0.0
            else 0.0
        )
        # Skip bootstrap if the account cannot afford even 1 share at limit price.
        _can_afford_one_share = limit_price > 0.0 and investable >= limit_price
        held_quantity = max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0))
        if (
            _should_retain_existing_whole_share(symbol, target_value=target_value, price=price, quantity=held_quantity)
            and price > 0.0
            and 0.0 < target_value < price
            and held_quantity >= 1.0
        ):
            target_mv[symbol] = price
            if investable > 0.0:
                target_weights[symbol] = price / investable
            small_account_retained_symbols.append(symbol)
            continue
        if (
            held_quantity <= 0.0
            and 0.0 < target_value < limit_price
            and _can_afford_one_share
            and _should_bootstrap_whole_share_buy(symbol, target_value=target_value, limit_price=limit_price)
        ):
            target_mv[symbol] = limit_price
            if investable > 0.0:
                target_weights[symbol] = limit_price / investable
            small_account_bootstrap_symbols.append(symbol)
    small_account_compatibility = apply_small_account_cash_compatibility(
        target_mv,
        prices,
        candidate_symbols=small_account_candidate_symbols,
        safe_haven_cash_symbols=safe_haven_symbols,
        quantity_step=order_quantity_step,
        cash_substitute_limit_usd=SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD,
    )
    target_mv = small_account_compatibility.targets
    small_account_substituted_symbols = small_account_compatibility.whole_share_substituted_symbols
    for symbol in small_account_substituted_symbols:
        target_weights[symbol] = 0.0
    small_account_safe_haven_cash_substituted_symbols = list(
        small_account_compatibility.safe_haven_cash_substituted_symbols
    )
    for symbol in small_account_safe_haven_cash_substituted_symbols:
        target_weights[symbol] = 0.0
    if safe_haven_symbols:
        execution_summary["realized_safe_haven_weight"] = float(
            sum(
                float(target_weights.get(str(symbol or "").strip().upper(), 0.0) or 0.0)
                for symbol in safe_haven_symbols
            )
        )
    execution_summary["small_account_whole_share_substituted_symbols"] = list(
        small_account_substituted_symbols
    )
    execution_summary["small_account_safe_haven_cash_substituted_symbols"] = (
        small_account_safe_haven_cash_substituted_symbols
    )
    execution_summary["small_account_existing_whole_share_retained_symbols"] = list(
        dict.fromkeys(small_account_retained_symbols)
    )
    execution_summary["small_account_whole_share_bootstrap_symbols"] = list(
        dict.fromkeys(small_account_bootstrap_symbols)
    )
    execution_summary["small_account_whole_share_cash_notes"] = list(
        small_account_compatibility.cash_substitution_notes
    )
    trade_logs = []
    trade_logs.extend(
        format_small_account_cash_substitution_notes(
            small_account_compatibility.cash_substitution_notes,
            translator=translator,
        )
    )
    trade_logs.extend(
        _format_small_account_whole_share_bootstrap_notes(
            execution_summary["small_account_whole_share_bootstrap_symbols"],
            translator=translator,
        )
    )

    def append_small_account_allocation_drift_notes():
        if execution_summary.get("execution_status") == "blocked":
            return
        if execution_summary.get("small_account_allocation_drift_notes"):
            return
        submitted_orders = tuple(execution_summary.get("orders_submitted") or ()) + tuple(
            execution_summary.get("orders_pending") or ()
        ) + tuple(
            execution_summary.get("orders_filled") or ()
        ) + tuple(execution_summary.get("orders_partially_filled") or ())
        notes = build_small_account_allocation_drift_notes(
            target_values=small_account_reference_target_mv,
            current_values=current_mv,
            current_quantities=current_quantities,
            prices=prices,
            submitted_orders=submitted_orders,
            total_value=float(equity or 0.0),
            cash_value=_investable_buying_power(
                float(account_values.get("buying_power", 0.0) or 0.0),
                reserved,
            ),
        )
        if not notes:
            return
        execution_summary["small_account_allocation_drift_notes"] = list(notes)
        trade_logs.extend(format_small_account_allocation_drift_notes(notes, translator=translator))

    target_hash = _build_target_hash(target_weights)
    execution_summary["target_vs_current"] = _build_target_diff_rows(target_weights, current_mv, equity)
    if paper_execution_admission_enabled:
        paper_admission = evaluate_ibkr_paper_execution_admission(
            signal_metadata=signal_metadata,
            strategy_profile=strategy_profile,
            account_scope=account_group,
            positions=positions,
            prices=prices,
            target_market_values=target_mv,
            option_order_intents=option_order_intents,
            runtime_release_receipt=runtime_release_receipt,
            expected_strategy_release=expected_strategy_release,
        )
        execution_summary["paper_execution_admission"] = paper_admission
        if not paper_admission["broker_write_allowed"]:
            reason = "paper_execution_admission_blocked"
            execution_summary["execution_status"] = "blocked"
            execution_summary["no_op_reason"] = reason
            execution_summary["skipped_reasons"].append(reason)
            trade_logs.append(translator("failed", reason=reason))
            return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)
    if equity > 0:
        current_safe_haven_mv = current_mv.get(safe_haven_symbol, 0.0) if safe_haven_symbol else 0.0
        execution_summary["current_safe_haven_weight"] = float(current_safe_haven_mv / equity)
        execution_summary["current_stock_weight"] = float(
            (sum(current_mv.values()) - current_safe_haven_mv) / equity
        )

    pending_symbols = _collect_pending_symbols(
        ib,
        set(all_symbols) | set(option_underliers),
        account_ids=normalized_account_ids,
    )
    if pending_symbols:
        reason = f"pending_orders_detected:{','.join(pending_symbols)}"
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "pending_orders_detected",
                profile=_display_text(strategy_profile, fallback="<unknown>"),
                symbols=",".join(pending_symbols),
            )
        )
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)

    trade_logs.append(
        " | ".join(
            [
                translator(
                    "execution_profile_detail",
                    profile=_display_text(strategy_profile, fallback="<unknown>"),
                ),
                translator(
                    "regime_detail",
                    value=_display_text(signal_metadata.get("regime"), fallback="<none>"),
                ),
                translator(
                    "breadth_detail",
                    value=f"{float(signal_metadata.get('breadth_ratio', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "target_stock_detail",
                    value=f"{float(signal_metadata.get('target_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "realized_stock_detail",
                    value=f"{float(signal_metadata.get('realized_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "snapshot_as_of_detail",
                    value=_display_text(snapshot_date, fallback="<none>"),
                ),
                translator(
                    "trade_date_detail",
                    value=_display_text(trade_date, fallback="<none>"),
                ),
            ]
        )
    )
    if snapshot_price_fallback_symbols:
        trade_logs.append(
            translator(
                "dry_run_snapshot_prices" if dry_run_only else "price_fallback_prices",
                count=len(snapshot_price_fallback_symbols),
                symbols=_format_symbol_preview(snapshot_price_fallback_symbols),
            )
        )
    trade_logs.extend(_format_target_lines(target_weights, current_mv, equity, translator=translator))

    missing_price_symbols: list[str] = []
    insufficient_buying_power_symbols: list[str] = []
    min_notional_symbols: list[str] = []
    quantity_zero_symbols: list[str] = []
    anticipated_buying_power = get_available_buying_power(
        ib,
        account_values.get("buying_power", 0),
        account_ids=normalized_account_ids,
        currency=market_currency,
        cash_only_execution=cash_only_execution,
    )
    investable_anticipated_buying_power = _investable_buying_power(anticipated_buying_power, reserved)
    cash_sweep_quantity = 0
    cash_sweep_price = float(prices.get(safe_haven_symbol, 0.0) or 0.0) if safe_haven_symbol else 0.0
    dry_run_sale_proceeds = 0.0

    def cash_sweep_sale_quantity_to_fund_buy(max_quantity: int, candidate_symbols: tuple[str, ...]) -> int:
        if max_quantity <= 0 or not safe_haven_symbol or cash_sweep_price <= 0.0:
            return 0
        base_buying_power = max(0.0, float(investable_anticipated_buying_power))
        funding_needs = []
        for symbol in candidate_symbols:
            underweight_value = target_mv[symbol] - current_mv.get(symbol, 0.0)
            if underweight_value <= threshold:
                continue
            ask = prices.get(symbol)
            if not ask or ask <= 0.0:
                continue
            funding_needs.append(
                (
                    underweight_value,
                    _limit_buy_price(symbol, ask, limit_buy_premium, limit_buy_premium_by_symbol),
                )
            )
        if should_sell_cash_sweep_to_fund_whole_share_buy(
            float(max_quantity),
            cash_sweep_price,
            base_buying_power,
            funding_needs,
        ):
            return int(max_quantity)
        return 0

    has_sell_plan = False
    for symbol in all_symbols:
        current = current_mv.get(symbol, 0.0)
        target = target_mv.get(symbol, 0.0)
        if current <= target + threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        qty = _sell_order_quantity(
            current_value=current,
            target_value=target,
            price=price,
            position_quantity=positions.get(symbol, {}).get("quantity", 0),
            quantity_step=order_quantity_step,
        )
        if qty > 0:
            has_sell_plan = True
            break
        quantity_zero_symbols.append(symbol)

    funding_buy_candidates = [
        symbol
        for symbol in target_mv
        if symbol != safe_haven_symbol
        and (target_mv[symbol] - current_mv.get(symbol, 0.0)) > threshold
        and abs(target_mv[symbol] - current_mv.get(symbol, 0.0)) > minimum_order_notional
    ]
    if (
        not has_sell_plan
        and funding_buy_candidates
        and safe_haven_symbol
        and cash_sweep_price > 0.0
        and float(positions.get(safe_haven_symbol, {}).get("quantity", 0.0) or 0.0) > 0.0
    ):
        cash_sweep_quantity = cash_sweep_sale_quantity_to_fund_buy(
            int(float(positions.get(safe_haven_symbol, {}).get("quantity", 0.0) or 0.0)),
            tuple(funding_buy_candidates),
        )
        if cash_sweep_quantity > 0:
            has_sell_plan = True

    has_buy_plan = False
    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0.0)
        if current >= target - threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        buy_value = min(target - current, investable_anticipated_buying_power * 0.95)
        if buy_value <= 0:
            insufficient_buying_power_symbols.append(symbol)
            continue
        if buy_value < minimum_order_notional:
            min_notional_symbols.append(symbol)
            continue
        limit_price = _limit_buy_price(symbol, price, limit_buy_premium, limit_buy_premium_by_symbol)
        qty, _forced_whole_share = _planned_buy_order_quantity(
            symbol,
            buy_value=buy_value,
            limit_price=limit_price,
            quantity_step=order_quantity_step,
            investable_buying_power=investable_anticipated_buying_power,
            held_quantity=max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)),
        )
        if qty > 0:
            has_buy_plan = True
            break
        quantity_zero_symbols.append(symbol)

    if not has_sell_plan and not has_buy_plan:
        reason = "target_diff_below_threshold"
        status = "no_op"
        if missing_price_symbols:
            symbols = ",".join(sorted(dict.fromkeys(missing_price_symbols)))
            reason = f"missing_price:{symbols}"
            status = "blocked"
            execution_summary["orders_skipped"].extend(
                {"symbol": symbol, "reason": "missing_price"}
                for symbol in sorted(dict.fromkeys(missing_price_symbols))
            )
        elif insufficient_buying_power_symbols:
            symbols = ",".join(sorted(dict.fromkeys(insufficient_buying_power_symbols)))
            reason = f"insufficient_buying_power:{symbols}"
            status = "blocked"
        elif min_notional_symbols:
            symbols = ",".join(sorted(dict.fromkeys(min_notional_symbols)))
            reason = f"min_notional:{symbols}"
        elif quantity_zero_symbols:
            symbols = ",".join(sorted(dict.fromkeys(quantity_zero_symbols)))
            reason = f"quantity_zero:{symbols}"

        if not (reason == "target_diff_below_threshold" and has_executable_option_plan):
            _record_unsupported_option_intents(execution_summary, option_order_intents)
            if execution_summary["option_orders_skipped"] and reason == "target_diff_below_threshold":
                reason = "option_orders_skipped"
        execution_summary["execution_status"] = status
        execution_summary["no_op_reason"] = reason
        if reason != "target_diff_below_threshold":
            execution_summary["skipped_reasons"].append(reason)
            if status == "blocked":
                trade_logs.append(translator("failed", reason=reason))
        if not (reason == "target_diff_below_threshold" and has_executable_option_plan):
            append_small_account_allocation_drift_notes()
            return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)

    same_day_filled_symbols = _collect_same_day_filled_symbols(
        ib,
        set(all_symbols) | set(option_underliers),
        trade_date,
        account_ids=normalized_account_ids,
    )
    if same_day_filled_symbols:
        reason = f"same_day_fills_detected:{','.join(same_day_filled_symbols)}"
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "same_day_fills_detected",
                profile=_display_text(strategy_profile, fallback="<unknown>"),
                mode=resolved_execution_mode,
                symbols=",".join(same_day_filled_symbols),
                trade_date=_display_text(trade_date, fallback="<none>"),
            )
        )
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)

    lock_path = _resolve_execution_lock_path(
        strategy_profile=strategy_profile,
        account_group=account_group,
        service_name=service_name,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        execution_mode=resolved_execution_mode,
        execution_lock_dir=execution_lock_dir,
    )
    lock_payload = _build_execution_lock_payload(
        strategy_profile=strategy_profile,
        account_group=account_group,
        service_name=service_name,
        account_ids=normalized_account_ids,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        target_hash=target_hash,
        execution_mode=resolved_execution_mode,
    )
    if not _try_create_execution_lock(lock_path, lock_payload):
        existing = _read_execution_lock(lock_path) or {}
        reason = (
            f"same_day_execution_locked:mode={resolved_execution_mode}:"
            f"target_hash={existing.get('target_hash', '<unknown>')}"
        )
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        execution_summary["lock_path"] = str(lock_path)
        trade_logs.append(
            translator(
                "same_day_execution_locked",
                profile=_display_text(strategy_profile, fallback="<unknown>"),
                mode=resolved_execution_mode,
                trade_date=_display_text(trade_date, fallback="<none>"),
                snapshot_date=_display_text(snapshot_date, fallback="<none>"),
                target_hash=_display_text(existing.get("target_hash"), fallback="<unknown>"),
                lock_path=str(lock_path),
            )
        )
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)
    execution_summary["lock_path"] = str(lock_path)
    trade_logs.append(
        translator(
            "execution_lock_acquired",
            mode=resolved_execution_mode,
            trade_date=_display_text(trade_date, fallback="<none>"),
            snapshot_date=_display_text(snapshot_date, fallback="<none>"),
            lock_path=str(lock_path),
        )
    )
    execution_summary["execution_status"] = "executing"

    sell_executed = False
    projected_sell_release_value = 0.0
    pending_sell_release_symbols: list[str] = []
    for symbol in all_symbols:
        current = current_mv.get(symbol, 0)
        target = target_mv.get(symbol, 0)
        price = prices.get(symbol)
        if symbol == safe_haven_symbol and cash_sweep_quantity > 0:
            if not price:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "missing_price"})
                execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
                continue
            regular_qty = _sell_order_quantity(
                current_value=current,
                target_value=target,
                price=price,
                position_quantity=positions.get(symbol, {}).get("quantity", 0),
                quantity_step=order_quantity_step,
            )
            qty = max(int(cash_sweep_quantity), int(regular_qty))
            if qty <= 0:
                if current > target + threshold:
                    pending_sell_release_symbols.append(symbol)
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "quantity_zero"})
                continue
        elif current > target + threshold:
            if not price:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "missing_price"})
                execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
                continue
            qty = _sell_order_quantity(
                current_value=current,
                target_value=target,
                price=price,
                position_quantity=positions.get(symbol, {}).get("quantity", 0),
                quantity_step=order_quantity_step,
            )
            if qty <= 0:
                pending_sell_release_symbols.append(symbol)
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "quantity_zero"})
                continue
        else:
            continue

        if dry_run_only:
            execution_summary["orders_submitted"].append(
                {"symbol": symbol, "side": "sell", "quantity": qty, "status": "dry_run"}
            )
            trade_logs.append(f"DRY_RUN sell {symbol} {format_quantity(qty)}")
            dry_run_sale_proceeds += float(qty) * float(price)
            continue
        report = submit_order_intent(
            ib,
            order_intent_cls(
                symbol=symbol,
                side="sell",
                quantity=qty,
                account_id=order_account_id,
            ),
        )
        _, status_msg = check_order_submitted(report, translator=translator)
        status = str(getattr(report, "status", "") or "")
        order_payload = {
            "symbol": symbol,
            "side": "sell",
            "quantity": qty,
            "status": status,
            "broker_order_id": getattr(report, "broker_order_id", None),
        }
        outcome = _record_order_outcome(execution_summary, order_payload, status=status)
        trade_logs.append(translator("market_sell", symbol=symbol, qty=format_quantity(qty)) + f" {status_msg}")
        if outcome != "failed":
            sell_executed = True
            projected_sell_release_value += _projected_sell_release_value_for_report(
                report,
                fallback_price=price,
                fallback_quantity=qty,
            )

    if dry_run_only:
        buying_power = max(0.0, anticipated_buying_power + dry_run_sale_proceeds)
    else:
        if sell_executed:
            time.sleep(sell_settle_delay_sec)
            buying_power = get_available_buying_power(
                ib,
                account_values.get("buying_power", 0),
                account_ids=normalized_account_ids,
                currency=market_currency,
                cash_only_execution=cash_only_execution,
            )
            if cash_only_execution and projected_sell_release_value > 0.0:
                buying_power = max(
                    float(buying_power or 0.0),
                    float(anticipated_buying_power or 0.0) + float(projected_sell_release_value),
                )
        else:
            buying_power = anticipated_buying_power
    investable_buying_power = _investable_buying_power(buying_power, reserved)
    execution_summary["projected_sell_release_value"] = float(projected_sell_release_value)
    pending_sell_release_symbols = list(dict.fromkeys(pending_sell_release_symbols))
    buy_needed_symbols = [
        symbol
        for symbol, target in target_mv.items()
        if current_mv.get(symbol, 0.0) < float(target or 0.0) - threshold
    ]
    buys_blocked_reason = None
    if bool(signal_metadata.get("small_account_warning")) and buy_needed_symbols:
        buys_blocked_reason = "small_account_below_recommended_equity"
        execution_summary["small_account_buy_blocked"] = True
        execution_summary["small_account_buy_block_reason"] = buys_blocked_reason
        execution_summary["skipped_reasons"].append(buys_blocked_reason)
        trade_logs.append(
            translator(
                "buy_deferred_small_account",
                portfolio_equity=f"{float(equity or 0.0):,.2f}",
                min_recommended_equity=f"{float(signal_metadata.get('min_recommended_equity_usd') or 0.0):,.2f}",
            )
        )
    if buys_blocked_reason is None and cash_only_execution and pending_sell_release_symbols and buy_needed_symbols:
        if _rotation_guard_should_block_buys(
            pending_sell_release_symbols=pending_sell_release_symbols,
            buy_needed_symbols=buy_needed_symbols,
            current_mv=current_mv,
            target_mv=target_mv,
            threshold=threshold,
            investable_buying_power=investable_buying_power,
            prices=prices,
            limit_buy_premium=limit_buy_premium,
            limit_buy_premium_by_symbol=limit_buy_premium_by_symbol,
            quantity_step=order_quantity_step,
            minimum_order_notional=minimum_order_notional,
        ):
            buys_blocked_reason = "pending_sell_release"
            release_symbols = _format_symbol_preview(tuple(pending_sell_release_symbols))
            execution_summary["pending_sell_release_symbols"] = list(pending_sell_release_symbols)
            execution_summary["skipped_reasons"].append(
                f"pending_sell_release:{','.join(pending_sell_release_symbols)}"
            )
            trade_logs.append(
                translator(
                    "buy_deferred_pending_sell_release",
                    symbols=release_symbols,
                )
            )
    if buys_blocked_reason is None and cash_only_execution:
        raw_cash = _cash_value_for_currency(
            list(ib.accountValues() or ()),
            currency=market_currency,
            account_ids=normalized_account_ids,
        )
        if raw_cash is not None and float(raw_cash) < 0.0 and buy_needed_symbols:
            buys_blocked_reason = "negative_cash"
            execution_summary["skipped_reasons"].append(f"negative_cash:{float(raw_cash):.2f}")
            trade_logs.append(
                translator(
                    "buy_deferred_negative_cash",
                    cash=f"{float(raw_cash):,.2f}",
                )
            )

    for symbol, target in target_mv.items():
        if buys_blocked_reason:
            if symbol in buy_needed_symbols:
                execution_summary["orders_skipped"].append(
                    {"symbol": symbol, "side": "buy", "reason": buys_blocked_reason}
                )
            continue
        current = current_mv.get(symbol, 0)
        if current < target - threshold:
            buy_value = min(target - current, investable_buying_power * 0.95)
            price = prices.get(symbol)
            if not price:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "missing_price"})
                execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
                continue
            if buy_value < minimum_order_notional:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "min_notional"})
                continue

            limit_price = _limit_buy_price(symbol, price, limit_buy_premium, limit_buy_premium_by_symbol)
            held_quantity = max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0))
            qty, forced_whole_share = _planned_buy_order_quantity(
                symbol,
                buy_value=buy_value,
                limit_price=limit_price,
                quantity_step=order_quantity_step,
                investable_buying_power=investable_buying_power,
                held_quantity=held_quantity,
            )
            if (
                forced_whole_share
                and symbol not in execution_summary["small_account_whole_share_bootstrap_symbols"]
            ):
                execution_summary["small_account_whole_share_bootstrap_symbols"].append(symbol)
                trade_logs.extend(
                    _format_small_account_whole_share_bootstrap_notes(
                        (symbol,),
                        translator=translator,
                    )
                )
            if qty <= 0:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "quantity_zero"})
                continue

            order_cost = float(qty) * float(limit_price)
            if order_cost > investable_buying_power:
                qty = _floor_order_quantity(
                    investable_buying_power / limit_price,
                    quantity_step=order_quantity_step,
                )
                order_cost = float(qty) * float(limit_price)
            if qty <= 0 or order_cost > investable_buying_power:
                execution_summary["orders_skipped"].append(
                    {"symbol": symbol, "side": "buy", "reason": "insufficient_buying_power"}
                )
                execution_summary["skipped_reasons"].append(f"insufficient_buying_power:{symbol}")
                continue

            if dry_run_only:
                execution_summary["orders_submitted"].append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "quantity": qty,
                        "limit_price": limit_price,
                        "status": "dry_run",
                    }
                )
                trade_logs.append(f"DRY_RUN buy {symbol} {format_quantity(qty)} @{limit_price:.2f}")
                investable_buying_power -= qty * limit_price
                continue
            report = submit_order_intent(
                ib,
                order_intent_cls(
                    symbol=symbol,
                    side="buy",
                    quantity=qty,
                    order_type="limit",
                    limit_price=limit_price,
                    time_in_force="DAY",
                    account_id=order_account_id,
                ),
            )
            _, status_msg = check_order_submitted(report, translator=translator)
            status = str(getattr(report, "status", "") or "")
            order_payload = {
                "symbol": symbol,
                "side": "buy",
                "quantity": qty,
                "limit_price": limit_price,
                "status": status,
                "broker_order_id": getattr(report, "broker_order_id", None),
            }
            outcome = _record_order_outcome(execution_summary, order_payload, status=status)
            trade_logs.append(
                translator("limit_buy", symbol=symbol, qty=format_quantity(qty), price=f"{limit_price:.2f}") + f" {status_msg}"
            )
            if outcome != "failed":
                investable_buying_power -= qty * limit_price

    buying_power = _execute_option_order_intents(
        ib,
        option_order_intents,
        submit_order_intent=submit_order_intent,
        order_intent_cls=order_intent_cls,
        translator=translator,
        execution_summary=execution_summary,
        trade_logs=trade_logs,
        dry_run_only=dry_run_only,
        order_account_id=order_account_id,
        buying_power=buying_power,
    )

    has_terminal_order = bool(
        execution_summary["orders_submitted"]
        or execution_summary["orders_filled"]
        or execution_summary["orders_partially_filled"]
        or execution_summary["option_orders_submitted"]
        or execution_summary["option_orders_filled"]
        or execution_summary["option_orders_partially_filled"]
    )
    has_pending_order = bool(
        execution_summary["orders_pending"] or execution_summary["option_orders_pending"]
    )
    submission_failure = next(
        (
            reason
            for reason in execution_summary["skipped_reasons"]
            if reason.startswith(("submit_failed:", "option_submit_failed:"))
        ),
        None,
    )
    if has_pending_order:
        execution_summary["execution_status"] = "pending_reconciliation"
        execution_summary["no_op_reason"] = "broker_order_pending_confirmation"
    elif has_terminal_order:
        execution_summary["execution_status"] = "executed"
        execution_summary["no_op_reason"] = None
    elif submission_failure:
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = submission_failure
        trade_logs.append(translator("failed", reason=submission_failure))
    else:
        execution_summary["execution_status"] = "no_op"
    execution_summary["residual_cash_estimate"] = float(max(buying_power, 0.0))
    append_small_account_allocation_drift_notes()
    return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)
