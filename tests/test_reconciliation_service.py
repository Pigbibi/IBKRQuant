from __future__ import annotations

from types import MappingProxyType

import pytest

from application.reconciliation_service import (
    build_non_live_reconciliation_envelope,
    canonical_reconciliation_envelope_json,
    reconciliation_envelope_digest,
)


def test_static_envelope_is_fail_closed_despite_runtime_descriptive_metadata():
    envelope = build_non_live_reconciliation_envelope(
        learning_disposition="negative",
        source_revision="0dbca440",
        source_digests={"selection": "a" * 64},
        metadata={
            "strategy_profile": "soxl_soxx_trend_income",
            "mode": "live",
            "execution_report": {"result": "executed"},
        },
    )

    assert envelope == {
        "envelope_version": "v1",
        "strategy_profile": "soxl_soxx_trend_income",
        "evidence_scope": "NON_LIVE_STATIC",
        "reconciliation": {"status": "MISSING"},
        "learning_only": True,
        "promotion_eligible": False,
        "live_ready": False,
        "size_zero_required": True,
        "no_order": True,
        "learning_disposition": "negative",
        "source_revision": "0dbca440",
        "source_digests": {"selection": "a" * 64},
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"reconciliation": {"status": "MATCHED"}},
        {"reconciliation": {"status": "MISMATCHED"}},
        {"verified_active": True},
        {"fills": [{"symbol": "SOXL"}]},
        {"capital": 1},
        {"order": {"id": "order-1"}},
        {"balance": 1},
        {"position": {"symbol": "SOXL"}},
        {"account_identifier": "redacted"},
        {"provider_row": {"close": 1}},
        {"raw_market_data": {"close": 1}},
    ],
)
def test_static_envelope_rejects_material_runtime_assertions(metadata):
    with pytest.raises(ValueError, match="not allowed"):
        build_non_live_reconciliation_envelope(
            learning_disposition="negative",
            metadata=metadata,
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"outer": {"secret": "value"}},
        {"outer": {"token": "value"}},
        {"outer": {"headers": {"Authorization": "Bearer value"}}},
        {"outer": {"jwt": "value"}},
        {"outer": {"cookie": "value"}},
        {"outer": {"api_key": "value"}},
    ],
)
def test_static_envelope_rejects_nested_sensitive_metadata(metadata):
    with pytest.raises(ValueError, match="not allowed"):
        build_non_live_reconciliation_envelope(
            learning_disposition="negative",
            metadata=metadata,
        )


def test_static_envelope_canonical_serialization_and_digest_are_deterministic():
    first = build_non_live_reconciliation_envelope(
        learning_disposition="negative",
        source_digests={"z": "2", "a": "1"},
    )
    second = build_non_live_reconciliation_envelope(
        learning_disposition="negative",
        source_digests={"a": "1", "z": "2"},
    )

    assert canonical_reconciliation_envelope_json(first) == canonical_reconciliation_envelope_json(second)
    assert reconciliation_envelope_digest(first) == reconciliation_envelope_digest(second)


def test_canonical_serialization_rejects_attempts_to_override_fail_closed_flags():
    envelope = build_non_live_reconciliation_envelope(learning_disposition="negative")
    envelope["reconciliation"] = {"status": "MATCHED"}

    with pytest.raises(ValueError, match="not allowed"):
        canonical_reconciliation_envelope_json(envelope)


@pytest.mark.parametrize(
    ("key", "integer_substitute"),
    [
        ("learning_only", 1),
        ("promotion_eligible", 0),
        ("live_ready", 0),
        ("size_zero_required", 1),
        ("no_order", 1),
    ],
)
def test_canonical_serialization_rejects_integer_substitutes_for_fixed_boolean_flags(key, integer_substitute):
    envelope = build_non_live_reconciliation_envelope(learning_disposition="negative")
    envelope[key] = integer_substitute

    with pytest.raises(ValueError, match="not allowed"):
        canonical_reconciliation_envelope_json(envelope)


@pytest.mark.parametrize("sensitive_key", ["password", "passphrase"])
def test_static_envelope_rejects_password_bearing_metadata(sensitive_key):
    with pytest.raises(ValueError, match="not allowed"):
        build_non_live_reconciliation_envelope(
            learning_disposition="negative",
            metadata={sensitive_key: "redacted"},
        )


@pytest.mark.parametrize("sensitive_key", ["private_key", "access_key", "access_key_id"])
def test_static_envelope_rejects_conventional_access_credentials(sensitive_key):
    with pytest.raises(ValueError, match="not allowed"):
        build_non_live_reconciliation_envelope(
            learning_disposition="negative",
            metadata={"nested": {sensitive_key: "redacted"}},
        )


@pytest.mark.parametrize("metadata_key", ["max_drawdown", "drawdown_report"])
def test_static_envelope_accepts_descriptive_drawdown_metadata(metadata_key):
    envelope = build_non_live_reconciliation_envelope(
        learning_disposition="negative",
        metadata={metadata_key: "research-only"},
    )

    assert envelope["reconciliation"] == {"status": "MISSING"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source_revision", 123),
        ("source_revision", ""),
        ("source_digests", None),
        ("source_digests", []),
        ("source_digests", {"selection": float("nan")}),
        ("source_digests", {1: "a" * 64}),
    ],
)
def test_canonical_serialization_rejects_invalid_optional_provenance_types(key, value):
    envelope = build_non_live_reconciliation_envelope(learning_disposition="negative")
    envelope[key] = value

    with pytest.raises(ValueError, match="not allowed"):
        canonical_reconciliation_envelope_json(envelope)


@pytest.mark.parametrize("assertion", ["VERIFIED-ACTIVE", "verified active"])
def test_static_envelope_rejects_normalized_forbidden_assertions(assertion):
    with pytest.raises(ValueError, match="not allowed"):
        build_non_live_reconciliation_envelope(
            learning_disposition="negative",
            metadata={"reconciliation_assertion": assertion},
        )


def test_canonical_serialization_accepts_arbitrary_mapping_envelopes():
    envelope = build_non_live_reconciliation_envelope(learning_disposition="negative")

    assert canonical_reconciliation_envelope_json(MappingProxyType(envelope)) == canonical_reconciliation_envelope_json(
        envelope
    )
