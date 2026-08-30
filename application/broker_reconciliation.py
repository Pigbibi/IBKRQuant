"""Read-only IBKR observations used by frozen-live reconciliation.

The module deliberately does not decide whether a baseline can resume.  It
only reads the broker surfaces that a higher-level reconciliation contract must
compare with a durable local ledger: configured account identity, positions,
cash, active orders, and recent executions.  Callers must hash the returned
normalised values locally and must never log or return them from an HTTP route.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from typing import Any

from quant_platform_kit.common.broker_reconciliation import (
    BrokerReconciliationEvidence,
    BrokerReconciliationFinding,
    build_broker_reconciliation_evidence,
    calculate_broker_observation_sha256,
    evaluate_broker_reconciliation_recovery,
)
from quant_platform_kit.common.execution_state import build_execution_marker_store_from_env


class IBKRReconciliationReadError(RuntimeError):
    """Raised when a required read-only broker surface cannot be reconciled."""


IBKR_RECONCILIATION_EXPECTED_DIGESTS_ENV = "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON"
_EXPECTED_DIGEST_KEYS = (
    "positions_sha256",
    "cash_sha256",
    "open_orders_sha256",
    "recent_executions_sha256",
    "local_execution_ledger_sha256",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _number(value: object, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise IBKRReconciliationReadError(f"IBKR reconciliation is missing {field_name}.") from exc


def normalize_account_ids(account_ids: Iterable[str] | str | None) -> tuple[str, ...]:
    if account_ids is None:
        return ()
    candidates = (account_ids,) if isinstance(account_ids, str) else tuple(account_ids)
    normalized = tuple(dict.fromkeys(_text(account_id) for account_id in candidates if _text(account_id)))
    if not normalized:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires one or more configured account ids."
        )
    return normalized


def _account_in_scope(account_id: object, *, selected_account_ids: tuple[str, ...], surface: str) -> bool:
    normalized = _text(account_id)
    if not normalized:
        raise IBKRReconciliationReadError(
            f"IBKR reconciliation received an unscoped {surface} record."
        )
    return normalized in selected_account_ids


def _safe_contract_fields(contract: Any) -> dict[str, object]:
    return {
        "con_id": _text(getattr(contract, "conId", "")),
        "symbol": _text(getattr(contract, "symbol", "")).upper(),
        "local_symbol": _text(getattr(contract, "localSymbol", "")),
        "sec_type": _text(getattr(contract, "secType", "")).upper(),
        "currency": _text(getattr(contract, "currency", "")).upper(),
    }


def _normalise_position(position: Any) -> dict[str, object]:
    return {
        "symbol": _text(getattr(position, "symbol", "")).upper(),
        "quantity": _number(getattr(position, "quantity", None), field_name="position quantity"),
        "average_cost": _number(
            getattr(position, "average_cost", None), field_name="position average cost"
        ),
        "currency": _text(getattr(position, "currency", "")).upper(),
    }


def _normalise_option_position(
    position: Mapping[str, object],
    *,
    selected_account_ids: tuple[str, ...],
) -> dict[str, object]:
    account_id = _text(position.get("account_id"))
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="option position"):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation received an out-of-scope option position."
        )
    return {
        "account": account_id,
        "underlier": _text(position.get("underlier")).upper(),
        "local_symbol": _text(position.get("local_symbol")),
        "expiration": _text(position.get("expiration")),
        "right": _text(position.get("right")).upper(),
        "strike": _number(position.get("strike"), field_name="option strike"),
        "quantity": _number(position.get("quantity"), field_name="option quantity"),
        "average_cost": _number(position.get("average_cost"), field_name="option average cost"),
        "currency": _text(position.get("currency")).upper(),
    }


def _normalise_cash_balance(value: Mapping[str, object], *, selected_account_ids: tuple[str, ...]) -> dict[str, object]:
    account_id = _text(value.get("account_id"))
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="cash"):
        raise IBKRReconciliationReadError("IBKR reconciliation received an out-of-scope cash record.")
    numeric_tags = {
        _text(tag): _number(number, field_name=f"cash tag {_text(tag)}")
        for tag, number in value.items()
        if _text(tag) not in {"account_id", "currency"}
    }
    return {
        "account": account_id,
        "currency": _text(value.get("currency")).upper(),
        "tags": dict(sorted(numeric_tags.items())),
    }


def _open_trade_account(trade: Any) -> object:
    order = getattr(trade, "order", None)
    return getattr(order, "account", None) or getattr(trade, "account", None)


def _normalise_open_trade(trade: Any, *, selected_account_ids: tuple[str, ...]) -> dict[str, object] | None:
    account_id = _open_trade_account(trade)
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="open order"):
        return None
    order = getattr(trade, "order", trade)
    status = getattr(trade, "orderStatus", None)
    contract = getattr(trade, "contract", None)
    if contract is None:
        raise IBKRReconciliationReadError("IBKR reconciliation received an open order without a contract.")
    return {
        "account": _text(account_id),
        "contract": _safe_contract_fields(contract),
        "perm_id": _text(getattr(order, "permId", "")),
        "action": _text(getattr(order, "action", "")).upper(),
        "order_type": _text(getattr(order, "orderType", "")).upper(),
        "total_quantity": _number(
            getattr(order, "totalQuantity", None), field_name="open order total quantity"
        ),
        "limit_price": _number(getattr(order, "lmtPrice", 0.0), field_name="open order limit price"),
        "aux_price": _number(getattr(order, "auxPrice", 0.0), field_name="open order aux price"),
        "status": _text(getattr(status, "status", "")),
        "filled": _number(getattr(status, "filled", 0.0), field_name="open order filled quantity"),
        "remaining": _number(
            getattr(status, "remaining", 0.0), field_name="open order remaining quantity"
        ),
    }


def _execution_account(fill: Any) -> object:
    execution = getattr(fill, "execution", None)
    account_id = getattr(execution, "acctNumber", None)
    if _text(account_id):
        return account_id
    order = getattr(getattr(fill, "trade", None), "order", None)
    return getattr(order, "account", None)


def _normalise_execution(fill: Any, *, selected_account_ids: tuple[str, ...]) -> dict[str, object] | None:
    account_id = _execution_account(fill)
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="execution"):
        return None
    execution = getattr(fill, "execution", None)
    contract = getattr(fill, "contract", None)
    if execution is None or contract is None:
        raise IBKRReconciliationReadError("IBKR reconciliation received an incomplete execution record.")
    return {
        "account": _text(account_id),
        "contract": _safe_contract_fields(contract),
        "execution_id": _text(getattr(execution, "execId", "")),
        "order_id": _text(getattr(execution, "orderId", "")),
        "time": _text(getattr(execution, "time", "")),
        "side": _text(getattr(execution, "side", "")).upper(),
        "shares": _number(getattr(execution, "shares", None), field_name="execution shares"),
        "price": _number(getattr(execution, "price", None), field_name="execution price"),
    }


def _sorted_records(records: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return sorted((dict(record) for record in records), key=lambda record: repr(sorted(record.items())))


def _load_all_open_trades(ib: Any) -> tuple[Any, ...]:
    request_all_open_orders = getattr(ib, "reqAllOpenOrders", None)
    open_trades = getattr(ib, "openTrades", None)
    if not callable(request_all_open_orders) or not callable(open_trades):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires read-only all-open-orders support."
        )
    try:
        request_all_open_orders()
        return tuple(open_trades() or ())
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load all active orders."
        ) from exc


def _load_recent_executions(ib: Any) -> tuple[Any, ...]:
    request_executions = getattr(ib, "reqExecutions", None)
    if not callable(request_executions):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires read-only recent-executions support."
        )
    try:
        return tuple(request_executions() or ())
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load recent executions."
        ) from exc


@dataclass(frozen=True)
class IBKRReconciliationObservations:
    """Sensitive, in-memory broker observations; never serialize to responses."""

    account_scope: Mapping[str, object]
    account_identity_match: bool
    positions: tuple[Mapping[str, object], ...]
    cash: tuple[Mapping[str, object], ...]
    open_orders: tuple[Mapping[str, object], ...]
    recent_executions: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class IBKRReconciliationCandidate:
    """Public-safe recovery candidate; the raw broker observations are omitted."""

    evidence: BrokerReconciliationEvidence
    recovery_blockers: tuple[BrokerReconciliationFinding, ...]
    expected_digests_configured: bool
    execution_ledger_records_count: int

    @property
    def permits_active_lkg(self) -> bool:
        return not self.recovery_blockers

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "schema_version": "ibkr_reconciliation_candidate.v1",
            "permits_active_lkg": self.permits_active_lkg,
            "expected_digests_configured": self.expected_digests_configured,
            "execution_ledger_records_count": self.execution_ledger_records_count,
            "recovery_blockers": [finding.value for finding in self.recovery_blockers],
            "evidence": self.evidence.to_dict(),
        }


def collect_read_only_reconciliation_observations(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None,
    fetch_portfolio_snapshot: Callable[..., Any],
    market_currency: str,
    cash_only_execution: bool,
) -> IBKRReconciliationObservations:
    """Read and normalise all broker surfaces needed by a continuity check.

    ``fetch_portfolio_snapshot`` is injected so this adapter uses the existing
    market-aware platform portfolio reader.  This keeps reconciliation scoped
    to exactly the account/currency semantics used by execution.
    """

    selected_account_ids = normalize_account_ids(account_ids)
    managed_accounts_fn = getattr(ib, "managedAccounts", None)
    if not callable(managed_accounts_fn):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires a managed-accounts identity response."
        )
    try:
        managed_accounts = {_text(account_id) for account_id in (managed_accounts_fn() or ()) if _text(account_id)}
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load managed-account identity."
        ) from exc
    identity_match = set(selected_account_ids).issubset(managed_accounts)
    snapshot = fetch_portfolio_snapshot(
        ib,
        account_ids=selected_account_ids,
        currency=market_currency,
        cash_only_execution=cash_only_execution,
    )
    metadata = getattr(snapshot, "metadata", {})
    if not isinstance(metadata, Mapping):
        raise IBKRReconciliationReadError("IBKR reconciliation portfolio snapshot metadata is invalid.")
    cash_balances = metadata.get("cash_balances")
    if not isinstance(cash_balances, (list, tuple)):
        raise IBKRReconciliationReadError("IBKR reconciliation is missing scoped cash balances.")
    option_positions = metadata.get("option_positions") or ()
    if not isinstance(option_positions, (list, tuple)):
        raise IBKRReconciliationReadError("IBKR reconciliation option-position metadata is invalid.")

    if any(not isinstance(option_position, Mapping) for option_position in option_positions):
        raise IBKRReconciliationReadError("IBKR reconciliation option-position metadata is invalid.")
    if any(not isinstance(value, Mapping) for value in cash_balances):
        raise IBKRReconciliationReadError("IBKR reconciliation cash-balance metadata is invalid.")
    positions = [_normalise_position(position) for position in (getattr(snapshot, "positions", ()) or ())]
    positions.extend(
        _normalise_option_position(
            option_position,
            selected_account_ids=selected_account_ids,
        )
        for option_position in option_positions
    )
    open_orders = [
        record
        for trade in _load_all_open_trades(ib)
        if (record := _normalise_open_trade(trade, selected_account_ids=selected_account_ids)) is not None
    ]
    executions = [
        record
        for fill in _load_recent_executions(ib)
        if (record := _normalise_execution(fill, selected_account_ids=selected_account_ids)) is not None
    ]
    return IBKRReconciliationObservations(
        account_scope={"account_ids": sorted(selected_account_ids)},
        account_identity_match=identity_match,
        positions=tuple(_sorted_records(positions)),
        cash=tuple(
            _sorted_records(
                _normalise_cash_balance(value, selected_account_ids=selected_account_ids)
                for value in cash_balances
            )
        ),
        open_orders=tuple(_sorted_records(open_orders)),
        recent_executions=tuple(_sorted_records(executions)),
    )


def _resolve_expected_digests(
    *,
    env_reader: Callable[[str, str | None], str | None] = os.getenv,
) -> Mapping[str, str] | None:
    raw_value = env_reader(IBKR_RECONCILIATION_EXPECTED_DIGESTS_ENV, None)
    if not _text(raw_value):
        return None
    try:
        decoded = json.loads(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation expected-digest configuration is not valid JSON."
        ) from exc
    if not isinstance(decoded, Mapping):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation expected-digest configuration must be an object."
        )
    normalized = {key: _text(decoded.get(key)) for key in _EXPECTED_DIGEST_KEYS}
    if any(not value for value in normalized.values()):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation expected-digest configuration is incomplete."
        )
    return normalized


def _continuity_fields(runtime_target: Any) -> tuple[str, str, str]:
    continuity = getattr(runtime_target, "live_continuity", None)
    if continuity is None:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires a frozen live-continuity baseline."
        )
    baseline_id = _text(getattr(continuity, "baseline_id", ""))
    baseline_target_sha256 = _text(getattr(continuity, "baseline_target_sha256", "")).lower()
    if not baseline_id or not baseline_target_sha256:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation live-continuity baseline is incomplete."
        )
    # ``resolve_runtime_target_from_env`` verifies this digest against the
    # original RUNTIME_TARGET_JSON before it returns RuntimeTarget.  Do not
    # re-fingerprint ``RuntimeTarget.to_dict()`` here: that representation
    # adds derived execution fields, so a legacy JSON baseline could be
    # falsely reported as changed even though the deployed target is valid.
    # The broker evidence remains strictly bound to the startup-validated
    # baseline; this only removes a representation-level false mismatch.
    return baseline_id, baseline_target_sha256, baseline_target_sha256


def build_reconciliation_candidate(
    *,
    observations: IBKRReconciliationObservations,
    runtime_target: Any,
    platform_id: str,
    strategy_profile: str,
    account_group: str,
    project_id: str | None,
    env_reader: Callable[[str, str | None], str | None] = os.getenv,
    observed_at: datetime | None = None,
) -> IBKRReconciliationCandidate:
    """Build a fail-closed, redacted recovery candidate from read-only data.

    The expected digests are deliberately optional for the *first* legacy
    probe.  If they do not yet exist, every state surface stays unmatched and
    the candidate remains in ``RECONCILE_ONLY``.  A trusted control plane must
    independently establish those digests before a later candidate can pass.
    """

    expected_digests = _resolve_expected_digests(env_reader=env_reader)
    account_scope_sha256 = calculate_broker_observation_sha256(observations.account_scope)
    positions_sha256 = calculate_broker_observation_sha256(observations.positions)
    cash_sha256 = calculate_broker_observation_sha256(observations.cash)
    open_orders_sha256 = calculate_broker_observation_sha256(observations.open_orders)
    recent_executions_sha256 = calculate_broker_observation_sha256(observations.recent_executions)
    execution_state_store = build_execution_marker_store_from_env(
        platform_env_prefix="IBKR",
        env_reader=env_reader,
        project_id=project_id,
    )
    local_execution_ledger_sha256, ledger_records_count = (
        execution_state_store.calculate_recent_ledger_digest(
            platform=platform_id,
            strategy_profile=strategy_profile,
            account_scope=account_group,
            execution_mode="live",
        )
    )
    baseline_id, baseline_target_sha256, runtime_target_sha256 = _continuity_fields(runtime_target)

    def matches(key: str, actual_digest: str) -> bool:
        return expected_digests is not None and expected_digests[key] == actual_digest

    evidence = build_broker_reconciliation_evidence(
        platform_id=platform_id,
        strategy_profile=strategy_profile,
        account_scope_sha256=account_scope_sha256,
        baseline_id=baseline_id,
        baseline_target_sha256=baseline_target_sha256,
        runtime_target_sha256=runtime_target_sha256,
        observed_at=observed_at or datetime.now(timezone.utc),
        broker_connected=True,
        account_identity_match=observations.account_identity_match,
        positions_match=matches("positions_sha256", positions_sha256),
        cash_match=matches("cash_sha256", cash_sha256),
        open_orders_match=matches("open_orders_sha256", open_orders_sha256),
        recent_executions_match=matches("recent_executions_sha256", recent_executions_sha256),
        local_execution_ledger_match=matches(
            "local_execution_ledger_sha256", local_execution_ledger_sha256
        ),
        positions_sha256=positions_sha256,
        cash_sha256=cash_sha256,
        open_orders_sha256=open_orders_sha256,
        recent_executions_sha256=recent_executions_sha256,
        local_execution_ledger_sha256=local_execution_ledger_sha256,
    )
    blockers = evaluate_broker_reconciliation_recovery(
        evidence,
        now=observed_at or datetime.now(timezone.utc),
        expected_platform_id=platform_id,
        expected_strategy_profile=strategy_profile,
        expected_account_scope_sha256=account_scope_sha256,
        expected_baseline_id=baseline_id,
        expected_runtime_target_sha256=runtime_target_sha256,
        expected_positions_sha256=(expected_digests or {}).get("positions_sha256"),
        expected_cash_sha256=(expected_digests or {}).get("cash_sha256"),
        expected_open_orders_sha256=(expected_digests or {}).get("open_orders_sha256"),
        expected_recent_executions_sha256=(expected_digests or {}).get("recent_executions_sha256"),
        expected_local_execution_ledger_sha256=(expected_digests or {}).get(
            "local_execution_ledger_sha256"
        ),
    )
    return IBKRReconciliationCandidate(
        evidence=evidence,
        recovery_blockers=blockers,
        expected_digests_configured=expected_digests is not None,
        execution_ledger_records_count=ledger_records_count,
    )


__all__ = [
    "IBKR_RECONCILIATION_EXPECTED_DIGESTS_ENV",
    "IBKRReconciliationCandidate",
    "IBKRReconciliationObservations",
    "IBKRReconciliationReadError",
    "build_reconciliation_candidate",
    "collect_read_only_reconciliation_observations",
    "normalize_account_ids",
]
