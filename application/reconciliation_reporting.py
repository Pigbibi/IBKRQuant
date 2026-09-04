"""Safe persistence boundary for read-only IBKR reconciliation reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_CANDIDATE_FIELDS = (
    "schema_version",
    "permits_active_lkg",
    "expected_digests_configured",
    "execution_ledger_records_count",
    "recovery_blockers",
)
_EVIDENCE_FIELDS = (
    "schema_version",
    "platform_id",
    "strategy_profile",
    "account_scope_sha256",
    "baseline_id",
    "baseline_target_sha256",
    "runtime_target_sha256",
    "observed_at",
    "broker_connected",
    "account_identity_match",
    "positions_match",
    "cash_match",
    "open_orders_match",
    "recent_executions_match",
    "local_execution_ledger_match",
    "positions_sha256",
    "cash_sha256",
    "open_orders_sha256",
    "recent_executions_sha256",
    "local_execution_ledger_sha256",
    "evidence_sha256",
)
_SUMMARY_FIELDS = (
    "broker_reconciliation_permits_active_lkg",
    "broker_reconciliation_blockers_count",
    "broker_reconciliation_ledger_records_count",
)
_ERROR_FIELDS = ("stage", "error_type", "failure_category")
_REPORT_IDENTITY_FIELDS = (
    "schema_version",
    "platform",
    "deploy_target",
    "strategy_profile",
    "run_id",
    "run_source",
    "status",
    "started_at",
    "finished_at",
)


def _selected_mapping(value: object, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {field: value[field] for field in fields if field in value}


def _safe_candidate(value: object) -> dict[str, Any]:
    candidate = _selected_mapping(value, _CANDIDATE_FIELDS)
    if not candidate:
        return {}
    evidence = _selected_mapping(value.get("evidence") if isinstance(value, Mapping) else None, _EVIDENCE_FIELDS)
    if evidence:
        candidate["evidence"] = evidence
    return candidate


def build_persistable_reconciliation_report(report: Mapping[str, object]) -> dict[str, Any]:
    """Return the schema-compatible, redacted subset allowed for persistence.

    The read-only route owns this boundary.  It intentionally excludes runtime
    target/configuration, account scope, diagnostics, artifact input paths, and
    exception messages while retaining the public reconciliation receipt.
    """

    safe_report = _selected_mapping(report, _REPORT_IDENTITY_FIELDS)
    safe_report["dry_run"] = True
    safe_report["summary"] = _selected_mapping(report.get("summary"), _SUMMARY_FIELDS)
    safe_report["diagnostics"] = {}
    candidate = _safe_candidate(
        (report.get("diagnostics") or {}).get("broker_reconciliation")
        if isinstance(report.get("diagnostics"), Mapping)
        else None
    )
    if candidate:
        safe_report["diagnostics"]["broker_reconciliation"] = candidate
    failure = _selected_mapping(
        (report.get("diagnostics") or {}).get("broker_reconciliation_failure")
        if isinstance(report.get("diagnostics"), Mapping)
        else None,
        ("error_type",),
    )
    if failure:
        safe_report["diagnostics"]["broker_reconciliation_failure"] = failure["error_type"]
    safe_report["artifacts"] = {}
    safe_report["errors"] = [
        _selected_mapping(error, _ERROR_FIELDS)
        for error in (report.get("errors") or ())
        if isinstance(error, Mapping)
    ]
    return safe_report
