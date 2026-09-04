"""Builder helpers for IBKR runtime reporting and structured logging."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from quant_platform_kit.common.runtime_assembly import RuntimeAssembly
from quant_platform_kit.common.strategy_contracts import build_execution_timing_metadata
from quant_platform_kit.common.runtime_target import RuntimeTarget
from runtime_logging import RuntimeLogContext


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IBKRRuntimeReportingAdapters:
    runtime_assembly: RuntimeAssembly
    strategy_domain: str | None
    runtime_target: RuntimeTarget | None = None
    extra_context_fields: Mapping[str, Any] = field(default_factory=dict)
    managed_symbols: tuple[str, ...] = ()
    signal_source: str = ""
    status_icon: str = ""
    safe_haven: str = ""
    strategy_display_name: str = ""
    strategy_display_name_localized: str = ""
    dry_run: bool = False
    signal_effective_after_trading_days: int | None = None
    strategy_config_source: str | None = None
    ib_gateway_host_resolver: Callable[[], str] | None = None
    ib_gateway_port: int = 0
    ib_gateway_mode: str = ""
    ib_gateway_ip_mode: str = ""
    ib_client_id: int = 0
    ib_connect_timeout_seconds: int = 0
    feature_snapshot_path: str | None = None
    feature_snapshot_manifest_path: str | None = None
    strategy_config_path: str | None = None
    reconciliation_output_path: str | None = None
    report_base_dir: str | None = None
    report_gcs_prefix_uri: str | None = None
    run_id_builder: Callable[[], str] | None = None
    event_logger: Callable[..., dict[str, Any]] | None = None
    report_builder: Callable[..., dict[str, Any]] | None = None
    report_persister: Callable[..., Any] | None = None
    trace_extractor: Callable[..., str | None] | None = None
    printer: Callable[..., Any] = print
    clock: Callable[[], datetime] = _utcnow

    def __post_init__(self) -> None:
        required = {
            "ib_gateway_host_resolver": self.ib_gateway_host_resolver,
            "run_id_builder": self.run_id_builder,
            "event_logger": self.event_logger,
            "report_builder": self.report_builder,
            "report_persister": self.report_persister,
            "trace_extractor": self.trace_extractor,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"Missing reporting adapter dependencies: {', '.join(missing)}")

    def build_log_context(self, *, trace_header: str | None = None) -> RuntimeLogContext:
        return self.runtime_assembly.with_overrides(
            runtime_target=self.runtime_target,
            extra_context_fields=self.extra_context_fields,
        ).build_log_context(
            run_id=self.run_id_builder(),
            trace=self.trace_extractor(self.runtime_assembly.project_id, trace_header),
        )

    def build_report(self, log_context: RuntimeLogContext) -> dict[str, Any]:
        started_at = self.clock()
        timing_summary = build_execution_timing_metadata(
            signal_date=started_at,
            signal_effective_after_trading_days=self.signal_effective_after_trading_days,
        )
        return self.report_builder(
            **self.runtime_assembly.with_overrides(
                runtime_target=self.runtime_target,
                extra_context_fields=self.extra_context_fields,
            ).build_report_base_kwargs(
                run_id=log_context.run_id,
                dry_run=self.dry_run,
                started_at=started_at,
                strategy_domain=self.strategy_domain,
            ),
            summary={
                "account_ids": list(self.extra_context_fields.get("account_ids") or ()),
                "managed_symbols": list(self.managed_symbols),
                "signal_source": self.signal_source,
                "status_icon": self.status_icon,
                "safe_haven": self.safe_haven,
                "strategy_display_name": self.strategy_display_name,
                "strategy_display_name_localized": self.strategy_display_name_localized,
                **timing_summary,
            },
            diagnostics={
                "strategy_config_source": self.strategy_config_source,
                "ib_gateway_host": self.ib_gateway_host_resolver(),
                "ib_gateway_port": self.ib_gateway_port,
                "ib_gateway_mode": self.ib_gateway_mode,
                "ib_gateway_ip_mode": self.ib_gateway_ip_mode,
                "ib_client_id": self.ib_client_id,
                "ib_connect_timeout_seconds": self.ib_connect_timeout_seconds,
            },
            artifacts={
                "feature_snapshot_path": self.feature_snapshot_path,
                "feature_snapshot_manifest_path": self.feature_snapshot_manifest_path,
                "strategy_config_path": self.strategy_config_path,
                "reconciliation_output_path": self.reconciliation_output_path,
            },
        )

    def start_request_run(self, *, trace_header: str | None = None) -> tuple[RuntimeLogContext, dict[str, Any]]:
        log_context = self.build_log_context(trace_header=trace_header)
        return log_context, self.build_report(log_context)

    def log_event(self, log_context: RuntimeLogContext, event: str, **fields: Any) -> dict[str, Any]:
        return self.event_logger(
            log_context,
            event,
            printer=self.printer,
            **fields,
        )

    def persist_execution_report(self, report: dict[str, Any]) -> str | None:
        persisted = self.report_persister(
            report,
            base_dir=self.report_base_dir,
            cloud_prefix_uri=self.report_gcs_prefix_uri,
            project_id=self.runtime_assembly.project_id,
        )
        if isinstance(persisted, str):
            return persisted
        return getattr(persisted, "gcs_uri", None) or getattr(persisted, "local_path", None)


def build_runtime_reporting_adapters(
    *,
    runtime_assembly: RuntimeAssembly,
    strategy_domain: str | None,
    runtime_target: RuntimeTarget | None = None,
    extra_context_fields: Mapping[str, Any] | None = None,
    managed_symbols: tuple[str, ...],
    signal_source: str,
    status_icon: str,
    safe_haven: str,
    strategy_display_name: str,
    strategy_display_name_localized: str,
    dry_run: bool,
    signal_effective_after_trading_days: int | None,
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
    report_base_dir: str | None,
    report_gcs_prefix_uri: str | None,
    run_id_builder: Callable[[], str],
    event_logger: Callable[..., dict[str, Any]],
    report_builder: Callable[..., dict[str, Any]],
    report_persister: Callable[..., Any],
    trace_extractor: Callable[..., str | None],
    printer: Callable[..., Any] = print,
    clock: Callable[[], datetime] = _utcnow,
) -> IBKRRuntimeReportingAdapters:
    return IBKRRuntimeReportingAdapters(
        runtime_assembly=runtime_assembly,
        strategy_domain=strategy_domain,
        runtime_target=runtime_target,
        extra_context_fields=dict(extra_context_fields or {}),
        managed_symbols=tuple(managed_symbols),
        signal_source=str(signal_source or ""),
        status_icon=str(status_icon or ""),
        safe_haven=str(safe_haven or ""),
        strategy_display_name=str(strategy_display_name or ""),
        strategy_display_name_localized=str(strategy_display_name_localized or ""),
        dry_run=bool(dry_run),
        signal_effective_after_trading_days=signal_effective_after_trading_days,
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
        report_base_dir=report_base_dir,
        report_gcs_prefix_uri=report_gcs_prefix_uri,
        run_id_builder=run_id_builder,
        event_logger=event_logger,
        report_builder=report_builder,
        report_persister=report_persister,
        trace_extractor=trace_extractor,
        printer=printer,
        clock=clock,
    )
