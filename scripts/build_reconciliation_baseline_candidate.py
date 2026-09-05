#!/usr/bin/env python3
"""Build a review-ready legacy-baseline candidate from redacted IBKR receipts.

The input files must be private runtime reports or ``/reconcile`` response
payloads.  Only QPK's digest-only evidence is loaded; this tool never opens a
broker connection, changes Cloud Run configuration, writes an execution
marker, or submits an order.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from application.broker_reconciliation_candidate import (
    SourceReceiptExpectation, calculate_source_receipts_sha256, validate_reconciliation_candidate_sources,
)

from quant_platform_kit.common.broker_reconciliation import BrokerReconciliationEvidence
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    evaluate_broker_reconciliation_baseline_enrollment,
)


def extract_reconciliation_evidence(payload: Mapping[str, Any]) -> BrokerReconciliationEvidence:
    """Extract one digest-only receipt from an endpoint or persisted report."""

    if not isinstance(payload, Mapping):
        raise ValueError("reconciliation receipt must be a JSON object")
    candidate = payload
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        nested = diagnostics.get("broker_reconciliation")
        if isinstance(nested, Mapping):
            candidate = nested
    # The manual evidence workflow stores only this safe candidate beneath a
    # small artifact envelope.  Accept that envelope too, so enrollment never
    # needs the broader private execution report as an input.
    reconciliation = payload.get("reconciliation")
    if isinstance(reconciliation, Mapping):
        candidate = reconciliation
    evidence = candidate.get("evidence") if isinstance(candidate, Mapping) else None
    if not isinstance(evidence, Mapping):
        raise ValueError("receipt does not contain broker_reconciliation evidence")
    return BrokerReconciliationEvidence.from_dict(evidence)


def evaluate_receipts(
    payloads: Iterable[Mapping[str, Any]],
    *,
    source_receipt_records: Sequence[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a redacted candidate or stable findings for a private controller."""

    evidences = [extract_reconciliation_evidence(payload) for payload in payloads]
    if not evidences:
        raise ValueError("at least one reconciliation receipt is required")
    root = calculate_source_receipts_sha256(
        source_receipt_records, strategy_profile=evidences[0].strategy_profile, expectations=expectations,
    )
    if sorted(record["evidence_sha256"] for record in source_receipt_records) != sorted(item.evidence_sha256 for item in evidences):
        raise ValueError("source records must match receipt evidence members exactly")
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(evidences, now=now, source_receipts_sha256=root)
    result: dict[str, object] = {
        "schema_version": "ibkr_reconciliation_baseline_enrollment.v1",
        "ready_for_independent_review": evaluation.ready_for_independent_review,
        "findings": [finding.value for finding in evaluation.findings],
    }
    if evaluation.candidate is not None:
        result["candidate"] = validate_reconciliation_candidate_sources(
            evaluation.candidate, source_receipt_records=source_receipt_records, expectations=expectations,
        ).to_dict()
    return result


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read receipt: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"receipt is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be a JSON object: {path}")
    return value


def load_source_inputs(
    records_path: Path, expectations_path: Path,
) -> tuple[list[Mapping[str, object]], tuple[SourceReceiptExpectation, ...]]:
    """Load existing private record lists, never infer trusted expectations."""
    try:
        records = json.loads(records_path.read_text(encoding="utf-8"))
        raw_expectations = json.loads(expectations_path.read_text(encoding="utf-8"))
        if not isinstance(records, list) or not isinstance(raw_expectations, list):
            raise ValueError("source inputs must be lists")
        expectations = tuple(SourceReceiptExpectation(**item) for item in raw_expectations)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("private source inputs are invalid or unavailable") from exc
    return records, expectations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a non-authorising legacy IBKR reconciliation baseline candidate."
    )
    parser.add_argument(
        "--receipt",
        action="append",
        type=Path,
        required=True,
        help="Private /reconcile response or persisted runtime report; supply at least once.",
    )
    parser.add_argument("--source-records", type=Path, required=True, help="Private saved source record list")
    parser.add_argument("--source-expectations", type=Path, required=True, help="Independently verified private source expectations")
    parser.add_argument("--now", help="Optional ISO-8601 time used for deterministic validation.")
    args = parser.parse_args(argv)
    try:
        reference_now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
        records, expectations = load_source_inputs(args.source_records, args.source_expectations)
        result = evaluate_receipts((_load_payload(path) for path in args.receipt), now=reference_now,
                                   source_receipt_records=records, expectations=expectations)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready_for_independent_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
