#!/usr/bin/env python3
"""Build or explicitly create an immutable IBKR recovery state ledger.

By default this command only prints a locally derived ledger.  The optional
GCS write uses create-only semantics and does not deploy, modify a runtime
target, connect to IBKR, or submit an order.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.reconciliation_recovery_state_ledger import (
    RECOVERY_STATE_LEDGER_SCHEMA_VERSION,
    build_recovery_state_ledger,
)


_STATE_LEDGER_PREFIX = "reconciliation-recovery/ibkr/state/"


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("unable to read reconciliation recovery verification") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("reconciliation recovery verification is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("reconciliation recovery verification must be a JSON object")
    return value


def _parse_state_ledger_uri(value: str) -> tuple[str, str]:
    normalized = str(value or "").strip()
    if not normalized.startswith("gs://"):
        raise ValueError("state_ledger_uri must use gs://")
    bucket_name, separator, object_name = normalized.removeprefix("gs://").partition("/")
    if not bucket_name or not separator or not object_name.startswith(_STATE_LEDGER_PREFIX) or not object_name.endswith(".json"):
        raise ValueError("state_ledger_uri must use the IBKR recovery state prefix and .json suffix")
    return bucket_name, object_name


def archive_recovery_state_ledger(
    ledger: Mapping[str, object],
    *,
    state_ledger_uri: str,
    storage_client_factory: Any | None = None,
) -> dict[str, str]:
    """Create an immutable ledger object without reading, listing, or replacing it."""

    if ledger.get("schema_version") != RECOVERY_STATE_LEDGER_SCHEMA_VERSION:
        raise ValueError("reconciliation recovery state ledger has an unsupported schema")
    bucket_name, object_name = _parse_state_ledger_uri(state_ledger_uri)
    if storage_client_factory is None:
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - runtime image provides this dependency.
            raise RuntimeError("google-cloud-storage is required to archive a recovery state ledger") from exc
        client = storage.Client()
    else:
        client = storage_client_factory()
    try:
        client.bucket(bucket_name).blob(object_name).upload_from_string(
            json.dumps(dict(ledger), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            content_type="application/json",
            if_generation_match=0,
        )
    except Exception as exc:
        raise RuntimeError("reconciliation recovery state ledger was not created") from exc
    return {
        "uri": f"gs://{bucket_name}/{object_name}",
        "schema_version": RECOVERY_STATE_LEDGER_SCHEMA_VERSION,
        "recovery_id": str(ledger["recovery_id"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or explicitly archive an immutable IBKR recovery state ledger."
    )
    parser.add_argument("--verification", type=Path, required=True, help="Fresh verify-only recovery result")
    parser.add_argument("--service-name", required=True, help="One exact Cloud Run service name")
    parser.add_argument(
        "--archive-gcs-uri",
        help="Explicit private gs://.../reconciliation-recovery/ibkr/state/*.json object; uses create-only generation precondition",
    )
    args = parser.parse_args(argv)
    try:
        ledger = build_recovery_state_ledger(
            verification=_load_json(args.verification),
            service_name=args.service_name,
        )
        if args.archive_gcs_uri:
            print(json.dumps({"ledger": ledger, "archive": archive_recovery_state_ledger(
                ledger,
                state_ledger_uri=args.archive_gcs_uri,
            )}, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(ledger, ensure_ascii=False, sort_keys=True))
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
