"""Fail-closed PAPER admission for IBKR's ordinary rebalance path.

This adapter deliberately has no broker dependency.  A strategy/control-plane
producer supplies an immutable QPK ``ExecutionCommand`` in signal metadata;
this module verifies its embedded deterministic-risk receipt and the current
runtime release before the normal rebalance service can submit any order.

Exposure is classified from reconciled quantities and the quotes used by this
cycle.  In particular, a buy/sell label is never treated as evidence that an
order increases or reduces risk.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from quant_platform_kit.common.execution_commands import ExecutionCommand
from quant_platform_kit.common.paper_execution_admission import (
    PAPER_RISK_ADMISSION_RECEIPT_INTENT_FIELD,
    PaperRiskAdmissionReceipt,
    evaluate_paper_execution_admission,
)
from quant_platform_kit.common.runtime_command_gate import (
    RuntimeCommandAction,
    RuntimeCommandExposureEffect,
    RuntimeCommandGateEnforcement,
    RuntimeCommandGatePolicy,
    evaluate_runtime_command_gate,
)
from quant_platform_kit.common.runtime_target import RuntimeExecutionEnvironment


PAPER_EXECUTION_ADMISSION_SCHEMA_VERSION = "ibkr.paper_execution_admission.v1"
PAPER_EXECUTION_COMMAND_SIGNAL_FIELD = "paper_execution_command"
_PAPER_ADMISSION_GATE_POLICY = RuntimeCommandGatePolicy(
    enforcement=RuntimeCommandGateEnforcement.ENFORCE,
)
_EPSILON = 0.01


def resolve_paper_execution_admission_enabled(
    *,
    env_reader,
    dry_run_only: bool,
    execution_mode: object,
    execution_environment: object | None = None,
) -> bool:
    """Resolve the opt-in flag and require an explicit broker PAPER target."""

    raw_value = str(env_reader("IBKR_PAPER_EXECUTION_ADMISSION_ENABLED", "") or "").strip().lower()
    enabled = raw_value in {"1", "true", "t", "yes", "y", "on"}
    if not enabled:
        return False
    normalized_mode = str(execution_mode or "").strip().lower().replace("-", "_")
    normalized_environment = str(
        getattr(execution_environment, "value", execution_environment) or ""
    ).strip().lower()
    if (
        dry_run_only
        or normalized_mode != "paper"
        or normalized_environment != RuntimeExecutionEnvironment.PAPER.value
    ):
        raise RuntimeError(
            "IBKR_PAPER_EXECUTION_ADMISSION_ENABLED requires "
            "dry_run_only=false, execution_mode=paper, and execution_environment=paper"
        )
    return True


def _append_finding(findings: list[str], finding: str) -> None:
    if finding not in findings:
        findings.append(finding)


def _finite_nonnegative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0:
        return None
    return number


def _effective_session(signal_metadata: Mapping[str, object]) -> str | None:
    value = signal_metadata.get("effective_date") or signal_metadata.get("trade_date")
    text = str(value or "").strip()
    return text[:10] or None


def _load_command(signal_metadata: Mapping[str, object]) -> tuple[ExecutionCommand | None, list[str]]:
    raw_command = signal_metadata.get(PAPER_EXECUTION_COMMAND_SIGNAL_FIELD)
    if not isinstance(raw_command, Mapping):
        return None, ["paper_risk_admission_receipt_missing"]
    try:
        return ExecutionCommand.from_dict(raw_command), []
    except (TypeError, ValueError):
        return None, ["command_digest_mismatch"]


def _command_contract_findings(
    command: ExecutionCommand | None,
    *,
    strategy_profile: object,
    account_scope: object,
    effective_session: str | None,
) -> list[str]:
    if command is None:
        return []
    findings: list[str] = []
    if command.platform != "ibkr":
        _append_finding(findings, "durable_event_history_invalid")
    if command.execution_mode != "paper":
        _append_finding(findings, "paper_execution_mode_invalid")
    if command.strategy_profile != str(strategy_profile or "").strip().lower():
        _append_finding(findings, "command_digest_mismatch")
    if command.account_scope != str(account_scope or "").strip().lower():
        _append_finding(findings, "durable_event_history_invalid")
    if not effective_session or command.effective_date != effective_session:
        _append_finding(findings, "signal_timing_invalid")
    return findings


def _exposure_facts(
    *,
    positions: Mapping[str, Mapping[str, object]],
    prices: Mapping[str, object],
    target_market_values: Mapping[str, object],
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    """Classify target changes using position quantities and current quotes."""

    normalized_positions = {
        str(symbol).strip().upper(): details
        for symbol, details in positions.items()
        if str(symbol).strip() and isinstance(details, Mapping)
    }
    normalized_prices = {
        str(symbol).strip().upper(): value
        for symbol, value in prices.items()
        if str(symbol).strip()
    }
    normalized_targets = {
        str(symbol).strip().upper(): value
        for symbol, value in target_market_values.items()
        if str(symbol).strip()
    }
    facts: list[dict[str, object]] = []
    findings: list[str] = []
    symbols = sorted(set(normalized_positions) | set(normalized_targets))
    for symbol in symbols:
        position = normalized_positions.get(symbol) or {}
        quantity = _finite_nonnegative(position.get("quantity"))
        target_value = _finite_nonnegative(normalized_targets.get(symbol, 0.0))
        price = _finite_nonnegative(normalized_prices.get(symbol))
        if quantity is None or target_value is None or price is None or price <= 0.0:
            _append_finding(findings, "position_reconciliation_mismatch")
            continue
        current_value = quantity * price
        before_exposure = abs(current_value)
        after_exposure = abs(target_value)
        exposure_delta = after_exposure - before_exposure
        if exposure_delta > _EPSILON:
            effect = RuntimeCommandExposureEffect.INCREASES
        elif exposure_delta < -_EPSILON:
            effect = RuntimeCommandExposureEffect.REDUCES
        else:
            effect = RuntimeCommandExposureEffect.NEUTRAL
        facts.append(
            {
                "symbol": symbol,
                "position_quantity": round(quantity, 8),
                "quote_price": round(price, 8),
                "current_market_value": round(current_value, 8),
                "target_market_value": round(target_value, 8),
                "exposure_effect": effect.value,
            }
        )
    return tuple(facts), tuple(findings)


def evaluate_ibkr_paper_execution_admission(
    *,
    signal_metadata: Mapping[str, object] | None,
    strategy_profile: object,
    account_scope: object,
    positions: Mapping[str, Mapping[str, object]],
    prices: Mapping[str, object],
    target_market_values: Mapping[str, object],
    option_order_intents: Sequence[Mapping[str, object]] = (),
    runtime_release_receipt: Mapping[str, Any] | None,
    expected_strategy_release: Any = None,
) -> dict[str, object]:
    """Return durable audit evidence and block if a PAPER broker write is unsafe.

    The caller must invoke this after it has collected the current portfolio
    and quotes, but before it invokes any submit adapter.
    """

    metadata = signal_metadata if isinstance(signal_metadata, Mapping) else {}
    command, findings = _load_command(metadata)
    effective_session = _effective_session(metadata)
    for finding in _command_contract_findings(
        command,
        strategy_profile=strategy_profile,
        account_scope=account_scope,
        effective_session=effective_session,
    ):
        _append_finding(findings, finding)

    paper_receipt: Mapping[str, object] | None = None
    if command is not None:
        raw_receipt = command.intent.get(PAPER_RISK_ADMISSION_RECEIPT_INTENT_FIELD)
        if isinstance(raw_receipt, Mapping):
            try:
                paper_receipt = PaperRiskAdmissionReceipt.from_dict(raw_receipt).to_dict()
            except (TypeError, ValueError):
                # Invalid untrusted payloads must not be copied into reports.
                paper_receipt = None
        admission = evaluate_paper_execution_admission(
            command=command,
            expected_strategy_release=expected_strategy_release,
        )
        for finding in admission.integrity_findings:
            _append_finding(findings, finding)
    else:
        admission = None

    if option_order_intents:
        # The ordinary equity target model has no reconciled option valuation
        # contract yet.  Treating a side label as exposure evidence would be
        # unsafe, so PAPER admission closes the whole cycle instead.
        _append_finding(findings, "durable_event_history_invalid")

    facts, fact_findings = _exposure_facts(
        positions=positions,
        prices=prices,
        target_market_values=target_market_values,
    )
    for finding in fact_findings:
        _append_finding(findings, finding)

    effects = [RuntimeCommandExposureEffect(fact["exposure_effect"]) for fact in facts]
    if not effects:
        effects = [RuntimeCommandExposureEffect.NEUTRAL]
    gate_receipts = []
    for effect in effects:
        decision = evaluate_runtime_command_gate(
            action=RuntimeCommandAction.SUBMIT,
            exposure_effect=effect,
            command=command,
            as_of_session=effective_session,
            runtime_release_receipt=runtime_release_receipt,
            expected_strategy_release=expected_strategy_release,
            integrity_findings=findings,
            policy=_PAPER_ADMISSION_GATE_POLICY,
        )
        gate_receipts.append(decision.to_receipt())

    return {
        "schema_version": PAPER_EXECUTION_ADMISSION_SCHEMA_VERSION,
        "enabled": True,
        "command_id": command.command_id if command is not None else None,
        "decision_digest": command.decision_digest if command is not None else None,
        "effective_session": effective_session,
        "risk_admission_receipt": dict(paper_receipt or {}),
        "risk_admission_receipt_sha256": (
            admission.receipt_sha256 if admission is not None else None
        ),
        "risk_disposition": admission.disposition.value if admission is not None else "halted",
        "integrity_findings": list(dict.fromkeys(findings)),
        "exposure_facts": list(facts),
        "runtime_command_gate_receipts": gate_receipts,
        "broker_write_allowed": bool(gate_receipts) and all(
            bool(receipt["broker_write_allowed"]) for receipt in gate_receipts
        ),
    }


__all__ = [
    "PAPER_EXECUTION_ADMISSION_SCHEMA_VERSION",
    "PAPER_EXECUTION_COMMAND_SIGNAL_FIELD",
    "evaluate_ibkr_paper_execution_admission",
    "resolve_paper_execution_admission_enabled",
]
