"""Top-level runtime composer for IBKR application wiring."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Mapping

from application.runtime_dependencies import IBKRRebalanceConfig, IBKRRebalanceRuntime
from application.paper_execution_admission import resolve_paper_execution_admission_enabled
from application.runtime_notification_adapters import build_runtime_notification_adapters
from application.runtime_reporting_adapters import build_runtime_reporting_adapters
from quant_platform_kit.common.execution_state import (
    build_execution_marker_store_from_env,
    resolve_execution_dedup_enabled,
)
from quant_platform_kit.common.runtime_assembly import build_runtime_assembly
from quant_platform_kit.common.runtime_target import build_runtime_context_fields
from quant_platform_kit.common.port_adapters import CallableNotificationPort, CallablePortfolioPort
from quant_platform_kit.common.runtime_target import RuntimeTarget
from quant_platform_kit.common.strategy_release import build_runtime_loaded_receipt


@dataclass(frozen=True)
class IBKRRuntimeComposer:
    service_name: str
    strategy_profile: str
    strategy_domain: str | None
    account_group: str
    project_id: str | None
    instance_name: str | None
    account_ids: tuple[str, ...]
    strategy_target_mode: str | None
    strategy_artifact_dir: str | None
    strategy_display_name: str
    strategy_display_name_localized: str
    managed_symbols: tuple[str, ...]
    signal_effective_after_trading_days: int | None
    signal_source: str
    status_icon: str
    safe_haven: str
    dry_run_only: bool
    strategy_config_source: str | None
    ib_gateway_host_resolver: Callable[[], str]
    ib_gateway_port: int
    ib_gateway_mode: str
    ib_gateway_ip_mode: str
    ib_client_id: int
    ib_connect_timeout_seconds: int
    feature_snapshot_path: str | None
    feature_snapshot_manifest_path: str | None
    strategy_config_path: str | None
    reconciliation_output_path: str | None
    translator: Callable[..., str]
    separator: str
    send_message: Callable[[str], None]
    connect_ib_fn: Callable[[], Any]
    build_portfolio_snapshot_fn: Callable[[Any], Any]
    compute_signals_fn: Callable[[Any, set[str]], tuple[Any, ...]]
    execute_rebalance_fn: Callable[..., Any]
    run_id_builder: Callable[[], str]
    event_logger: Callable[..., dict[str, Any]]
    report_builder: Callable[..., dict[str, Any]]
    report_persister: Callable[..., Any]
    trace_extractor: Callable[..., str | None]
    env_reader: Callable[[str, str], str | None]
    printer: Callable[..., Any] = print
    notification_builder: Callable[..., Any] = build_runtime_notification_adapters
    reporting_builder: Callable[..., Any] = build_runtime_reporting_adapters
    runtime_target: RuntimeTarget | None = None
    extra_reporting_fields: Mapping[str, Any] = field(default_factory=dict)
    notification_channel: str = "telegram"

    def build_notification_adapters(self, *, delivery_events: list[dict[str, Any]] | None = None):
        return self.notification_builder(
            send_message=self.send_message,
            notification_channel=self.notification_channel,
            log_message=lambda message: self.printer(message, flush=True),
            delivery_events=delivery_events,
        )

    def build_reporting_adapters(self):
        runtime_assembly = build_runtime_assembly(
            platform="interactive_brokers",
            deploy_target="cloud_run",
            service_name=self.service_name,
            strategy_profile=self.strategy_profile,
            runtime_target=self.runtime_target,
            account_scope=(
                self.runtime_target.account_scope
                if self.runtime_target and self.runtime_target.account_scope
                else self.account_group
            ),
            account_group=self.account_group,
            project_id=self.project_id,
            instance_name=self.instance_name,
            extra_context_fields=build_runtime_context_fields(
                {
                    "account_ids": list(self.account_ids),
                    "strategy_target_mode": self.strategy_target_mode,
                    "strategy_artifact_dir": self.strategy_artifact_dir,
                    "strategy_display_name": self.strategy_display_name,
                    "strategy_display_name_localized": self.strategy_display_name_localized,
                    **dict(self.extra_reporting_fields or {}),
                },
            ),
        )
        return self.reporting_builder(
            runtime_assembly=runtime_assembly,
            strategy_domain=self.strategy_domain,
            managed_symbols=self.managed_symbols,
            signal_source=self.signal_source,
            status_icon=self.status_icon,
            safe_haven=self.safe_haven,
            strategy_display_name=self.strategy_display_name,
            strategy_display_name_localized=self.strategy_display_name_localized,
            dry_run=self.dry_run_only,
            signal_effective_after_trading_days=self.signal_effective_after_trading_days,
            strategy_config_source=self.strategy_config_source,
            ib_gateway_host_resolver=self.ib_gateway_host_resolver,
            ib_gateway_port=self.ib_gateway_port,
            ib_gateway_mode=self.ib_gateway_mode,
            ib_gateway_ip_mode=self.ib_gateway_ip_mode,
            ib_client_id=self.ib_client_id,
            ib_connect_timeout_seconds=self.ib_connect_timeout_seconds,
            feature_snapshot_path=self.feature_snapshot_path,
            feature_snapshot_manifest_path=self.feature_snapshot_manifest_path,
            strategy_config_path=self.strategy_config_path,
            reconciliation_output_path=self.reconciliation_output_path,
            report_base_dir=self.env_reader("EXECUTION_REPORT_OUTPUT_DIR", ""),
            report_gcs_prefix_uri=self.env_reader("EXECUTION_REPORT_GCS_URI", ""),
            run_id_builder=self.run_id_builder,
            event_logger=self.event_logger,
            report_builder=self.report_builder,
            report_persister=self.report_persister,
            trace_extractor=self.trace_extractor,
            printer=lambda line: self.printer(line, flush=True),
        )

    def build_rebalance_runtime(
        self,
        *,
        silent_cycle_notifications: bool = False,
        notification_delivery_events: list[dict[str, Any]] | None = None,
    ):
        notification_adapters = self.build_notification_adapters(delivery_events=notification_delivery_events)
        notifications = (
            CallableNotificationPort(lambda _message: None)
            if silent_cycle_notifications
            else notification_adapters.notification_port
        )
        return IBKRRebalanceRuntime(
            connect_ib=self.connect_ib_fn,
            portfolio_port_factory=lambda ib: CallablePortfolioPort(
                lambda: self.build_portfolio_snapshot_fn(ib)
            ),
            compute_signals=self.compute_signals_fn,
            execute_rebalance=self.execute_rebalance_fn,
            notifications=notifications,
        )

    def build_rebalance_config(self, *, extra_notification_lines=(), cash_only_execution=True):
        execution_mode = "dry_run" if self.dry_run_only else str(
            self.ib_gateway_mode or "paper"
        ).strip().lower().replace("-", "_")
        execution_state_account_scope = (
            "PAPER" if self.dry_run_only else str(self.account_group or "LIVE").strip().upper()
        )
        return IBKRRebalanceConfig(
            translator=self.translator,
            separator=self.separator,
            strategy_display_name=self.strategy_display_name_localized,
            reconciliation_output_path=self.reconciliation_output_path,
            extra_notification_lines=tuple(extra_notification_lines or ()),
            cash_only_execution=bool(cash_only_execution),
            execution_mode=execution_mode,
            strategy_profile=self.strategy_profile,
            dry_run_only=self.dry_run_only,
            notify_no_trade_cycles=False,
            execution_dedup_enabled=resolve_execution_dedup_enabled(
                platform_env_prefix="IBKR",
                env_reader=self.env_reader,
                dry_run_only=self.dry_run_only,
                account_scope=execution_state_account_scope,
            ),
            execution_state_store=build_execution_marker_store_from_env(
                platform_env_prefix="IBKR",
                env_reader=self.env_reader,
                project_id=self.project_id,
            ),
            execution_state_account_scope=execution_state_account_scope,
            paper_execution_admission_enabled=resolve_paper_execution_admission_enabled(
                env_reader=self.env_reader,
                dry_run_only=self.dry_run_only,
                execution_mode=execution_mode,
            ),
            runtime_release_receipt=build_runtime_loaded_receipt(
                strategy_release=(
                    self.runtime_target.strategy_release if self.runtime_target is not None else None
                ),
            ),
            expected_strategy_release=(
                self.runtime_target.strategy_release if self.runtime_target is not None else None
            ),
        )


def build_runtime_composer(
    *,
    service_name: str,
    strategy_profile: str,
    strategy_domain: str | None,
    account_group: str,
    project_id: str | None,
    instance_name: str | None,
    account_ids: tuple[str, ...],
    strategy_target_mode: str | None,
    strategy_artifact_dir: str | None,
    strategy_display_name: str,
    strategy_display_name_localized: str,
    managed_symbols: tuple[str, ...],
    signal_effective_after_trading_days: int | None,
    signal_source: str,
    status_icon: str,
    safe_haven: str,
    dry_run_only: bool,
    strategy_config_source: str | None,
    ib_gateway_host_resolver: Callable[[], str],
    ib_gateway_port: int,
    ib_gateway_mode: str,
    ib_gateway_ip_mode: str,
    ib_client_id: int,
    ib_connect_timeout_seconds: int,
    feature_snapshot_path: str | None,
    feature_snapshot_manifest_path: str | None,
    strategy_config_path: str | None,
    reconciliation_output_path: str | None,
    translator: Callable[..., str],
    separator: str,
    send_message: Callable[[str], None],
    notification_channel: str = "telegram",
    connect_ib_fn: Callable[[], Any],
    build_portfolio_snapshot_fn: Callable[[Any], Any],
    compute_signals_fn: Callable[[Any, set[str]], tuple[Any, ...]],
    execute_rebalance_fn: Callable[..., Any],
    run_id_builder: Callable[[], str],
    event_logger: Callable[..., dict[str, Any]],
    report_builder: Callable[..., dict[str, Any]],
    report_persister: Callable[..., Any],
    trace_extractor: Callable[..., str | None],
    env_reader: Callable[[str, str], str | None],
    printer: Callable[..., Any] = print,
    extra_reporting_fields: Mapping[str, Any] | None = None,
    runtime_target: RuntimeTarget | None = None,
) -> IBKRRuntimeComposer:
    return IBKRRuntimeComposer(
        service_name=str(service_name or ""),
        strategy_profile=str(strategy_profile),
        strategy_domain=strategy_domain,
        account_group=str(account_group or ""),
        project_id=project_id,
        instance_name=instance_name,
        account_ids=tuple(account_ids),
        strategy_target_mode=strategy_target_mode,
        strategy_artifact_dir=strategy_artifact_dir,
        strategy_display_name=str(strategy_display_name or ""),
        strategy_display_name_localized=str(strategy_display_name_localized or ""),
        managed_symbols=tuple(managed_symbols),
        signal_effective_after_trading_days=signal_effective_after_trading_days,
        signal_source=str(signal_source or ""),
        status_icon=str(status_icon or ""),
        safe_haven=str(safe_haven or ""),
        dry_run_only=bool(dry_run_only),
        strategy_config_source=strategy_config_source,
        ib_gateway_host_resolver=ib_gateway_host_resolver,
        ib_gateway_port=int(ib_gateway_port),
        ib_gateway_mode=str(ib_gateway_mode or ""),
        ib_gateway_ip_mode=str(ib_gateway_ip_mode or ""),
        ib_client_id=int(ib_client_id),
        ib_connect_timeout_seconds=int(ib_connect_timeout_seconds),
        feature_snapshot_path=feature_snapshot_path,
        feature_snapshot_manifest_path=feature_snapshot_manifest_path,
        strategy_config_path=strategy_config_path,
        reconciliation_output_path=reconciliation_output_path,
        translator=translator,
        separator=str(separator or ""),
        send_message=send_message,
        notification_channel=notification_channel,
        connect_ib_fn=connect_ib_fn,
        build_portfolio_snapshot_fn=build_portfolio_snapshot_fn,
        compute_signals_fn=compute_signals_fn,
        execute_rebalance_fn=execute_rebalance_fn,
        run_id_builder=run_id_builder,
        event_logger=event_logger,
        report_builder=report_builder,
        report_persister=report_persister,
        trace_extractor=trace_extractor,
        env_reader=env_reader,
        printer=printer,
        runtime_target=runtime_target,
        extra_reporting_fields=dict(extra_reporting_fields or {}),
    )
