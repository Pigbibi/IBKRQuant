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

ACCOUNT_NEW_RISK_GATE_ENV = "ACCOUNT_NEW_RISK_GATE"

_DEFAULT_HEALTH = {
    "observation_status": "COMPLETE",
    "reconciliation_status": "VERIFIED",
    "circuit_breaker_state": "CLOSED",
}

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
    projection = dict(metadata.get("account_new_risk_snapshot") or {})
    if projection:
        portfolio["account_new_risk_snapshot"] = projection
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
    projection = dict(portfolio.get("account_new_risk_snapshot") or {})
    equity_usd = _coerce_optional_float(projection.get("equity_usd"))
    if equity_usd is None:
        equity_usd = _resolve_equity_usd(portfolio, execution)
    return InjectedReconciliationSnapshot(
        observation_status=str(
            projection.get("observation_status") or _DEFAULT_HEALTH["observation_status"]
        ),
        reconciliation_status=str(
            projection.get("reconciliation_status") or _DEFAULT_HEALTH["reconciliation_status"]
        ),
        circuit_breaker_state=str(
            projection.get("circuit_breaker_state") or _DEFAULT_HEALTH["circuit_breaker_state"]
        ),
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
