from __future__ import annotations

import json

from scripts.verify_local_runtime_admission import evaluate_local_runtime_admission


def _all_clear_gates() -> dict[str, bool]:
    return {
        "live_ready": True,
        "release": True,
        "mandate": True,
        "broker_session": True,
        "data_entitlement": True,
        "ledger": True,
        "unknown_pending_orders": False,
        "reconciliation": True,
    }


def test_local_admission_reports_only_fixed_sanitized_gates_when_ready() -> None:
    result = evaluate_local_runtime_admission(_all_clear_gates())

    assert result == {
        "live_ready": "PASS",
        "release": "PASS",
        "mandate": "PASS",
        "broker_session": "PASS",
        "data_entitlement": "PASS",
        "ledger": "PASS",
        "unknown_pending_orders": "PASS",
        "reconciliation": "PASS",
        "state": "READY",
        "reason_code": "READY",
    }


def test_local_admission_fails_closed_for_unknown_pending_orders_without_echoing_input() -> None:
    gates = _all_clear_gates()
    gates["unknown_pending_orders"] = True
    gates["sensitive_detail"] = "do-not-echo"

    result = evaluate_local_runtime_admission(gates)

    assert result["unknown_pending_orders"] == "PARK"
    assert result["state"] == "PARK"
    assert result["reason_code"] == "UNKNOWN_PENDING_ORDERS"
    assert "do-not-echo" not in json.dumps(result)
