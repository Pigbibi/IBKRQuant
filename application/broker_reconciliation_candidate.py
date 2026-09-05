"""Build a private, provenance-bound reconciliation candidate without I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BROKER_RECONCILIATION_BASELINE_CANDIDATE_SCHEMA_VERSION,
    BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION,
    BrokerReconciliationBaselineCandidate,
    calculate_broker_reconciliation_baseline_candidate_sha256,
)


SOURCE_RECEIPT_RECORD_SCHEMA_VERSION = "ibkr_reconciliation_source_receipt_record.v1"
_SHA256_LENGTH = 64
_GIT_SHA_LENGTH = 40
_MAIN_REF = "refs/heads/main"
_SUCCESSFUL_WORKFLOW_CONCLUSION = "success"
_SOURCE_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "workflow_path",
        "workflow_run_id",
        "workflow_run_attempt",
        "workflow_head_sha",
        "artifact_id",
        "artifact_name",
        "artifact_sha256",
        "evidence_sha256",
        "service_name",
        "service_revision",
        "service_revision_commit_sha",
        "service_deploy_run_id",
    }
)


@dataclass(frozen=True)
class SourceReceiptExpectation:
    """Audit-designated provenance for one saved source record."""

    strategy_profile: str
    repository: str
    workflow_path: str
    workflow_ref: str
    workflow_conclusion: str
    workflow_run_id: str
    workflow_run_attempt: str
    workflow_head_sha: str
    artifact_id: str
    artifact_name: str
    artifact_sha256: str
    evidence_sha256: str
    service_name: str
    service_revision: str
    service_revision_commit_sha: str
    service_deploy_run_id: str


def _canonical_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty canonical string")
    return value


def _canonical_sha256(value: object, *, field_name: str) -> str:
    text = _canonical_text(value, field_name=field_name)
    if len(text) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return text


def _canonical_git_sha(value: object, *, field_name: str) -> str:
    text = _canonical_text(value, field_name=field_name)
    if len(text) != _GIT_SHA_LENGTH or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise ValueError(f"{field_name} must be a lowercase Git SHA")
    return text


def _canonical_repository(value: object) -> str:
    repository = _canonical_text(value, field_name="repository")
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository must be an owner/name identifier")
    return repository


def _canonical_workflow_path(value: object) -> str:
    workflow_path = _canonical_text(value, field_name="workflow_path")
    if (
        not workflow_path.startswith(".github/workflows/")
        or not workflow_path.endswith((".yml", ".yaml"))
        or ".." in workflow_path
    ):
        raise ValueError("workflow_path must be a canonical GitHub workflow path")
    return workflow_path


def _normalize_expectations(
    expectations: Sequence[SourceReceiptExpectation],
) -> dict[str, dict[str, str]]:
    if not expectations:
        raise ValueError("at least one designated source receipt is required")
    normalized: dict[str, dict[str, str]] = {}
    for expectation in expectations:
        if type(expectation) is not SourceReceiptExpectation:
            raise TypeError("expectations must contain SourceReceiptExpectation values")
        if expectation.workflow_ref != _MAIN_REF:
            raise ValueError("source receipt workflow_ref must be refs/heads/main")
        if expectation.workflow_conclusion != _SUCCESSFUL_WORKFLOW_CONCLUSION:
            raise ValueError("source receipt workflow_conclusion must be success")
        normalized_expectation = {
            "strategy_profile": _canonical_text(
                expectation.strategy_profile, field_name="strategy_profile"
            ),
            "repository": _canonical_repository(expectation.repository),
            "workflow_path": _canonical_workflow_path(expectation.workflow_path),
            "workflow_run_id": _canonical_text(
                expectation.workflow_run_id, field_name="workflow_run_id"
            ),
            "workflow_run_attempt": _canonical_text(
                expectation.workflow_run_attempt, field_name="workflow_run_attempt"
            ),
            "workflow_head_sha": _canonical_git_sha(
                expectation.workflow_head_sha, field_name="workflow_head_sha"
            ),
            "artifact_id": _canonical_text(
                expectation.artifact_id, field_name="artifact_id"
            ),
            "artifact_name": _canonical_text(
                expectation.artifact_name, field_name="artifact_name"
            ),
            "artifact_sha256": _canonical_sha256(
                expectation.artifact_sha256, field_name="artifact_sha256"
            ),
            "evidence_sha256": _canonical_sha256(
                expectation.evidence_sha256, field_name="evidence_sha256"
            ),
            "service_name": _canonical_text(
                expectation.service_name, field_name="service_name"
            ),
            "service_revision": _canonical_text(
                expectation.service_revision, field_name="service_revision"
            ),
            "service_revision_commit_sha": _canonical_git_sha(
                expectation.service_revision_commit_sha,
                field_name="service_revision_commit_sha",
            ),
            "service_deploy_run_id": _canonical_text(
                expectation.service_deploy_run_id, field_name="service_deploy_run_id"
            ),
        }
        if normalized_expectation["artifact_name"] != (
            "ibkr-reconciliation-"
            f"{normalized_expectation['strategy_profile']}-"
            f"{normalized_expectation['workflow_run_id']}"
        ):
            raise ValueError("source receipt artifact_name does not match the workflow")
        artifact_id = normalized_expectation["artifact_id"]
        if artifact_id in normalized:
            raise ValueError("audit-designated artifact IDs must be unique")
        normalized[artifact_id] = normalized_expectation
    if len({value["workflow_run_id"] for value in normalized.values()}) != len(
        normalized
    ):
        raise ValueError("audit-designated workflow run IDs must be unique")
    for field_name in (
        "strategy_profile",
        "repository",
        "workflow_path",
        "workflow_head_sha",
        "service_name",
        "service_revision",
        "service_revision_commit_sha",
    ):
        if len({value[field_name] for value in normalized.values()}) != 1:
            raise ValueError(f"audit-designated source receipts must share {field_name}")
    return normalized


def _normalize_source_record(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_RECORD_FIELDS:
        raise ValueError("source receipt record has invalid fields")
    if value["schema_version"] != SOURCE_RECEIPT_RECORD_SCHEMA_VERSION:
        raise ValueError("unsupported source receipt record schema version")
    return {
        "schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
        "repository": _canonical_repository(value["repository"]),
        "workflow_path": _canonical_workflow_path(value["workflow_path"]),
        "workflow_run_id": _canonical_text(
            value["workflow_run_id"], field_name="workflow_run_id"
        ),
        "workflow_run_attempt": _canonical_text(
            value["workflow_run_attempt"], field_name="workflow_run_attempt"
        ),
        "workflow_head_sha": _canonical_git_sha(
            value["workflow_head_sha"], field_name="workflow_head_sha"
        ),
        "artifact_id": _canonical_text(value["artifact_id"], field_name="artifact_id"),
        "artifact_name": _canonical_text(
            value["artifact_name"], field_name="artifact_name"
        ),
        "artifact_sha256": _canonical_sha256(
            value["artifact_sha256"], field_name="artifact_sha256"
        ),
        "evidence_sha256": _canonical_sha256(
            value["evidence_sha256"], field_name="evidence_sha256"
        ),
        "service_name": _canonical_text(
            value["service_name"], field_name="service_name"
        ),
        "service_revision": _canonical_text(
            value["service_revision"], field_name="service_revision"
        ),
        "service_revision_commit_sha": _canonical_git_sha(
            value["service_revision_commit_sha"],
            field_name="service_revision_commit_sha",
        ),
        "service_deploy_run_id": _canonical_text(
            value["service_deploy_run_id"], field_name="service_deploy_run_id"
        ),
    }


def _source_record_key(record: Mapping[str, str]) -> tuple[str, str]:
    return record["artifact_id"], record["workflow_run_id"]


def canonical_source_receipt_records_json(
    records: Iterable[Mapping[str, object]],
    *,
    strategy_profile: str,
    expectations: Sequence[SourceReceiptExpectation],
) -> str:
    """Validate and canonically serialize designated redacted source records."""

    profile = _canonical_text(strategy_profile, field_name="strategy_profile")
    expected_by_artifact = _normalize_expectations(expectations)
    normalized = tuple(_normalize_source_record(record) for record in records)
    if not normalized:
        raise ValueError("at least one source receipt record is required")
    if len({_source_record_key(record) for record in normalized}) != len(normalized):
        raise ValueError("source receipt artifact/run pairs must be unique")
    if {record["artifact_id"] for record in normalized} != set(expected_by_artifact):
        raise ValueError("source receipt artifact IDs must match the audit designation")
    for record in normalized:
        expected = expected_by_artifact[record["artifact_id"]]
        if expected["strategy_profile"] != profile:
            raise ValueError("source receipt strategy_profile must match the candidate")
        if any(
            record[field_name] != expected[field_name]
            for field_name in _SOURCE_RECORD_FIELDS - {"schema_version"}
        ):
            raise ValueError("source receipt provenance does not match the audit designation")
    try:
        return json.dumps(
            sorted(normalized, key=_source_record_key),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("source receipt records cannot be canonicalized") from exc


def calculate_source_receipts_sha256(
    records: Iterable[Mapping[str, object]],
    *,
    strategy_profile: str,
    expectations: Sequence[SourceReceiptExpectation],
) -> str:
    """Bind validated source content; the caller must independently verify its origin."""

    return hashlib.sha256(
        canonical_source_receipt_records_json(
            records,
            strategy_profile=strategy_profile,
            expectations=expectations,
        ).encode("utf-8")
    ).hexdigest()


def build_reconciliation_candidate_v2(
    candidate: BrokerReconciliationBaselineCandidate | Mapping[str, object],
    *,
    source_receipt_records: Iterable[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
) -> BrokerReconciliationBaselineCandidate:
    """Bind designated redacted source records to one existing v1 candidate.

    The caller owns record retrieval and storage. This pure function neither
    resolves GitHub artifacts nor contacts Cloud Run, a broker, or any account.
    """

    normalized_candidate = (
        candidate
        if isinstance(candidate, BrokerReconciliationBaselineCandidate)
        else BrokerReconciliationBaselineCandidate.from_dict(candidate)
    )
    if (
        normalized_candidate.schema_version
        != BROKER_RECONCILIATION_BASELINE_CANDIDATE_SCHEMA_VERSION
    ):
        raise ValueError("only a v1 reconciliation candidate can be upgraded to v2")
    records = tuple(source_receipt_records)
    source_receipts_sha256 = calculate_source_receipts_sha256(
        records,
        strategy_profile=normalized_candidate.strategy_profile,
        expectations=expectations,
    )
    _validate_evidence_members(normalized_candidate, records)
    payload: dict[str, Any] = normalized_candidate.to_dict()
    payload["schema_version"] = (
        BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION
    )
    payload["source_receipts_sha256"] = source_receipts_sha256
    payload["candidate_sha256"] = "0" * _SHA256_LENGTH
    payload["candidate_sha256"] = (
        calculate_broker_reconciliation_baseline_candidate_sha256(payload)
    )
    return BrokerReconciliationBaselineCandidate.from_dict(payload)


def _validate_evidence_members(
    candidate: BrokerReconciliationBaselineCandidate,
    records: Sequence[Mapping[str, object]],
) -> None:
    members = [record.get("evidence_sha256") for record in records]
    if sorted(members) != sorted(candidate.source_evidence_sha256):
        raise ValueError("source records must match candidate evidence members exactly")


def validate_reconciliation_candidate_sources(
    candidate: BrokerReconciliationBaselineCandidate,
    *,
    source_receipt_records: Iterable[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
) -> BrokerReconciliationBaselineCandidate:
    """Recheck private source content against independently designated expectations.

    These inputs bind provenance, not accounting completeness or live authority.
    The caller must obtain expectations independently of untrusted candidate data.
    """
    candidate = BrokerReconciliationBaselineCandidate.from_dict(candidate.to_dict())
    if candidate.schema_version != BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION:
        raise ValueError("a source-bound v2 candidate is required")
    records = tuple(source_receipt_records)
    root = calculate_source_receipts_sha256(
        records, strategy_profile=candidate.strategy_profile, expectations=expectations,
    )
    _validate_evidence_members(candidate, records)
    if root != candidate.source_receipts_sha256:
        raise ValueError("candidate source receipts binding mismatch")
    return candidate


__all__ = [
    "validate_reconciliation_candidate_sources",
    "SOURCE_RECEIPT_RECORD_SCHEMA_VERSION",
    "SourceReceiptExpectation",
    "build_reconciliation_candidate_v2",
    "calculate_source_receipts_sha256",
    "canonical_source_receipt_records_json",
]
