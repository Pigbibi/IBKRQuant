from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.build_reconciliation_baseline_candidate import (
    evaluate_receipts,
    extract_reconciliation_evidence,
)
from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence


def _digest(character: str) -> str:
    return character * 64


def _payload(*, observed_at: datetime, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "account_scope_sha256": _digest("a"),
        "baseline_id": "ibkr-soxl-lkg-20260830",
        "baseline_target_sha256": _digest("b"),
        "runtime_target_sha256": _digest("b"),
        "observed_at": observed_at,
        "broker_connected": True,
        "account_identity_match": True,
        "positions_match": False,
        "cash_match": False,
        "open_orders_match": False,
        "recent_executions_match": False,
        "local_execution_ledger_match": False,
        "positions_sha256": _digest("c"),
        "cash_sha256": _digest("d"),
        "open_orders_sha256": _digest("e"),
        "recent_executions_sha256": _digest("f"),
        "local_execution_ledger_sha256": _digest("0"),
    }
    values.update(overrides)
    return {
        "schema_version": "ibkr_reconciliation_candidate.v1",
        "evidence": build_broker_reconciliation_evidence(**values).to_dict(),
    }



def _sources_for_evidences(evidences):
    """Synthetic independently designated records, never broker observations."""
    from dataclasses import asdict
    from application.broker_reconciliation_candidate import SourceReceiptExpectation, SOURCE_RECEIPT_RECORD_SCHEMA_VERSION
    expectations = []
    records = []
    for index, evidence in enumerate(evidences, start=1):
        expectation = SourceReceiptExpectation(
            strategy_profile=evidence.strategy_profile,
            repository="QuantStrategyLab/InteractiveBrokersPlatform",
            workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
            workflow_ref="refs/heads/main", workflow_conclusion="success",
            workflow_run_id=str(index), workflow_run_attempt="1", workflow_head_sha="c" * 40,
            artifact_id=str(index), artifact_name=f"ibkr-reconciliation-{evidence.strategy_profile}-{index}",
            artifact_sha256="d" * 64, evidence_sha256=evidence.evidence_sha256,
            service_name="synthetic-service", service_revision="synthetic-revision",
            service_revision_commit_sha="e" * 40, service_deploy_run_id="100",
        )
        expectations.append(expectation)
        record = asdict(expectation)
        for key in ("strategy_profile", "workflow_ref", "workflow_conclusion"):
            record.pop(key)
        records.append({"schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION, **record})
    return {"source_receipt_records": records, "expectations": tuple(expectations)}


def test_builds_redacted_review_candidate_from_two_matching_receipts() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    reconciled = dict.fromkeys(("positions_match", "cash_match", "open_orders_match", "recent_executions_match", "local_execution_ledger_match"), True)
    payloads = [_payload(observed_at=start, **reconciled), _payload(observed_at=start + timedelta(minutes=2), **reconciled)]
    result = evaluate_receipts(payloads, now=start + timedelta(minutes=3),
                              **_sources_for_evidences([extract_reconciliation_evidence(item) for item in payloads]))

    assert result["ready_for_independent_review"] is True
    assert result["findings"] == []
    candidate = result["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["candidate_sha256"]
    assert "account_scope" not in candidate


def test_rejects_mismatched_receipts_without_authorisation() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    payloads = [_payload(observed_at=start), _payload(observed_at=start + timedelta(minutes=2), cash_sha256=_digest("9"))]
    result = evaluate_receipts(payloads, now=start + timedelta(minutes=3),
                              **_sources_for_evidences([extract_reconciliation_evidence(item) for item in payloads]))

    assert result == {
        "schema_version": "ibkr_reconciliation_baseline_enrollment.v1",
        "ready_for_independent_review": False,
        "findings": ["broker_reconciliation_enrollment_observation_mismatch"],
    }


def test_accepts_persisted_runtime_report_shape() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    payload = {"diagnostics": {"broker_reconciliation": _payload(observed_at=start)}}

    evidence = extract_reconciliation_evidence(payload)

    assert evidence.platform_id == "ibkr"


def test_accepts_redacted_workflow_artifact_shape() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    payload = {
        "schema_version": "ibkr_reconciliation_artifact.v1",
        "strategy_profile": "soxl_soxx_trend_income",
        "reconciliation": _payload(observed_at=start),
    }

    evidence = extract_reconciliation_evidence(payload)

    assert evidence.platform_id == "ibkr"


def test_missing_redacted_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not contain"):
        extract_reconciliation_evidence({"diagnostics": {}})
