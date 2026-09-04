#!/usr/bin/env python3
"""Evaluate redacted local IBKR admission facts without touching a runtime.

The input contains only verified boolean gate facts.  This tool neither reads
broker/provider state nor treats configuration or CI as proof of an omitted
fact.  Missing or malformed facts therefore fail closed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_GATES = (
    "live_ready",
    "release",
    "mandate",
    "broker_session",
    "data_entitlement",
    "ledger",
    "unknown_pending_orders",
    "reconciliation",
)
_PASSING_VALUES = {
    **{gate: True for gate in _GATES if gate != "unknown_pending_orders"},
    "unknown_pending_orders": False,
}
_REASON_CODES = {
    "live_ready": "LIVE_READY_NOT_READY",
    "release": "RELEASE_NOT_READY",
    "mandate": "MANDATE_NOT_READY",
    "broker_session": "BROKER_SESSION_NOT_READY",
    "data_entitlement": "DATA_ENTITLEMENT_NOT_READY",
    "ledger": "LEDGER_NOT_READY",
    "unknown_pending_orders": "UNKNOWN_PENDING_ORDERS",
    "reconciliation": "RECONCILIATION_NOT_READY",
}


def _parked_result(*, reason_code: str) -> dict[str, str]:
    return {
        **{gate: "PARK" for gate in _GATES},
        "state": "PARK",
        "reason_code": reason_code,
    }


def evaluate_local_runtime_admission(gates: Mapping[str, object]) -> dict[str, str]:
    """Return only fixed, redacted gate statuses and a fail-closed disposition."""

    statuses: dict[str, str] = {}
    first_failed_gate: str | None = None
    for gate in _GATES:
        passed = gates.get(gate) is _PASSING_VALUES[gate]
        statuses[gate] = "PASS" if passed else "PARK"
        if not passed and first_failed_gate is None:
            first_failed_gate = gate

    if first_failed_gate is None:
        return {**statuses, "state": "READY", "reason_code": "READY"}
    return {
        **statuses,
        "state": "PARK",
        "reason_code": _REASON_CODES[first_failed_gate],
    }


def _load_gates(path: Path) -> Mapping[str, object] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate redacted local IBKR admission gates without broker or provider access."
    )
    parser.add_argument("--gates", type=Path, required=True, help="Local JSON object of verified boolean gates")
    args = parser.parse_args(argv)

    gates = _load_gates(args.gates)
    result = (
        evaluate_local_runtime_admission(gates)
        if gates is not None
        else _parked_result(reason_code="INPUT_INVALID")
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
