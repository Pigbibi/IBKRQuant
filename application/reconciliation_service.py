"""Structured reconciliation record helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd


_NON_LIVE_ENVELOPE_VERSION = "v1"
_NON_LIVE_ENVELOPE_STRATEGY_PROFILE = "soxl_soxx_trend_income"
_NON_LIVE_ENVELOPE_EVIDENCE_SCOPE = "NON_LIVE_STATIC"
_FORBIDDEN_METADATA_KEY_SEQUENCES = frozenset(
    {
        ("access", "key"),
        ("access", "key", "id"),
        ("accesskey",),
        ("accesskeyid",),
        ("account",),
        ("api", "key"),
        ("apikey",),
        ("authorization",),
        ("balance",),
        ("capital",),
        ("cookie",),
        ("credential",),
        ("fill",),
        ("fills",),
        ("header",),
        ("headers",),
        ("jwt",),
        ("notional",),
        ("order",),
        ("passphrase",),
        ("password",),
        ("position",),
        ("private", "key"),
        ("privatekey",),
        ("provider",),
        ("quantity",),
        ("raw",),
        ("secret",),
        ("token",),
        ("verified", "active"),
        ("verifiedactive",),
    }
)
_FORBIDDEN_METADATA_VALUES = {"matched", "mismatched", "verifiedactive"}
_NON_LIVE_ENVELOPE_KEYS = {
    "envelope_version",
    "strategy_profile",
    "evidence_scope",
    "reconciliation",
    "learning_only",
    "promotion_eligible",
    "live_ready",
    "size_zero_required",
    "no_order",
    "learning_disposition",
    "source_revision",
    "source_digests",
}


def _json_safe(value: Any):
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _normalized_metadata_key(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _metadata_key_tokens(value: Any) -> tuple[str, ...]:
    tokens = []
    for segment in re.findall(r"[A-Za-z0-9]+", str(value)):
        tokens.extend(
            token.lower()
            for token in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", segment)
        )
    return tuple(tokens)


def _contains_forbidden_metadata_key(value: Any) -> bool:
    tokens = _metadata_key_tokens(value)
    return any(
        tokens[index : index + len(sequence)] == sequence
        for sequence in _FORBIDDEN_METADATA_KEY_SEQUENCES
        for index in range(len(tokens) - len(sequence) + 1)
    )


def _reject_non_live_metadata(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _contains_forbidden_metadata_key(key):
                raise ValueError("non-live reconciliation metadata contains a value that is not allowed")
            _reject_non_live_metadata(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_live_metadata(item)
        return
    if isinstance(value, str) and _normalized_metadata_key(value) in _FORBIDDEN_METADATA_VALUES:
        raise ValueError("non-live reconciliation metadata contains a value that is not allowed")


def _validate_optional_provenance_fields(envelope: Mapping[str, Any]) -> None:
    if "source_revision" in envelope:
        source_revision = envelope["source_revision"]
        if not isinstance(source_revision, str) or not source_revision.strip():
            raise ValueError("non-live reconciliation envelope contains a value that is not allowed")
        _reject_non_live_metadata({"source_revision": source_revision})

    if "source_digests" in envelope:
        source_digests = envelope["source_digests"]
        if not isinstance(source_digests, Mapping) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
            for key, value in source_digests.items()
        ):
            raise ValueError("non-live reconciliation envelope contains a value that is not allowed")
        _reject_non_live_metadata(source_digests)


def build_non_live_reconciliation_envelope(
    *,
    learning_disposition: str,
    source_revision: str | None = None,
    source_digests: Mapping[str, str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the fixed, fail-closed envelope for the SOXL static evidence slice."""
    if learning_disposition != "negative":
        raise ValueError("non-live reconciliation envelopes require learning_disposition='negative'")
    if metadata is not None:
        _reject_non_live_metadata(metadata)

    envelope = {
        "envelope_version": _NON_LIVE_ENVELOPE_VERSION,
        "strategy_profile": _NON_LIVE_ENVELOPE_STRATEGY_PROFILE,
        "evidence_scope": _NON_LIVE_ENVELOPE_EVIDENCE_SCOPE,
        "reconciliation": {"status": "MISSING"},
        "learning_only": True,
        "promotion_eligible": False,
        "live_ready": False,
        "size_zero_required": True,
        "no_order": True,
        "learning_disposition": "negative",
    }
    if source_revision is not None:
        _validate_optional_provenance_fields({"source_revision": source_revision})
        envelope["source_revision"] = source_revision
    if source_digests is not None:
        _validate_optional_provenance_fields({"source_digests": source_digests})
        envelope["source_digests"] = dict(source_digests)
    return envelope


def canonical_reconciliation_envelope_json(envelope: Mapping[str, Any]) -> str:
    """Serialize a static non-live envelope deterministically without unsafe metadata."""
    if set(envelope).difference(_NON_LIVE_ENVELOPE_KEYS):
        raise ValueError("non-live reconciliation envelope contains a value that is not allowed")
    required_values = {
        "envelope_version": _NON_LIVE_ENVELOPE_VERSION,
        "strategy_profile": _NON_LIVE_ENVELOPE_STRATEGY_PROFILE,
        "evidence_scope": _NON_LIVE_ENVELOPE_EVIDENCE_SCOPE,
        "reconciliation": {"status": "MISSING"},
        "learning_only": True,
        "promotion_eligible": False,
        "live_ready": False,
        "size_zero_required": True,
        "no_order": True,
        "learning_disposition": "negative",
    }
    for key, expected_value in required_values.items():
        actual_value = envelope.get(key)
        if expected_value is True or expected_value is False:
            if type(actual_value) is not bool or actual_value is not expected_value:
                raise ValueError("non-live reconciliation envelope contains a value that is not allowed")
        elif actual_value != expected_value:
            raise ValueError("non-live reconciliation envelope contains a value that is not allowed")
    _validate_optional_provenance_fields(envelope)
    return json.dumps(_json_safe(envelope), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def reconciliation_envelope_digest(envelope: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 digest for a static non-live envelope."""
    canonical_json = canonical_reconciliation_envelope_json(envelope)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def default_reconciliation_output_path(strategy_profile: str | None) -> Path:
    profile = str(strategy_profile or "unknown").strip() or "unknown"
    safe_profile = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in profile)
    return Path(tempfile.gettempdir()) / f"ibkr_reconciliation_{safe_profile}.json"


def build_reconciliation_record(
    *,
    strategy_profile: str | None,
    mode: str,
    trade_date: str | None,
    snapshot_as_of,
    signal_metadata: dict[str, Any] | None,
    target_weights: dict[str, float] | None,
    execution_summary: dict[str, Any] | None,
    no_op_reason: str | None = None,
) -> dict[str, Any]:
    signal_metadata = dict(signal_metadata or {})
    execution_summary = dict(execution_summary or {})
    target_weights = dict(target_weights or {})
    allocation = dict(signal_metadata.get("allocation") or {})
    allocation_safe_haven_symbols = tuple(allocation.get("safe_haven_symbols") or ())
    allocation_safe_haven_symbol = allocation_safe_haven_symbols[0] if allocation_safe_haven_symbols else None
    record = {
        "strategy_profile": strategy_profile,
        "mode": mode,
        "trade_date": trade_date,
        "snapshot_as_of": snapshot_as_of,
        "snapshot_guard_decision": signal_metadata.get("snapshot_guard_decision"),
        "snapshot_path": signal_metadata.get("feature_snapshot_path") or signal_metadata.get("snapshot_path"),
        "regime": signal_metadata.get("regime"),
        "breadth": signal_metadata.get("breadth_ratio"),
        "target_stock_weight": signal_metadata.get("target_stock_weight"),
        "realized_stock_weight": signal_metadata.get("realized_stock_weight"),
        "target_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "realized_safe_haven_weight": execution_summary.get("realized_safe_haven_weight"),
        "safe_haven_symbol": (
            execution_summary.get("safe_haven_symbol")
            or signal_metadata.get("safe_haven_symbol")
            or allocation_safe_haven_symbol
        ),
        "target_holdings": [
            {"symbol": symbol, "target_weight": float(weight)}
            for symbol, weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0]))
        ],
        "target_vs_current": execution_summary.get("target_vs_current") or [],
        "orders_submitted": execution_summary.get("orders_submitted") or [],
        "orders_filled": execution_summary.get("orders_filled") or [],
        "orders_partially_filled": execution_summary.get("orders_partially_filled") or [],
        "orders_skipped": execution_summary.get("orders_skipped") or [],
        "skipped_reasons": execution_summary.get("skipped_reasons") or [],
        "residual_cash_estimate": execution_summary.get("residual_cash_estimate"),
        "cash_reserve_dollars": execution_summary.get("cash_reserve_dollars"),
        "current_stock_weight": execution_summary.get("current_stock_weight"),
        "current_safe_haven_weight": execution_summary.get("current_safe_haven_weight"),
        "price_source_mode": execution_summary.get("price_source_mode"),
        "quote_snapshot": execution_summary.get("quote_snapshot") or {},
        "snapshot_price_fallback_used": execution_summary.get("snapshot_price_fallback_used"),
        "snapshot_price_fallback_count": execution_summary.get("snapshot_price_fallback_count"),
        "snapshot_price_fallback_symbols": execution_summary.get("snapshot_price_fallback_symbols") or [],
        "execution_status": execution_summary.get("execution_status") or ("no_op" if no_op_reason else "executed"),
        "lock_path": execution_summary.get("lock_path"),
        "no_op_reason": no_op_reason or execution_summary.get("no_op_reason"),
        "fail_reason": signal_metadata.get("fail_reason"),
        "status_icon": signal_metadata.get("status_icon"),
    }
    return _json_safe(record)


def write_reconciliation_record(record: dict[str, Any], *, output_path: str | Path | None = None) -> Path:
    path = Path(output_path) if output_path else default_reconciliation_output_path(record.get("strategy_profile"))
    if output_path and path.suffix.lower() != ".json":
        trade_date = str(record.get("trade_date") or "latest").strip() or "latest"
        path = path / trade_date / "reconciliation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(record), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path
