#!/usr/bin/env python3
"""Verify one confirmed IBKR legacy-recovery request without applying it.

The verifier is a private-control-plane building block.  It reads the bounded
QRS confirmation endpoint, optional advisory review, designated source records, a
private QPK baseline candidate, the currently deployed runtime target, and a
new read-only ``/reconcile`` receipt.  It only returns a QPK transition *plan*
when every value agrees; it never updates Cloud Run, a runtime target, a
broker setting, an execution marker, or an order.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quant_platform_kit.common.reconciliation_recovery import (
    ReconciliationRecoveryConfirmation,
    evaluate_reconciliation_recovery_activation,
)

from application.broker_reconciliation_candidate import SourceReceiptExpectation
from scripts.build_reconciliation_baseline_candidate import extract_reconciliation_evidence, load_source_inputs
from scripts.publish_reconciliation_recovery_source import (
    _load_json,
    _parse_time,
    extract_baseline_candidate,
    extract_bound_dual_review,
)


RECONCILIATION_RECOVERY_CONTROLLER_TOKEN_ENV = "RECONCILIATION_RECOVERY_CONTROLLER_TOKEN"
CONTROLLER_READ_SCHEMA_VERSION = "qsl_reconciliation_recovery_controller_read.v1"
VERIFY_ONLY_SCHEMA_VERSION = "ibkr_reconciliation_recovery_verification.v1"


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward a controller token to an HTTP redirect destination."""

    def redirect_request(self, request: Request, *_args: object, **_kwargs: object) -> None:
        return None


def _require_controller_read_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    expected_suffix = "/api/internal/reconciliation-recovery-confirmation"
    if not normalized.startswith("https://"):
        raise ValueError("confirmation_url must use https")
    if not normalized.endswith(expected_suffix):
        raise ValueError("confirmation_url must target the reconciliation recovery controller read endpoint")
    return normalized


def read_console_confirmation(
    *,
    confirmation_url: str,
    recovery_id: str,
    token: str,
    opener: Callable[..., Any] | None = None,
) -> dict[str, object]:
    """Read one current confirmation from QRS using its dedicated token."""

    normalized_url = _require_controller_read_url(confirmation_url)
    normalized_token = str(token or "").strip()
    if not normalized_token:
        raise ValueError(f"{RECONCILIATION_RECOVERY_CONTROLLER_TOKEN_ENV} is required")
    request = Request(
        f"{normalized_url}?{urlencode({'recovery_id': recovery_id})}",
        headers={"Authorization": f"Bearer {normalized_token}"},
        method="GET",
    )
    request_opener = opener or build_opener(_NoRedirect).open
    try:
        with request_opener(request, timeout=15) as response:
            status_code = int(response.status)
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"reconciliation recovery confirmation read returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("reconciliation recovery confirmation read failed") from exc
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"reconciliation recovery confirmation read returned HTTP {status_code}")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("reconciliation recovery confirmation read returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("reconciliation recovery confirmation was not acknowledged")
    return payload


def _runtime_target_state(
    payload: Mapping[str, Any],
    *,
    candidate: Any,
) -> tuple[str, tuple[str, ...]]:
    """Check only the non-sensitive deployed runtime identity and continuity state."""

    findings: list[str] = []
    if str(payload.get("platform_id") or "").strip() != candidate.platform_id:
        findings.append("ibkr_reconciliation_runtime_target_platform_mismatch")
    if str(payload.get("strategy_profile") or "").strip() != candidate.strategy_profile:
        findings.append("ibkr_reconciliation_runtime_target_strategy_mismatch")
    continuity = payload.get("live_continuity")
    if not isinstance(continuity, Mapping):
        return "", tuple(findings + ["ibkr_reconciliation_runtime_target_continuity_missing"])
    state = str(continuity.get("state") or "").strip().upper()
    if str(continuity.get("baseline_id") or "").strip() != candidate.baseline_id:
        findings.append("ibkr_reconciliation_runtime_target_baseline_mismatch")
    if str(continuity.get("baseline_target_sha256") or "").strip().lower() != candidate.baseline_target_sha256:
        findings.append("ibkr_reconciliation_runtime_target_baseline_digest_mismatch")
    return state, tuple(dict.fromkeys(findings))


def _verify_console_response(
    payload: Mapping[str, Any],
    *,
    recovery_id: str,
    candidate: Any,
) -> ReconciliationRecoveryConfirmation:
    if payload.get("schema_version") != CONTROLLER_READ_SCHEMA_VERSION:
        raise ValueError("controller confirmation response has an unsupported schema_version")
    policy = payload.get("policy")
    if not isinstance(policy, Mapping) or policy != {
        "no_order": True,
        "execution_authority_granted": False,
        "controller_must_reverify": True,
    }:
        raise ValueError("controller confirmation response has an invalid non-execution policy")
    recovery = payload.get("recovery")
    if not isinstance(recovery, Mapping):
        raise ValueError("controller confirmation response is missing recovery metadata")
    if str(recovery.get("recovery_id") or "").strip() != recovery_id:
        raise ValueError("controller confirmation recovery_id mismatch")
    if str(recovery.get("platform") or "").strip() != "ibkr":
        raise ValueError("controller confirmation platform mismatch")
    if str(recovery.get("strategy_profile") or "").strip() != candidate.strategy_profile:
        raise ValueError("controller confirmation strategy_profile mismatch")
    if str(recovery.get("environment") or "").strip().lower() != "live":
        raise ValueError("controller confirmation environment mismatch")
    if str(recovery.get("reconciliation_state") or "").strip().upper() != "RECONCILE_ONLY":
        raise ValueError("controller confirmation is not reconcile-only")
    if str(recovery.get("candidate_sha256") or "").strip().lower() != candidate.candidate_sha256:
        raise ValueError("controller confirmation candidate binding mismatch")
    if str(recovery.get("dual_review_binding_sha256") or "").strip().lower() != candidate.candidate_sha256:
        raise ValueError("controller confirmation dual review binding mismatch")
    count = recovery.get("evidence_sample_count")
    if type(count) is not int or count != len(candidate.source_evidence_sha256):
        raise ValueError("controller confirmation evidence sample count does not match candidate")
    confirmation = payload.get("confirmation")
    if not isinstance(confirmation, Mapping):
        raise ValueError("controller confirmation response is missing confirmation")
    return ReconciliationRecoveryConfirmation.from_dict(confirmation)


def verify_reconciliation_recovery(
    *,
    candidate_payload: Mapping[str, Any],
    source_receipt_records: Sequence[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
    dual_review_payload: Mapping[str, Any] | None = None,
    confirmation_payload: Mapping[str, Any],
    current_receipt_payload: Mapping[str, Any],
    runtime_target_payload: Mapping[str, Any],
    recovery_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a non-executable recovery verification result.

    The caller owns how the fresh broker receipt and deployed target are read;
    this function intentionally has no broker, Cloud Run, or state-write port.
    """

    candidate = extract_baseline_candidate(candidate_payload, source_receipt_records=source_receipt_records, expectations=expectations)
    # Validate any supplied advisory receipt without treating model votes as
    # source verification or authority. Missing review never fabricates approval.
    extract_bound_dual_review(dual_review_payload, candidate=candidate)
    confirmation = _verify_console_response(
        confirmation_payload,
        recovery_id=recovery_id,
        candidate=candidate,
    )
    current_evidence = extract_reconciliation_evidence(current_receipt_payload)
    continuity_state, target_findings = _runtime_target_state(runtime_target_payload, candidate=candidate)
    evaluation = evaluate_reconciliation_recovery_activation(
        recovery_id=recovery_id,
        candidate=candidate,
        confirmation=confirmation,
        current_evidence=current_evidence,
        current_live_continuity_state=continuity_state,
        now=now or datetime.now(timezone.utc),
    )
    findings = tuple(dict.fromkeys((*target_findings, *evaluation.findings)))
    plan = evaluation.transition_plan if not findings else None
    return {
        "schema_version": VERIFY_ONLY_SCHEMA_VERSION,
        "recovery_id": recovery_id,
        "candidate_sha256": candidate.candidate_sha256,
        "confirmation_sha256": confirmation.confirmation_sha256,
        "ready_for_atomic_state_transition": not findings and plan is not None,
        "findings": list(findings),
        "transition_plan": plan.to_dict() if plan is not None else None,
        "policy": {
            "controller_mode": "verify_only",
            "no_order": True,
            "execution_authority_granted": False,
            "state_write_attempted": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a confirmed IBKR legacy recovery without changing broker or runtime state."
    )
    parser.add_argument("--candidate", type=Path, required=True, help="Private QPK baseline-candidate output")
    parser.add_argument("--dual-review", type=Path, help="Optional private advisory review result")
    parser.add_argument("--source-records", type=Path, required=True)
    parser.add_argument("--source-expectations", type=Path, required=True)
    parser.add_argument("--current-receipt", type=Path, required=True, help="Fresh post-confirmation private /reconcile receipt")
    parser.add_argument("--runtime-target", type=Path, required=True, help="Read-only deployed RUNTIME_TARGET_JSON snapshot")
    parser.add_argument("--confirmation-url", required=True, help="QRS private controller-read HTTPS endpoint")
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--now", help="Optional ISO-8601 time used for deterministic validation")
    args = parser.parse_args(argv)
    try:
        records, expectations = load_source_inputs(args.source_records, args.source_expectations)
        confirmation = read_console_confirmation(
            confirmation_url=args.confirmation_url,
            recovery_id=args.recovery_id,
            token=os.environ.get(RECONCILIATION_RECOVERY_CONTROLLER_TOKEN_ENV, ""),
        )
        result = verify_reconciliation_recovery(
            candidate_payload=_load_json(args.candidate, label="baseline candidate"),
            dual_review_payload=_load_json(args.dual_review, label="dual review") if args.dual_review else None,
            source_receipt_records=records, expectations=expectations,
            confirmation_payload=confirmation,
            current_receipt_payload=_load_json(args.current_receipt, label="current reconciliation receipt"),
            runtime_target_payload=_load_json(args.runtime_target, label="runtime target"),
            recovery_id=args.recovery_id,
            now=_parse_time(args.now),
        )
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready_for_atomic_state_transition"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
