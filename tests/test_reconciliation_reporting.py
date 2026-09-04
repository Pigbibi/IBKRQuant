from __future__ import annotations

from datetime import datetime, timezone
import json

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from quant_platform_kit.common.runtime_reports import persist_runtime_report

from application.reconciliation_reporting import build_persistable_reconciliation_report


def _digest(character: str) -> str:
    return character * 64


def _safe_candidate() -> dict[str, object]:
    evidence = build_broker_reconciliation_evidence(
        platform_id="ibkr",
        strategy_profile="global_etf_rotation",
        account_scope_sha256=_digest("a"),
        baseline_id="baseline-001",
        baseline_target_sha256=_digest("b"),
        runtime_target_sha256=_digest("b"),
        observed_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        broker_connected=True,
        account_identity_match=False,
        positions_match=False,
        cash_match=False,
        open_orders_match=False,
        recent_executions_match=False,
        local_execution_ledger_match=False,
        positions_sha256=_digest("c"),
        cash_sha256=_digest("d"),
        open_orders_sha256=_digest("e"),
        recent_executions_sha256=_digest("f"),
        local_execution_ledger_sha256=_digest("0"),
    ).to_dict()
    return {
        "schema_version": "ibkr_reconciliation_candidate.v1",
        "permits_active_lkg": False,
        "expected_digests_configured": False,
        "execution_ledger_records_count": 0,
        "recovery_blockers": ["missing_expected_digests"],
        "evidence": evidence,
    }


def test_persistable_reconciliation_report_keeps_only_safe_evidence(tmp_path) -> None:
    marker = "gateway.example.internal:4999 client_id=72 account=demo-account /private/config.json"
    candidate = _safe_candidate()
    report = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "deploy_target": "cloud_run",
        "service_name": "private-service",
        "strategy_profile": "global_etf_rotation",
        "runtime_target": {"account_selector": ["demo-account"]},
        "account_scope": "demo-account",
        "account_group": "demo-account",
        "project_id": "private-project",
        "instance_name": marker,
        "account_ids": ["demo-account"],
        "run_id": "run-001",
        "run_source": "cloud_run",
        "status": "error",
        "dry_run": True,
        "started_at": "2026-09-04T00:00:00Z",
        "finished_at": "2026-09-04T00:01:00Z",
        "summary": {
            "broker_reconciliation_permits_active_lkg": False,
            "broker_reconciliation_blockers_count": 1,
            "broker_reconciliation_ledger_records_count": 0,
            "account_ids": ["demo-account"],
        },
        "diagnostics": {
            "broker_reconciliation": candidate,
            "reconciliation_request_id": "853a2e08-9396-4fe8-89ee-59fb17e40a1d",
            "reconciliation_scheduler_job_sha256": "a" * 64,
            "ib_gateway_host": marker,
        },
        "artifacts": {"strategy_config_path": marker},
        "errors": [
            {
                "stage": "broker_reconciliation",
                "message": marker,
                "error_type": "TimeoutError",
                "failure_category": "broker_reconciliation",
            }
        ],
    }

    sanitized = build_persistable_reconciliation_report(report)
    result = persist_runtime_report(sanitized, base_dir=tmp_path)
    payload = json.loads((tmp_path / "ibkr/global_etf_rotation/2026-09/run-001.json").read_text())

    assert result.local_path is not None
    assert payload["diagnostics"]["broker_reconciliation"] == candidate
    assert payload["diagnostics"]["reconciliation_request_id"] == "853a2e08-9396-4fe8-89ee-59fb17e40a1d"
    assert payload["diagnostics"]["reconciliation_scheduler_job_sha256"] == "a" * 64
    assert payload["errors"] == [
        {
            "stage": "broker_reconciliation",
            "error_type": "TimeoutError",
            "failure_category": "broker_reconciliation",
        }
    ]
    assert payload["summary"] == {
        "broker_reconciliation_permits_active_lkg": False,
        "broker_reconciliation_blockers_count": 1,
        "broker_reconciliation_ledger_records_count": 0,
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert marker not in serialized
    assert "demo-account" not in serialized
    assert "runtime_target" not in payload
    assert "account_scope" not in payload
    assert "service_name" not in payload


def test_persistable_reconciliation_report_rejects_untrusted_request_id() -> None:
    report = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "deploy_target": "cloud_run",
        "strategy_profile": "global_etf_rotation",
        "run_id": "run-001",
        "run_source": "cloud_run",
        "status": "ok",
        "started_at": "2026-09-04T00:00:00Z",
        "finished_at": "2026-09-04T00:01:00Z",
        "summary": {},
        "diagnostics": {"reconciliation_request_id": "unsafe value\nreport_uri=gs://x"},
        "artifacts": {},
        "errors": [],
    }

    sanitized = build_persistable_reconciliation_report(report)

    assert "reconciliation_request_id" not in sanitized["diagnostics"]
