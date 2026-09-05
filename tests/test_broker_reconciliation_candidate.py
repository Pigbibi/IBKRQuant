from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from application.broker_reconciliation_candidate import (
    SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
    SourceReceiptExpectation,
    build_reconciliation_candidate_v2,
    calculate_source_receipts_sha256,
    canonical_source_receipt_records_json,
)
from quant_platform_kit.common.broker_reconciliation import (
    build_broker_reconciliation_evidence,
)
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION,
    evaluate_broker_reconciliation_baseline_enrollment,
)


_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_HEAD_SHA = "c" * 40
_SERVICE_COMMIT_SHA = "d" * 40


def _candidate():
    observed_at = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    shared = {
        "platform_id": "ibkr",
        "strategy_profile": "global_etf_rotation",
        "account_scope_sha256": _DIGEST,
        "baseline_id": "legacy-live-baseline",
        "baseline_target_sha256": _DIGEST,
        "runtime_target_sha256": _DIGEST,
        "positions_sha256": _DIGEST,
        "cash_sha256": _DIGEST,
        "open_orders_sha256": _DIGEST,
        "recent_executions_sha256": _DIGEST,
        "local_execution_ledger_sha256": _DIGEST,
        "broker_connected": True,
        "account_identity_match": True,
        "positions_match": True,
        "cash_match": True,
        "open_orders_match": True,
        "recent_executions_match": True,
        "local_execution_ledger_match": True,
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (
            build_broker_reconciliation_evidence(observed_at=observed_at, **shared),
            build_broker_reconciliation_evidence(
                observed_at=observed_at + timedelta(minutes=2), **shared
            ),
        ),
        now=observed_at + timedelta(minutes=3),
    )
    assert evaluation.candidate is not None
    return evaluation.candidate


def _expectations() -> tuple[SourceReceiptExpectation, ...]:
    return (
        SourceReceiptExpectation(
            strategy_profile="global_etf_rotation",
            repository="QuantStrategyLab/InteractiveBrokersPlatform",
            workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
            workflow_ref="refs/heads/main",
            workflow_conclusion="success",
            workflow_run_id="10",
            workflow_run_attempt="1",
            workflow_head_sha=_HEAD_SHA,
            artifact_id="100",
            artifact_name="ibkr-reconciliation-global_etf_rotation-10",
            artifact_sha256=_DIGEST,
            evidence_sha256=_candidate().source_evidence_sha256[0],
            service_name="interactive-brokers-platform",
            service_revision="ibkr-service-00002",
            service_revision_commit_sha=_SERVICE_COMMIT_SHA,
            service_deploy_run_id="deploy-10",
        ),
        SourceReceiptExpectation(
            strategy_profile="global_etf_rotation",
            repository="QuantStrategyLab/InteractiveBrokersPlatform",
            workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
            workflow_ref="refs/heads/main",
            workflow_conclusion="success",
            workflow_run_id="20",
            workflow_run_attempt="1",
            workflow_head_sha=_HEAD_SHA,
            artifact_id="200",
            artifact_name="ibkr-reconciliation-global_etf_rotation-20",
            artifact_sha256=_OTHER_DIGEST,
            evidence_sha256=_candidate().source_evidence_sha256[1],
            service_name="interactive-brokers-platform",
            service_revision="ibkr-service-00002",
            service_revision_commit_sha=_SERVICE_COMMIT_SHA,
            service_deploy_run_id="deploy-20",
        ),
    )


def _records() -> tuple[dict[str, str], ...]:
    return (
        {
            "schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
            "repository": "QuantStrategyLab/InteractiveBrokersPlatform",
            "workflow_path": ".github/workflows/collect-reconciliation-evidence.yml",
            "workflow_run_id": "20",
            "workflow_run_attempt": "1",
            "workflow_head_sha": _HEAD_SHA,
            "artifact_id": "200",
            "artifact_name": "ibkr-reconciliation-global_etf_rotation-20",
            "artifact_sha256": _OTHER_DIGEST,
            "evidence_sha256": _candidate().source_evidence_sha256[1],
            "service_name": "interactive-brokers-platform",
            "service_revision": "ibkr-service-00002",
            "service_revision_commit_sha": _SERVICE_COMMIT_SHA,
            "service_deploy_run_id": "deploy-20",
        },
        {
            "schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
            "repository": "QuantStrategyLab/InteractiveBrokersPlatform",
            "workflow_path": ".github/workflows/collect-reconciliation-evidence.yml",
            "workflow_run_id": "10",
            "workflow_run_attempt": "1",
            "workflow_head_sha": _HEAD_SHA,
            "artifact_id": "100",
            "artifact_name": "ibkr-reconciliation-global_etf_rotation-10",
            "artifact_sha256": _DIGEST,
            "evidence_sha256": _candidate().source_evidence_sha256[0],
            "service_name": "interactive-brokers-platform",
            "service_revision": "ibkr-service-00002",
            "service_revision_commit_sha": _SERVICE_COMMIT_SHA,
            "service_deploy_run_id": "deploy-10",
        },
    )


def test_builds_v2_candidate_from_exactly_two_designated_redacted_source_records() -> (
    None
):
    candidate = _candidate()
    records = _records()

    result = build_reconciliation_candidate_v2(
        candidate, source_receipt_records=records, expectations=_expectations()
    )

    assert (
        result.schema_version
        == BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION
    )
    assert result.source_receipts_sha256 == calculate_source_receipts_sha256(
        tuple(reversed(records)),
        strategy_profile=candidate.strategy_profile,
        expectations=_expectations(),
    )
    assert result.candidate_sha256 != candidate.candidate_sha256
    assert set(json.loads(canonical_source_receipt_records_json(
        records,
        strategy_profile=candidate.strategy_profile,
        expectations=_expectations(),
    ))[0]) == set(records[0])


@pytest.mark.parametrize(
    ("records", "message"),
    (
        (_records()[:1], "artifact IDs must match"),
        ((_records()[0], _records()[0]), "artifact/run pairs must be unique"),
        (({**_records()[0], "unexpected": "field"}, _records()[1]), "invalid fields"),
    ),
)
def test_source_records_fail_closed_on_invalid_count_uniqueness_or_schema(
    records, message
) -> None:
    with pytest.raises(ValueError, match=message):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=records, expectations=_expectations()
        )


def test_source_records_fail_closed_when_profile_or_source_correspondence_mismatches() -> (
    None
):
    wrong_run, valid = _records()
    wrong_run = {**wrong_run, "workflow_run_id": "99"}
    wrong_profile_expectations = (
        replace(
            _expectations()[0],
            strategy_profile="other_profile",
            artifact_name="ibkr-reconciliation-other_profile-10",
        ),
        replace(
            _expectations()[1],
            strategy_profile="other_profile",
            artifact_name="ibkr-reconciliation-other_profile-20",
        ),
    )

    with pytest.raises(ValueError, match="strategy_profile must match"):
        build_reconciliation_candidate_v2(
            _candidate(),
            source_receipt_records=_records(),
            expectations=wrong_profile_expectations,
        )
    with pytest.raises(ValueError, match="provenance does not match"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=(wrong_run, valid), expectations=_expectations()
        )


def test_source_records_fail_closed_when_service_revision_provenance_mismatches() -> (
    None
):
    tampered, valid = _records()
    tampered = {**tampered, "service_revision_commit_sha": _HEAD_SHA}

    with pytest.raises(ValueError, match="provenance does not match"):
        build_reconciliation_candidate_v2(
            _candidate(),
            source_receipt_records=(tampered, valid),
            expectations=_expectations(),
        )


def test_source_records_fail_closed_when_frozen_source_record_field_is_missing() -> (
    None
):
    incomplete, valid = _records()
    incomplete = {key: value for key, value in incomplete.items() if key != "repository"}

    with pytest.raises(ValueError, match="invalid fields"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=(incomplete, valid), expectations=_expectations()
        )


def test_source_record_expectations_require_main_success_and_workflow_artifact_name() -> (
    None
):
    non_main = replace(_expectations()[0], workflow_ref="refs/heads/feature")
    unsuccessful = replace(_expectations()[0], workflow_conclusion="failure")
    malformed_name = replace(_expectations()[1], artifact_name="unexpected")

    with pytest.raises(ValueError, match="workflow_ref"):
        calculate_source_receipts_sha256(
            _records(), strategy_profile="global_etf_rotation", expectations=(non_main, _expectations()[1])
        )
    with pytest.raises(ValueError, match="workflow_conclusion"):
        calculate_source_receipts_sha256(
            _records(),
            strategy_profile="global_etf_rotation",
            expectations=(unsuccessful, _expectations()[1]),
        )
    with pytest.raises(ValueError, match="artifact_name"):
        calculate_source_receipts_sha256(
            _records(), strategy_profile="global_etf_rotation", expectations=(_expectations()[0], malformed_name)
        )


def test_source_receipts_root_covers_every_frozen_source_record_field() -> None:
    records = _records()
    baseline_root = calculate_source_receipts_sha256(
        records, strategy_profile="global_etf_rotation", expectations=_expectations()
    )
    updated_record = {**records[0], "evidence_sha256": "f" * 64}
    updated_expectation = replace(_expectations()[1], evidence_sha256="f" * 64)

    assert baseline_root != calculate_source_receipts_sha256(
        (updated_record, records[1]),
        strategy_profile="global_etf_rotation",
        expectations=(_expectations()[0], updated_expectation),
    )


def test_v2_builder_rejects_existing_v2_candidate_and_tampered_provenance_root() -> (
    None
):
    candidate = _candidate()
    upgraded = build_reconciliation_candidate_v2(
        candidate, source_receipt_records=_records(), expectations=_expectations()
    )

    with pytest.raises(ValueError, match="only a v1"):
        build_reconciliation_candidate_v2(
            upgraded, source_receipt_records=_records(), expectations=_expectations()
        )

    tampered = upgraded.to_dict()
    tampered["source_receipts_sha256"] = _OTHER_DIGEST
    with pytest.raises(ValueError, match="candidate_sha256 mismatch"):
        type(upgraded).from_dict(tampered)


def test_one_designated_source_record_does_not_require_repeated_collection():
    expectation = _expectations()[0]
    record = next(item for item in _records() if item["artifact_id"] == expectation.artifact_id)
    root = calculate_source_receipts_sha256(
        [record], strategy_profile=expectation.strategy_profile, expectations=[expectation],
    )
    assert len(root) == 64


def test_source_records_must_bind_actual_candidate_evidence_members():
    # Well-formed provenance must still bind the actual candidate members.
    records = tuple({**item, "evidence_sha256": _OTHER_DIGEST} for item in _records())
    expectations = tuple(replace(item, evidence_sha256=_OTHER_DIGEST) for item in _expectations())
    with pytest.raises(ValueError, match="evidence"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=records, expectations=expectations,
        )
