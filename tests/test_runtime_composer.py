import sys
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_platform_kit.common import build_runtime_target  # noqa: E402
from application.runtime_composer import IBKRRuntimeComposer  # noqa: E402


def test_runtime_composer_builds_runtime_and_config_from_local_builders():
    observed = {}

    def fake_notification_builder(**kwargs):
        observed["notification_builder"] = kwargs
        return SimpleNamespace(notification_port="notification-port")

    def fake_reporting_builder(**kwargs):
        observed["reporting_builder"] = kwargs
        return "reporting-adapters"

    composer = IBKRRuntimeComposer(
        service_name="interactive-brokers-platform",
        strategy_profile="global_etf_rotation",
        strategy_domain="us_equity",
        account_group="shared-live",
        project_id="project-1",
        instance_name="ib-gateway",
        account_ids=("U123456",),
        strategy_target_mode="weight",
        strategy_artifact_dir="/tmp/artifacts",
        strategy_display_name="Global ETF Rotation",
        strategy_display_name_localized="全球 ETF 轮动",
        managed_symbols=("AAA", "BIL"),
        signal_effective_after_trading_days=1,
        signal_source="market_data",
        status_icon="🐤",
        safe_haven="BIL",
        dry_run_only=True,
        runtime_target=build_runtime_target(
            platform_id="interactive_brokers",
            strategy_profile="global_etf_rotation",
            dry_run_only=True,
            account_scope="us-combo-shadow",
            service_name="interactive-brokers-platform",
        ),
        strategy_config_source="env",
        ib_gateway_host_resolver=lambda: "127.0.0.1",
        ib_gateway_port=4001,
        ib_gateway_mode="live",
        ib_gateway_ip_mode="internal",
        ib_client_id=1,
        ib_connect_timeout_seconds=60,
        feature_snapshot_path="/tmp/snapshot.csv",
        feature_snapshot_manifest_path="/tmp/snapshot.manifest.json",
        strategy_config_path="/tmp/config.json",
        reconciliation_output_path="/tmp/reconciliation.json",
        translator=lambda key, **_kwargs: key,
        separator="━━━━━━━━━━━━━━━━━━",
        send_message=lambda message: observed.setdefault("sent_message", message),
        connect_ib_fn=lambda: "ib-connection",
        build_portfolio_snapshot_fn=lambda ib: ("portfolio-snapshot", ib),
        compute_signals_fn="compute-signals",
        execute_rebalance_fn="execute-rebalance",
        run_id_builder=lambda: "run-001",
        event_logger="event-logger",
        report_builder="report-builder",
        report_persister="report-persister",
        trace_extractor="trace-extractor",
        env_reader=lambda name, default="": {
            "EXECUTION_REPORT_OUTPUT_DIR": "/tmp/runtime-reports",
            "EXECUTION_REPORT_GCS_URI": "gs://bucket/runtime-reports",
        }.get(name, default),
        printer=lambda *_args, **_kwargs: None,
        notification_builder=fake_notification_builder,
        reporting_builder=fake_reporting_builder,
    )

    notification_adapters = composer.build_notification_adapters()
    reporting_adapters = composer.build_reporting_adapters()
    runtime = composer.build_rebalance_runtime()
    silent_runtime = composer.build_rebalance_runtime(silent_cycle_notifications=True)
    config = composer.build_rebalance_config(extra_notification_lines=("plugin-line",))

    assert notification_adapters.notification_port == "notification-port"
    assert observed["notification_builder"]["send_message"]
    assert observed["reporting_builder"]["runtime_assembly"].account_scope == "us-combo-shadow"
    assert observed["reporting_builder"]["runtime_assembly"].account_group == "shared-live"
    assert observed["reporting_builder"]["managed_symbols"] == ("AAA", "BIL")
    assert observed["reporting_builder"]["signal_effective_after_trading_days"] == 1
    assert observed["reporting_builder"]["runtime_assembly"].runtime_target.platform_id == "interactive_brokers"
    assert observed["reporting_builder"]["runtime_assembly"].runtime_target.strategy_profile == "global_etf_rotation"
    assert observed["reporting_builder"]["runtime_assembly"].runtime_target.execution_mode == "paper"
    assert runtime.connect_ib() == "ib-connection"
    assert runtime.portfolio_port_factory("ib").get_portfolio_snapshot() == ("portfolio-snapshot", "ib")
    assert runtime.compute_signals == "compute-signals"
    assert runtime.execute_rebalance == "execute-rebalance"
    assert runtime.notifications == "notification-port"
    silent_runtime.notifications.send_text("precheck heartbeat")
    assert "sent_message" not in observed
    assert config.separator == "━━━━━━━━━━━━━━━━━━"
    assert config.strategy_display_name == "全球 ETF 轮动"
    assert config.reconciliation_output_path == "/tmp/reconciliation.json"
    assert config.extra_notification_lines == ("plugin-line",)
    assert config.notify_no_trade_cycles is False
    assert reporting_adapters == "reporting-adapters"


def test_runtime_composer_parks_live_without_durable_execution_claim_backend():
    class MinimalComposer(IBKRRuntimeComposer):
        pass

    fields = {
        "service_name": "ibkr",
        "strategy_profile": "profile",
        "strategy_domain": "us_equity",
        "account_group": "LIVE",
        "project_id": "project-1",
        "instance_name": "gw",
        "account_ids": ("U1",),
        "strategy_target_mode": "weight",
        "strategy_artifact_dir": "/tmp",
        "strategy_display_name": "Profile",
        "strategy_display_name_localized": "Profile",
        "managed_symbols": (),
        "signal_effective_after_trading_days": 1,
        "signal_source": "market_data",
        "status_icon": "*",
        "safe_haven": "BIL",
        "dry_run_only": False,
        "strategy_config_source": "env",
        "ib_gateway_host_resolver": lambda: "127.0.0.1",
        "ib_gateway_port": 4001,
        "ib_gateway_mode": "live",
        "ib_gateway_ip_mode": "internal",
        "ib_client_id": 1,
        "ib_connect_timeout_seconds": 60,
        "feature_snapshot_path": None,
        "feature_snapshot_manifest_path": None,
        "strategy_config_path": None,
        "reconciliation_output_path": "/tmp/reconciliation.json",
        "translator": lambda key, **_kwargs: key,
        "separator": "-",
        "send_message": lambda _message: None,
        "connect_ib_fn": lambda: None,
        "build_portfolio_snapshot_fn": lambda _ib: None,
        "compute_signals_fn": None,
        "execute_rebalance_fn": None,
        "run_id_builder": lambda: "run",
        "event_logger": None,
        "report_builder": None,
        "report_persister": None,
        "trace_extractor": None,
        "env_reader": lambda _name, default="": default,
    }
    composer = MinimalComposer(**fields)

    try:
        composer.build_rebalance_config()
    except RuntimeError as exc:
        assert "requires a gs:// execution state URI" in str(exc)
    else:
        raise AssertionError("live execution must fail closed without durable atomic claims")

    dry = replace(composer, dry_run_only=True)
    dry_config = dry.build_rebalance_config()
    assert dry_config.dry_run_only is True
    assert not str(dry_config.execution_state_store.cloud_prefix_uri or "").startswith("gs://")
