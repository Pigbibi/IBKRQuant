"""IBKR adapter for QPK account-level NEW_RISK gate (fail-closed)."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from quant_platform_kit.risk.account_new_risk_gate import (
    AccountNewRiskGateError,
    InjectedReconciliationSnapshot,
    NewRiskAdmissionResult,
    NewRiskDisposition,
    evaluate_new_risk_admission,
)
from quant_platform_kit.risk.cycle_new_risk_health import (
    CycleNewRiskHealthEvidence,
    apply_cycle_new_risk_health_axes,
)

ACCOUNT_NEW_RISK_GATE_ENV = "ACCOUNT_NEW_RISK_GATE"

_cycle_snapshot: InjectedReconciliationSnapshot | None = None


def is_account_new_risk_gate_enabled() -> bool:
    """Production default on; set ACCOUNT_NEW_RISK_GATE=0 only for tests."""
    return str(os.environ.get(ACCOUNT_NEW_RISK_GATE_ENV, "") or "").strip() != "0"


def _coerce_optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _resolve_equity_usd(portfolio: Mapping[str, Any], execution: Mapping[str, Any] | None) -> float | None:
    for key in ("total_equity", "total_strategy_equity", "equity"):
        equity = _coerce_optional_float(portfolio.get(key))
        if equity is not None and equity > 0.0:
            return equity
    if execution is not None:
        equity = _coerce_optional_float(execution.get("portfolio_total_equity"))
        if equity is not None and equity > 0.0:
            return equity
    return None


def _portfolio_metadata(portfolio: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = portfolio.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _resolve_unknown_pending(portfolio: Mapping[str, Any], projection: Mapping[str, Any]) -> bool:
    """Only an explicit unknown-pending signal counts; absence is never treated as pending."""
    if "unknown_pending_orders" in projection:
        return bool(projection.get("unknown_pending_orders"))
    if portfolio.get("unknown_pending_orders") is not None:
        return bool(portfolio.get("unknown_pending_orders"))
    return bool(_portfolio_metadata(portfolio).get("unknown_pending_orders"))


def _resolve_durable_breaker_open(portfolio: Mapping[str, Any], projection: Mapping[str, Any]) -> bool:
    """Only an explicit durable OPEN state trips the breaker; absence is never OPEN."""
    for source in (projection, portfolio, _portfolio_metadata(portfolio)):
        value = source.get("durable_circuit_breaker_state")
        if value is not None:
            return str(value).strip().upper() == "OPEN"
    return False


def build_account_new_risk_snapshot(
    portfolio: Mapping[str, Any],
    *,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a cycle-health projection from evidence available this cycle.

    ``observation_ok`` reflects whether equity resolved this cycle;
    ``unknown_pending`` and ``durable_breaker_open`` only trip on an explicit
    signal from the portfolio or its ``metadata`` -- their absence is never
    mistaken for a durable OPEN breaker or pending reconciliation. Explicit
    keys already present on an injected ``account_new_risk_snapshot`` always
    win over the projected axes (tests / HITL overrides).
    """
    projection = dict(portfolio.get("account_new_risk_snapshot") or {})
    equity_usd = _coerce_optional_float(projection.get("equity_usd"))
    if equity_usd is None:
        equity_usd = _resolve_equity_usd(portfolio, execution)

    evidence = CycleNewRiskHealthEvidence(
        observation_ok=equity_usd is not None,
        unknown_pending=_resolve_unknown_pending(portfolio, projection),
        durable_breaker_open=_resolve_durable_breaker_open(portfolio, projection),
    )
    projection = apply_cycle_new_risk_health_axes(projection, evidence)
    projection["equity_usd"] = equity_usd
    return projection


def build_portfolio_from_account_values(
    account_values: Mapping[str, Any],
    *,
    signal_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a portfolio dict from an existing IBKR account_values read."""
    metadata = dict(signal_metadata or {})
    portfolio: dict[str, Any] = {
        "total_equity": _coerce_optional_float(account_values.get("equity")),
    }
    for key in ("unknown_pending_orders", "durable_circuit_breaker_state"):
        if key in account_values:
            portfolio[key] = account_values[key]
        elif key in metadata:
            portfolio[key] = metadata[key]
    portfolio["account_new_risk_snapshot"] = build_account_new_risk_snapshot(
        {
            **portfolio,
            "account_new_risk_snapshot": metadata.get("account_new_risk_snapshot"),
        }
    )
    for key in ("peak_equity_usd", "drawdown_from_peak", "realized_vol"):
        if key in metadata:
            portfolio[key] = metadata[key]
    return portfolio


def build_snapshot_from_portfolio(
    portfolio: Mapping[str, Any],
    *,
    execution: Mapping[str, Any] | None = None,
) -> InjectedReconciliationSnapshot:
    """Project an injected reconciliation snapshot from an existing portfolio read."""
    projection = build_account_new_risk_snapshot(portfolio, execution=execution)
    equity_usd = _coerce_optional_float(projection.get("equity_usd"))
    if equity_usd is None:
        equity_usd = _resolve_equity_usd(portfolio, execution)
    return InjectedReconciliationSnapshot(
        observation_status=str(projection.get("observation_status") or "UNAVAILABLE"),
        reconciliation_status=str(projection.get("reconciliation_status") or "UNVERIFIED"),
        circuit_breaker_state=str(projection.get("circuit_breaker_state") or "OPEN"),
        equity_usd=equity_usd,
        peak_equity_usd=_coerce_optional_float(projection.get("peak_equity_usd"))
        if "peak_equity_usd" in projection
        else _coerce_optional_float(portfolio.get("peak_equity_usd")),
        drawdown_from_peak=_coerce_optional_float(projection.get("drawdown_from_peak"))
        if "drawdown_from_peak" in projection
        else _coerce_optional_float(portfolio.get("drawdown_from_peak")),
        realized_vol=_coerce_optional_float(projection.get("realized_vol"))
        if "realized_vol" in projection
        else _coerce_optional_float(portfolio.get("realized_vol")),
    )


def evaluate_portfolio_new_risk_admission(
    portfolio: Mapping[str, Any],
    *,
    execution: Mapping[str, Any] | None = None,
) -> NewRiskAdmissionResult:
    try:
        snapshot = build_snapshot_from_portfolio(portfolio, execution=execution)
        return evaluate_new_risk_admission(snapshot)
    except AccountNewRiskGateError:
        return NewRiskAdmissionResult(
            disposition=NewRiskDisposition.NEW_RISK_PROHIBITED,
            reason_codes=("SNAPSHOT_VALIDATION_FAIL_CLOSED",),
        )


def evaluate_account_values_new_risk_admission(
    account_values: Mapping[str, Any],
    *,
    signal_metadata: Mapping[str, Any] | None = None,
) -> NewRiskAdmissionResult:
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    return evaluate_portfolio_new_risk_admission(portfolio)


def new_risk_buy_prohibited(result: NewRiskAdmissionResult) -> bool:
    return result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED


def apply_combined_scale(value: float, scale: float | None) -> float:
    """Apply a valid reducing scale; missing or out-of-range values are a no-op."""
    if scale is None or not math.isfinite(scale) or not 0.0 < scale <= 1.0:
        return value
    return value * scale


def get_cycle_snapshot() -> InjectedReconciliationSnapshot | None:
    return _cycle_snapshot


def set_cycle_snapshot(snapshot: InjectedReconciliationSnapshot | None) -> None:
    global _cycle_snapshot
    _cycle_snapshot = snapshot


@contextmanager
def account_new_risk_gate_cycle(
    account_values: Mapping[str, Any],
    *,
    signal_metadata: Mapping[str, Any] | None = None,
):
    """Bind one account_values projection for the current execution cycle."""
    previous = _cycle_snapshot
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    set_cycle_snapshot(build_snapshot_from_portfolio(portfolio))
    try:
        yield
    finally:
        set_cycle_snapshot(previous)


def evaluate_cycle_new_risk_admission() -> NewRiskAdmissionResult:
    if _cycle_snapshot is None:
        return NewRiskAdmissionResult(
            disposition=NewRiskDisposition.NEW_RISK_PROHIBITED,
            reason_codes=("EQUITY_UNKNOWN_FAIL_CLOSED",),
        )
    try:
        return evaluate_new_risk_admission(_cycle_snapshot)
    except AccountNewRiskGateError:
        return NewRiskAdmissionResult(
            disposition=NewRiskDisposition.NEW_RISK_PROHIBITED,
            reason_codes=("SNAPSHOT_VALIDATION_FAIL_CLOSED",),
        )
