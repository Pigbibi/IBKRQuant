from __future__ import annotations

from quant_platform_kit.common.platform_runner.loader import (
    load_strategy_definition as _qpk_load_definition,
    load_strategy_entrypoint_for_profile as _qpk_load_entrypoint,
)
from quant_platform_kit.common.strategies import StrategyDefinition
from quant_platform_kit.common.strategy_contracts import (
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
)

from strategy_registry import (
    IBKR_PLATFORM,
    PLATFORM_POLICY,
    STRATEGY_CATALOG,
    get_platform_runtime_adapter,
)


def load_strategy_definition(raw_profile: str | None) -> StrategyDefinition:
    # Delegates to shared QPK loader — kept as thin wrapper for import compatibility.
    return _qpk_load_definition(
        raw_profile,
        platform_id=IBKR_PLATFORM,
        strategy_catalog=STRATEGY_CATALOG,
        policy=PLATFORM_POLICY,
    )


def load_strategy_entrypoint_for_profile(raw_profile: str | None) -> StrategyEntrypoint:
    runtime_adapter = load_strategy_runtime_adapter_for_profile(raw_profile)
    return _qpk_load_entrypoint(
        raw_profile,
        platform_id=IBKR_PLATFORM,
        strategy_catalog=STRATEGY_CATALOG,
        policy=PLATFORM_POLICY,
        runtime_adapter=runtime_adapter,
    )


def load_strategy_runtime_adapter_for_profile(raw_profile: str | None) -> StrategyRuntimeAdapter:
    definition = load_strategy_definition(raw_profile)
    return get_platform_runtime_adapter(
        definition.profile,
        platform_id=IBKR_PLATFORM,
    )
