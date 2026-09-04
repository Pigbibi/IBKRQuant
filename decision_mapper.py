from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant_platform_kit.common.strategy_contracts import (
    StrategyDecision,
    build_allocation_intent,
    build_allocation_payload,
)
from quant_platform_kit.common.execution_translation import (
    translate_decision_to_target_mode,
)
from strategy_registry import IBKR_PLATFORM, resolve_strategy_definition
from us_equity_strategies.cash_only_equity import resolve_strategy_equity_for_targets


_EMERGENCY_FLAGS = frozenset({"emergency", "hard_defense"})
_NO_EXECUTE_FLAGS = frozenset({"no_execute"})


def _resolve_allocation_order(strategy_profile: str) -> str:
    canonical_profile = resolve_strategy_definition(
        strategy_profile,
        platform_id=IBKR_PLATFORM,
    ).profile
    if canonical_profile == "soxl_soxx_trend_income":
        return "risk_income_safe"
    return "risk_safe_income"


def _normalize_to_weight_decision(
    decision: StrategyDecision,
    runtime_metadata: Mapping[str, Any],
) -> StrategyDecision:
    return translate_decision_to_target_mode(
        decision,
        target_mode="weight",
        total_equity=runtime_metadata.get("portfolio_total_equity"),
    )


def _derive_target_weights(decision: StrategyDecision) -> dict[str, float]:
    weights: dict[str, float] = {}
    for position in decision.positions:
        if position.target_weight is None:
            raise ValueError(
                "IBKR decision mapper only supports weight-based positions; "
                f"position {position.symbol!r} is missing target_weight"
            )
        weights[position.symbol] = float(position.target_weight)
    return weights


def _derive_managed_symbols(
    decision: StrategyDecision,
    runtime_metadata: Mapping[str, Any],
    *,
    allocation_payload: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    explicit = runtime_metadata.get("managed_symbols")
    if explicit:
        return tuple(str(symbol) for symbol in explicit)
    allocation_symbols = allocation_payload.get("strategy_symbols") if allocation_payload else None
    if allocation_symbols:
        return tuple(str(symbol) for symbol in allocation_symbols)
    return tuple(position.symbol for position in decision.positions)


def _derive_safe_haven_symbol(
    decision: StrategyDecision,
    runtime_metadata: Mapping[str, Any],
) -> str | None:
    explicit = runtime_metadata.get("safe_haven_symbol")
    if explicit:
        return str(explicit)
    for position in decision.positions:
        if position.role == "safe_haven":
            return position.symbol
    return None


def _derive_signal_description(
    decision: StrategyDecision,
    runtime_metadata: Mapping[str, Any],
) -> str:
    diagnostics = decision.diagnostics
    candidates = (
        diagnostics.get("signal_description"),
        diagnostics.get("signal_display"),
        diagnostics.get("signal_message"),
        runtime_metadata.get("signal_description"),
        runtime_metadata.get("signal_display"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return "decision_ready"


def _derive_status_description(
    decision: StrategyDecision,
    runtime_metadata: Mapping[str, Any],
) -> str:
    diagnostics = decision.diagnostics
    candidates = (
        diagnostics.get("status_description"),
        diagnostics.get("canary_status"),
        diagnostics.get("market_status"),
        runtime_metadata.get("status_description"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return _derive_signal_description(decision, runtime_metadata)


def _derive_execution_annotations(
    diagnostics: Mapping[str, Any],
    runtime_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    annotations: dict[str, Any] = {}
    raw_runtime_annotations = runtime_metadata.get("execution_annotations")
    if isinstance(raw_runtime_annotations, Mapping):
        annotations.update(raw_runtime_annotations)
    raw_diagnostic_annotations = diagnostics.get("execution_annotations")
    if isinstance(raw_diagnostic_annotations, Mapping):
        annotations.update(raw_diagnostic_annotations)
    return annotations


def map_strategy_decision(
    decision: StrategyDecision,
    *,
    strategy_profile: str,
    runtime_metadata: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float] | None, str, bool, str, dict[str, Any]]:
    runtime_metadata = dict(runtime_metadata or {})
    canonical_profile = resolve_strategy_definition(
        strategy_profile,
        platform_id=IBKR_PLATFORM,
    ).profile
    diagnostics = dict(decision.diagnostics)
    # Platform-computed risk inputs for QPK apply_risk_gate / stop-loss checks.
    if runtime_metadata.get("unrealized_pnl_pct") is not None:
        diagnostics["unrealized_pnl_pct"] = float(runtime_metadata["unrealized_pnl_pct"])
    if runtime_metadata.get("consecutive_losses") is not None:
        diagnostics["consecutive_losses"] = int(runtime_metadata["consecutive_losses"])
    risk_flags = tuple(str(flag) for flag in decision.risk_flags)
    no_execute = bool(_NO_EXECUTE_FLAGS & set(risk_flags))
    if not no_execute and not decision.positions:
        # An empty position set is not a safe implicit liquidation instruction.
        # It can result from a degraded strategy plug-in or missing inputs, so
        # keep the existing book unchanged until a strategy explicitly emits a
        # releasable allocation.
        no_execute = True
        risk_flags = tuple(dict.fromkeys((*risk_flags, "no_execute")))
        diagnostics.setdefault("execution_blocked_reason", "empty_position_decision")
    total_equity_value = runtime_metadata.get("portfolio_total_equity")
    cash_only_execution = bool(runtime_metadata.get("cash_only_execution", True))
    if not no_execute and total_equity_value is not None:
        total_equity = float(total_equity_value)
        market_values = runtime_metadata.get("market_values")
        if not isinstance(market_values, Mapping):
            market_values = {}
        raw_cash = float(runtime_metadata.get("liquid_cash", runtime_metadata.get("available_cash", 0.0)) or 0.0)
        effective_equity, deleverage_mode = resolve_strategy_equity_for_targets(
            market_values=market_values,
            raw_cash=raw_cash,
            cash_only_execution=cash_only_execution,
        )
        if total_equity <= 0.0:
            if deleverage_mode:
                runtime_metadata["cash_only_deleverage_mode"] = True
                runtime_metadata["portfolio_total_equity"] = effective_equity
            else:
                no_execute = True
                risk_flags = tuple(dict.fromkeys((*risk_flags, "no_execute")))
                diagnostics.setdefault("execution_blocked_reason", "non_positive_total_equity")
                diagnostics.setdefault("portfolio_total_equity", total_equity)
    normalized_decision = decision if no_execute else _normalize_to_weight_decision(decision, runtime_metadata)
    target_weights = None if no_execute else _derive_target_weights(normalized_decision)
    allocation_payload = None
    if not no_execute and normalized_decision.positions:
        allocation_payload = build_allocation_payload(
            build_allocation_intent(
                normalized_decision,
                strategy_profile=canonical_profile,
                strategy_symbols_order=_resolve_allocation_order(canonical_profile),
            )
        )
    signal_desc = _derive_signal_description(decision, runtime_metadata)
    status_desc = _derive_status_description(decision, runtime_metadata)
    is_emergency = bool(_EMERGENCY_FLAGS & set(risk_flags))

    metadata: dict[str, Any] = {**runtime_metadata, **diagnostics}
    metadata.setdefault("strategy_profile", canonical_profile)
    metadata.setdefault("status_icon", "🐤")
    metadata.setdefault(
        "managed_symbols",
        _derive_managed_symbols(normalized_decision, runtime_metadata, allocation_payload=allocation_payload),
    )
    safe_haven_symbol = _derive_safe_haven_symbol(normalized_decision, runtime_metadata)
    if safe_haven_symbol:
        metadata.setdefault("safe_haven_symbol", safe_haven_symbol)
    metadata.setdefault("risk_flags", risk_flags)
    metadata.setdefault("actionable", not no_execute)
    if allocation_payload:
        metadata.setdefault("allocation", allocation_payload)
    execution_annotations = _derive_execution_annotations(diagnostics, runtime_metadata)
    if execution_annotations:
        metadata.setdefault("execution_annotations", execution_annotations)
    dashboard_text = str(
        execution_annotations.get("dashboard_text")
        or diagnostics.get("dashboard")
        or metadata.get("dashboard_text")
        or ""
    ).strip()
    if dashboard_text:
        metadata.setdefault("dashboard_text", dashboard_text)

    return target_weights, signal_desc, is_emergency, status_desc, metadata
