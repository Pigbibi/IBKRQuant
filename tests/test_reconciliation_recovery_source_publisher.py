from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
from tests.test_reconciliation_baseline_candidate_script import _sources_for_evidences
from tests.test_reconciliation_recovery_verify_only import _candidate_source_inputs
from scripts.publish_reconciliation_recovery_source import (
    archive_private_evidence_package,
    build_private_evidence_package,
    build_recovery_source_snapshot,
    publish_recovery_source_snapshot,
)


def _digest(character: str) -> str:
    return character * 64


def _candidate_payload(start: datetime) -> dict[str, object]:
    base = {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "account_scope_sha256": _digest("a"),
        "baseline_id": "soxl-ibkr-lkg-20260830",
        "baseline_target_sha256": _digest("b"),
        "runtime_target_sha256": _digest("b"),
        "broker_connected": True,
        "account_identity_match": True,
        "positions_match": True,
        "cash_match": True,
        "open_orders_match": True,
        "recent_executions_match": True,
        "local_execution_ledger_match": True,
        "positions_sha256": _digest("c"),
        "cash_sha256": _digest("d"),
        "open_orders_sha256": _digest("e"),
        "recent_executions_sha256": _digest("f"),
        "local_execution_ledger_sha256": _digest("0"),
    }
    observations = [
        build_broker_reconciliation_evidence(**base, observed_at=start),
        build_broker_reconciliation_evidence(**base, observed_at=start + timedelta(minutes=2)),
    ]
    return evaluate_receipts(
        [{"evidence": item.to_dict()} for item in observations], now=start + timedelta(minutes=3),
        **_sources_for_evidences(observations),
    )


def _dual_review(candidate_sha256: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "trigger": "reconciliation_baseline",
        "strategy_profile": "soxl_soxx_trend_income",
        "primary_review": {"verdict": "approve", "confidence": 0.99},
        "secondary_review": {
            "mode": "dual_api",
            "gpt": {"verdict": "approve", "confidence": 0.98},
            "claude": {"verdict": "approve", "confidence": 0.98},
        },
        "escalated": True,
        "outcome": "pass",
        "evidence_binding_sha256": candidate_sha256,
        "requires_human_recovery_approval": True,
        "recovery_authority": {"human_review_required": True, "final_action": "escalate"},
    }
    result.update(overrides)
    return result


def test_builds_redacted_source_only_from_bound_mandatory_dual_review() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    snapshot = build_recovery_source_snapshot(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(str(candidate_value["candidate_sha256"])),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=3),
    )

    recoveries = snapshot["recoveries"]
    assert isinstance(recoveries, list)
    record = recoveries[0]
    assert snapshot["schema_version"] == "qsl_reconciliation_recovery_source_snapshot.v1"
    assert record["readiness"] == "awaiting_human_confirmation"
    assert record["dual_review"] == {
        "outcome": "approved",
        "reviewer_count": 3,
        "evidence_binding_sha256": candidate_value["candidate_sha256"],
    }
    assert "positions_sha256" not in record
    assert "account_scope_sha256" not in record


def test_rejects_audit_receipt_without_human_recovery_boundary() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    review = _dual_review(str(candidate_value["candidate_sha256"]), requires_human_recovery_approval=False)

    with pytest.raises(ValueError, match="human recovery approval"):
        build_recovery_source_snapshot(
            candidate_payload=candidate,
        **_candidate_source_inputs(start),
            dual_review_payload=review,
            recovery_id="ibkr-soxl-live-recovery",
            now=start + timedelta(minutes=3),
        )


def test_publish_uses_only_recovery_ingress_and_bearer_token() -> None:
    received: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "source_id": "ibkr.reconciliation_recovery"}

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        received["args"] = args
        received["kwargs"] = kwargs
        return FakeResponse()

    result = publish_recovery_source_snapshot(
        {"schema_version": "qsl_reconciliation_recovery_source_snapshot.v1"},
        publish_url="https://console.example/api/internal/sync-reconciliation-recovery-source",
        token="dedicated-token",
        post=fake_post,
    )

    assert result["ok"] is True
    kwargs = received["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["headers"] == {"Authorization": "Bearer dedicated-token"}


def test_publish_rejects_non_recovery_endpoint() -> None:
    with pytest.raises(ValueError, match="reconciliation recovery source ingress"):
        publish_recovery_source_snapshot(
            {},
            publish_url="https://console.example/api/manual-strategy-switch",
            token="dedicated-token",
        )


def test_private_evidence_archive_is_create_only_and_never_uses_console_shape() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    review = _dual_review(str(candidate_value["candidate_sha256"]))
    snapshot = build_recovery_source_snapshot(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=review,
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=3),
    )
    package = build_private_evidence_package(
        snapshot=snapshot,
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=review,
    )
    observed: dict[str, object] = {}

    class Blob:
        def upload_from_string(self, value: str, **kwargs: object) -> None:
            observed["value"] = value
            observed["kwargs"] = kwargs

    class Bucket:
        @staticmethod
        def blob(name: str) -> Blob:
            observed["object_name"] = name
            return Blob()

    class Client:
        @staticmethod
        def bucket(name: str) -> Bucket:
            observed["bucket_name"] = name
            return Bucket()

    result = archive_private_evidence_package(
        package,
        private_evidence_uri="gs://private-bucket/reconciliation-recovery/ibkr/source/ibkr-soxl-live-recovery/evidence.json",
        storage_client_factory=Client,
    )

    assert result["uri"].startswith("gs://private-bucket/reconciliation-recovery/ibkr/source/")
    assert observed["kwargs"] == {"content_type": "application/json", "if_generation_match": 0}
    assert '"baseline_candidate"' in observed["value"]  # type: ignore[operator]


@pytest.mark.parametrize("outcome", ["fail", "review_unavailable", "pass"])
def test_source_publisher_preserves_advisory_outcome_without_two_reviewers(outcome):
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    review = _dual_review(candidate["candidate"]["candidate_sha256"], outcome=outcome, escalated=False, secondary_review={})
    snapshot = build_recovery_source_snapshot(
        candidate_payload=candidate, **_candidate_source_inputs(start), dual_review_payload=review,
        recovery_id="ibkr-soxl-live-recovery", now=start + timedelta(minutes=3),
    )
    record = snapshot["recoveries"][0]
    assert record["readiness"] == "awaiting_human_confirmation"
    assert record["dual_review"]["outcome"] == {"fail": "rejected", "review_unavailable": "unavailable", "pass": "approved"}[outcome]
    assert record["dual_review"]["reviewer_count"] == 1


@pytest.mark.parametrize("consumer", ["publisher", "verifier"])
@pytest.mark.parametrize("fault", ["source_changed", "expectation_changed", "member_substitution", "legacy_v1", "review_binding", "missing_source"])
def test_both_consumers_revalidate_sources_and_candidate_members(consumer, fault):
    from dataclasses import replace
    from application.broker_reconciliation_candidate import calculate_source_receipts_sha256
    from quant_platform_kit.common.broker_reconciliation_enrollment import calculate_broker_reconciliation_baseline_candidate_sha256
    from scripts.verify_reconciliation_recovery import verify_reconciliation_recovery
    from tests.test_reconciliation_recovery_verify_only import _console_response, _evidence, _runtime_target

    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    sources = _candidate_source_inputs(start)
    review = None
    if fault == "source_changed":
        sources["source_receipt_records"][0]["service_revision"] = "unapproved-revision"
    elif fault == "expectation_changed":
        sources["expectations"] = (replace(sources["expectations"][0], workflow_head_sha="f" * 40), *sources["expectations"][1:])
    elif fault == "member_substitution":
        sources["source_receipt_records"][0]["evidence_sha256"] = "9" * 64
        sources["expectations"] = (replace(sources["expectations"][0], evidence_sha256="9" * 64), *sources["expectations"][1:])
        value = candidate["candidate"]
        value["source_receipts_sha256"] = calculate_source_receipts_sha256(sources["source_receipt_records"], strategy_profile=value["strategy_profile"], expectations=sources["expectations"])
        value["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(value)
    elif fault == "legacy_v1":
        value = candidate["candidate"]
        value["schema_version"] = "broker_reconciliation_baseline_candidate.v1"
        del value["source_receipts_sha256"]
        value["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(value)
    elif fault == "review_binding":
        review = _dual_review("9" * 64)
    elif fault == "missing_source":
        sources = {"source_receipt_records": [], "expectations": ()}
    with pytest.raises(ValueError):
        if consumer == "publisher":
            build_recovery_source_snapshot(candidate_payload=candidate, **sources, dual_review_payload=review,
                                           recovery_id="ibkr-soxl-live-recovery", now=start + timedelta(minutes=3))
        else:
            verify_reconciliation_recovery(
                candidate_payload=candidate, **sources, dual_review_payload=review,
                confirmation_payload=_console_response(candidate["candidate"]["candidate_sha256"], confirmed_at=start + timedelta(minutes=3)),
                current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
                runtime_target_payload=_runtime_target(), recovery_id="ibkr-soxl-live-recovery", now=start + timedelta(minutes=5),
            )
