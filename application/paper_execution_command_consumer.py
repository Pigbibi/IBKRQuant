"""IBKR read-only reconciliation adapter for shared paper commands.

This module deliberately has no order, contract-submission, or execution-port
import.  It consumes only normalized portfolio snapshots and quote snapshots
provided by the isolated endpoint after the shared command binding passes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

from quant_platform_kit.common.execution_commands import ExecutionCommand, ExecutionCommandStore
from quant_platform_kit.common.paper_execution_command_consumer import (
    PaperExecutionProposal,
    PaperExecutionReconciliation,
    consume_due_paper_execution_commands as consume_shared_paper_execution_commands,
)
from quant_platform_kit.common.runtime_command_gate import RuntimeCommandExposureEffect
from quant_platform_kit.common.strategy_release import StrategyReleaseIdentity


IBKR_PAPER_EXECUTION_INTENT_SCHEMA_VERSION = "ibkr.paper-execution-intent.v1"
_NOTIONAL_TOLERANCE = 0.01


def resolve_paper_execution_command_consumer_enabled(*, env_reader, dry_run_only: bool) -> bool:
    """Enable only the isolated local-dry-run command consumer."""

    enabled = str(
        env_reader("IBKR_PAPER_EXECUTION_COMMAND_CONSUMER_ENABLED", "") or ""
    ).strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    if enabled and not dry_run_only:
        raise RuntimeError("IBKR paper command consumer requires IBKR_DRY_RUN_ONLY=true")
    return enabled


def _symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _symbols(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {_symbol(item) for item in value if _symbol(item)}


def _finite(value: object, *, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _cash_balance(portfolio: Any) -> float:
    metadata = getattr(portfolio, "metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("portfolio metadata is unavailable")
    return _finite(metadata.get("market_currency_cash"), field_name="portfolio.market_currency_cash")


def _reconcile(
    command: ExecutionCommand,
    *,
    portfolio: Any,
    quote_loader: Callable[[str], Any],
    managed_symbols: Sequence[str],
) -> PaperExecutionReconciliation:
    intent = command.intent
    if str(intent.get("schema_version") or "") != IBKR_PAPER_EXECUTION_INTENT_SCHEMA_VERSION:
        return PaperExecutionReconciliation(proposals=(), integrity_findings=("data_artifact_invalid",))
    if str(intent.get("target_mode") or "") != "value":
        return PaperExecutionReconciliation(proposals=(), integrity_findings=("data_artifact_invalid",))
    raw_targets = intent.get("targets")
    if not isinstance(raw_targets, Mapping):
        return PaperExecutionReconciliation(proposals=(), integrity_findings=("data_artifact_invalid",))
    try:
        targets = {
            _symbol(symbol): _finite(value, field_name=f"targets[{symbol!r}]")
            for symbol, value in raw_targets.items()
            if _symbol(symbol)
        }
    except ValueError:
        return PaperExecutionReconciliation(proposals=(), integrity_findings=("data_artifact_invalid",))
    strategy_symbols = _symbols(intent.get("strategy_symbols"))
    expected_symbols = {_symbol(symbol) for symbol in managed_symbols if _symbol(symbol)}
    if (
        not strategy_symbols
        or strategy_symbols != expected_symbols
        or set(targets) != strategy_symbols
        or any(value < 0.0 for value in targets.values())
    ):
        return PaperExecutionReconciliation(proposals=(), integrity_findings=("data_artifact_invalid",))

    findings: list[str] = []
    quantities: dict[str, float] = {}
    current_values: dict[str, float] = {}
    for position in tuple(getattr(portfolio, "positions", ()) or ()):
        symbol = _symbol(getattr(position, "symbol", ""))
        if symbol not in strategy_symbols:
            findings.append("position_reconciliation_mismatch")
            continue
        try:
            quantity = _finite(getattr(position, "quantity", None), field_name=f"position[{symbol}].quantity")
            recorded_value = _finite(
                getattr(position, "market_value", None),
                field_name=f"position[{symbol}].market_value",
            )
            quote = quote_loader(symbol)
            price = _finite(getattr(quote, "last_price", None), field_name=f"quote[{symbol}].last_price")
            if price <= 0.0:
                raise ValueError("quote price must be positive")
        except Exception:
            findings.append("position_reconciliation_mismatch")
            continue
        quote_value = quantity * price
        # IBKR cash-only commands need current market-value evidence. A stale
        # average-cost snapshot is not enough to classify exposure safely.
        tolerance = max(1.0, abs(quote_value) * 0.005)
        if quantity < -_NOTIONAL_TOLERANCE or recorded_value < -_NOTIONAL_TOLERANCE:
            findings.append("position_reconciliation_mismatch")
        if abs(recorded_value - quote_value) > tolerance:
            findings.append("position_reconciliation_mismatch")
        quantities[symbol] = quantities.get(symbol, 0.0) + quantity
        current_values[symbol] = current_values.get(symbol, 0.0) + quote_value

    try:
        cash_balance = _cash_balance(portfolio)
        total_equity = _finite(getattr(portfolio, "total_equity", None), field_name="portfolio.total_equity")
        tolerance = max(1.0, abs(total_equity) * 0.005)
        if abs(cash_balance + sum(current_values.values()) - total_equity) > tolerance:
            findings.append("position_reconciliation_mismatch")
    except ValueError:
        findings.append("position_reconciliation_mismatch")

    proposals: list[PaperExecutionProposal] = []
    for symbol in sorted(strategy_symbols):
        current_value = current_values.get(symbol, 0.0)
        target_value = targets[symbol]
        delta_value = target_value - current_value
        if abs(delta_value) <= _NOTIONAL_TOLERANCE:
            continue
        try:
            quote = quote_loader(symbol)
            price = _finite(getattr(quote, "last_price", None), field_name=f"quote[{symbol}].last_price")
            if price <= 0.0:
                raise ValueError("quote price must be positive")
        except Exception:
            findings.append("position_reconciliation_mismatch")
            continue
        if abs(target_value) < abs(current_value) - _NOTIONAL_TOLERANCE:
            effect = RuntimeCommandExposureEffect.REDUCES
        elif abs(target_value) > abs(current_value) + _NOTIONAL_TOLERANCE:
            effect = RuntimeCommandExposureEffect.INCREASES
        else:
            effect = RuntimeCommandExposureEffect.NEUTRAL
        proposals.append(
            PaperExecutionProposal(
                symbol=symbol,
                exposure_effect=effect,
                details={
                    "side": "buy" if delta_value > 0.0 else "sell",
                    "quantity": round(abs(delta_value) / price, 8),
                    "reference_price": round(price, 8),
                    "current_value": round(current_value, 8),
                    "target_value": round(target_value, 8),
                    "target_notional_delta": round(delta_value, 8),
                    "current_quantity": round(quantities.get(symbol, 0.0), 8),
                },
            )
        )
    return PaperExecutionReconciliation(
        proposals=tuple(proposals),
        integrity_findings=tuple(dict.fromkeys(findings)),
    )


def consume_due_paper_execution_commands(
    *,
    store: ExecutionCommandStore | None,
    as_of_session: date | str,
    claimant: str,
    portfolio_loader: Callable[[], Any],
    quote_loader: Callable[[str], Any],
    managed_symbols: Sequence[str],
    runtime_release_receipt: Mapping[str, Any] | None,
    expected_strategy_release: StrategyReleaseIdentity | Mapping[str, object] | None,
    expected_command_binding: Mapping[str, object] | None,
) -> dict[str, object]:
    """Consume paper commands after shared release and delivery binding checks."""

    return consume_shared_paper_execution_commands(
        store=store,
        as_of_session=as_of_session,
        claimant=claimant,
        reconcile_command=lambda command: _reconcile(
            command,
            portfolio=portfolio_loader(),
            quote_loader=quote_loader,
            managed_symbols=managed_symbols,
        ),
        runtime_release_receipt=runtime_release_receipt,
        expected_strategy_release=expected_strategy_release,
        expected_command_binding=expected_command_binding,
    )


__all__ = (
    "IBKR_PAPER_EXECUTION_INTENT_SCHEMA_VERSION",
    "consume_due_paper_execution_commands",
    "resolve_paper_execution_command_consumer_enabled",
)
