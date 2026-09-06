"""Runtime dependency bundles for IBKR rebalance orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant_platform_kit.common.ports import NotificationPort, PortfolioPort


@dataclass(frozen=True)
class IBKRRebalanceConfig:
    translator: Callable[..., str]
    separator: str
    strategy_display_name: str | None = None
    reconciliation_output_path: str | Path | None = None
    extra_notification_lines: tuple[str, ...] = ()
    cash_only_execution: bool = True
    execution_mode: str = "paper"
    strategy_profile: str = ""
    dry_run_only: bool = False
    notify_no_trade_cycles: bool = True
    execution_dedup_enabled: bool = False
    execution_state_store: Any = None
    execution_state_account_scope: str = ""
    account_ids: tuple[str, ...] = ()
    paper_execution_admission_enabled: bool = False
    runtime_release_receipt: Any = None
    expected_strategy_release: Any = None


@dataclass(frozen=True)
class IBKRRebalanceRuntime:
    connect_ib: Callable[[], Any]
    portfolio_port_factory: Callable[[Any], PortfolioPort]
    compute_signals: Callable[[Any, set[str]], tuple[Any, ...]]
    execute_rebalance: Callable[..., Any]
    notifications: NotificationPort
