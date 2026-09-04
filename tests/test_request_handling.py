import json
import types

import pytest

import application.execution_receipt_adapter as execution_receipt_adapter
from application.cycle_result import StrategyCycleResult


@pytest.fixture(autouse=True)
def isolate_cycle_receipt_contract(monkeypatch):
    # Receipt semantics have dedicated adapter tests; these tests isolate HTTP routing/reporting.
    monkeypatch.setattr(
        execution_receipt_adapter,
        "attach_cycle_execution_receipt",
        lambda *_args, **_kwargs: None,
    )


def route_methods(strategy_module):
    return {
        rule.rule: sorted(rule.methods - {"HEAD", "OPTIONS"})
        for rule in strategy_module.app.url_map.iter_rules()
    }


def test_cloud_run_route_contracts_are_registered(strategy_module):
    assert route_methods(strategy_module) == {
        "/run": ["POST"],
        "/dry-run": ["GET", "POST"],
        "/paper-command-consumer": ["POST"],
        "/probe": ["POST"],
        "/reconcile": ["POST"],
        "/monitor-dispatch": ["GET", "POST"],
        "/health": ["GET"],
        "/healthz": ["GET"],
        "/static/<path:filename>": ["GET"],
    }


def test_paper_command_consumer_does_not_open_gateway_without_due_command(
    strategy_module_factory,
    monkeypatch,
    tmp_path,
):
    strategy_module = strategy_module_factory(
        IBKR_DRY_RUN_ONLY="true",
        RUNTIME_TARGET_ENABLED="false",
        IBKR_PAPER_EXECUTION_COMMAND_CONSUMER_ENABLED="true",
        IBKR_EXECUTION_COMMAND_DIR=str(tmp_path / "commands"),
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
            '"ib_gateway_mode":"paper","ib_client_id":1,"account_ids":["DU123"]}}}'
        ),
        RUNTIME_TARGET_JSON=json.dumps(
            {
                "platform_id": "ibkr",
                "strategy_profile": "global_etf_rotation",
                "account_scope": "paper",
                "dry_run_only": True,
                "execution_mode": "paper",
            },
            separators=(",", ":"),
        ),
    )
    observed = {"gateway": 0}

    class ReportingAdapters:
        def build_log_context(self, **_kwargs):
            return object()

        def build_report(self, _context):
            return {}

        def log_event(self, *_args, **_kwargs):
            return None

        def persist_execution_report(self, _report):
            return "/tmp/report.json"

    monkeypatch.setattr(
        strategy_module,
        "build_composer",
        lambda **_kwargs: types.SimpleNamespace(build_reporting_adapters=lambda: ReportingAdapters()),
    )

    def fail_if_gateway_opens(**_kwargs):
        observed["gateway"] += 1
        raise AssertionError("no due command must not open IBKR Gateway")

    monkeypatch.setattr(strategy_module, "build_broker_adapters", fail_if_gateway_opens)

    with strategy_module.app.test_request_context("/paper-command-consumer", method="POST"):
        body, status, _headers = strategy_module.handle_paper_execution_command_consumer()

    assert status == 200
    assert json.loads(body)["status"] == "blocked"
    assert observed["gateway"] == 0


def test_health_route_returns_ok(strategy_module):
    with strategy_module.app.test_request_context("/health", method="GET"):
        body, status = strategy_module.health()

    assert status == 200
    assert body == "OK"


def test_platform_notification_prefix_stays_empty_for_default_account(strategy_module):
    assert strategy_module._platform_notification_prefix() == ""
    assert strategy_module._with_platform_notification_prefix("hello") == "hello"


def test_platform_notification_prefix_uses_ibkr_account_scope(strategy_module_factory):
    module = strategy_module_factory(
        ACCOUNT_GROUP="live-u1234567",
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"live-u1234567":{"ib_gateway_instance_name":"127.0.0.1",'
            '"ib_gateway_mode":"live","ib_client_id":1,"account_ids":["U1234567"]}}}'
        ),
        RUNTIME_TARGET_JSON=json.dumps(
            {
                "platform_id": "ibkr",
                "strategy_profile": "global_etf_rotation",
                "dry_run_only": False,
                "execution_mode": "live",
                "account_selector": ["U1234567"],
                "account_scope": "live-u1234567",
            },
            separators=(",", ":"),
        ),
    )

    assert module._platform_notification_prefix() == "[U1234567]"
    assert module._with_platform_notification_prefix("hello") == "[U1234567] hello"


def test_handle_request_get_is_method_not_allowed(strategy_module, monkeypatch):
    def fail_if_called():
        raise AssertionError("GET should not execute strategy")

    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)

    response = strategy_module.app.test_client().get("/run")
    with strategy_module.app.test_request_context("/run", method="GET"):
        body, status = strategy_module.handle_request()

    assert response.status_code == 405
    assert (body, status) == ("Method Not Allowed", 405)


def test_handle_probe_get_is_method_not_allowed(strategy_module, monkeypatch):
    monkeypatch.setattr(
        strategy_module,
        "_handle_probe",
        lambda: (_ for _ in ()).throw(AssertionError("GET should not connect to the broker")),
    )

    response = strategy_module.app.test_client().get("/probe")
    with strategy_module.app.test_request_context("/probe", method="GET"):
        body, status = strategy_module.handle_probe()

    assert response.status_code == 405
    assert (body, status) == ("Method Not Allowed", 405)


def test_handle_request_post_executes_on_market_day(strategy_module, monkeypatch):
    observed = {"called": False}

    def fake_run_strategy_core(**_kwargs):
        observed["called"] = True
        return "OK - executed"

    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)
    monkeypatch.setattr(strategy_module, "attach_cycle_execution_receipt", lambda *_args, **_kwargs: None)

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["called"] is True


def test_handle_request_sends_escalated_strategy_plugin_alert(strategy_module, monkeypatch):
    signal = types.SimpleNamespace(
        plugin="crisis_response_shadow",
        effective_mode="shadow",
        canonical_route="true_crisis",
        suggested_action="defend",
        would_trade_if_enabled=True,
        as_of="2026-05-24",
    )
    observed = {"alerts": []}

    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((signal,), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)

    def fake_dispatch(signals, **kwargs):
        observed["alerts"].append((tuple(signals), kwargs))
        return types.SimpleNamespace(attach_to_report=lambda _report: None)

    monkeypatch.setattr(strategy_module, "dispatch_strategy_plugin_alerts", fake_dispatch)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - executed")

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert len(observed["alerts"]) == 1
    assert observed["alerts"][0][0] == (signal,)
    assert "ibkr" in observed["alerts"][0][1]["context_label"]
    assert observed["alerts"][0][1]["notification_settings"] is strategy_module.RUNTIME_SETTINGS
    assert observed["alerts"][0][1]["state_settings"] is not None


def test_handle_dry_run_post_uses_dry_run_override(strategy_module, monkeypatch):
    observed = {"called": False, "dry_run_only_override": None, "events": []}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json")
    monkeypatch.setattr(strategy_module, "emit_runtime_log", lambda context, event, **fields: observed["events"].append((event, fields)))
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)

    def fake_run_strategy_core(**kwargs):
        observed["called"] = True
        observed["dry_run_only_override"] = kwargs.get("dry_run_only_override")
        return "OK - dry-run"

    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/dry-run", method="POST"):
        body, status = strategy_module.handle_dry_run()

    assert status == 200
    assert body == "Dry Run OK"
    assert observed["called"] is True
    assert observed["dry_run_only_override"] is True
    assert observed["events"][0][0] == "strategy_cycle_received"
    assert observed["events"][0][1]["execution_window"] == "dry_run"


def test_handle_dry_run_returns_service_unavailable_before_platform_timeout(strategy_module, monkeypatch):
    observed = {"recycles": 0}

    class ExpiredDeadline:
        def __enter__(self):
            raise strategy_module.RuntimeDeadlineExceeded("dry-run deadline exceeded")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(strategy_module, "enforce_runtime_deadline", lambda *_args, **_kwargs: ExpiredDeadline())
    monkeypatch.setattr(
        strategy_module,
        "recycle_current_process_after_response",
        lambda: observed.__setitem__("recycles", observed["recycles"] + 1),
    )

    with strategy_module.app.test_request_context("/dry-run", method="POST"):
        body, status = strategy_module.handle_dry_run()

    assert status == 503
    assert body == "Error"
    assert observed["recycles"] == 1


def test_handle_dry_run_deadline_from_strategy_persists_once_and_recycles(strategy_module, monkeypatch):
    observed = {"persist_calls": 0, "recycles": 0}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda *_args, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(strategy_module, "log_runtime_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: (_ for _ in ()).throw(
            strategy_module.RuntimeDeadlineExceeded("strategy deadline exceeded")
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda *_args, **_kwargs: observed.__setitem__("persist_calls", observed["persist_calls"] + 1),
    )
    monkeypatch.setattr(
        strategy_module,
        "recycle_current_process_after_response",
        lambda: observed.__setitem__("recycles", observed["recycles"] + 1),
    )

    with strategy_module.app.test_request_context("/dry-run", method="POST"):
        body, status = strategy_module.handle_dry_run()

    assert status == 503
    assert body == "Error"
    assert observed["persist_calls"] == 1
    assert observed["recycles"] == 1


def test_handle_dry_run_deadline_during_plugin_setup_persists_report(strategy_module, monkeypatch):
    observed = {"reports": [], "recycles": 0}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda *_args, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(
        strategy_module,
        "load_strategy_plugin_signals",
        lambda: (_ for _ in ()).throw(
            strategy_module.RuntimeDeadlineExceeded("plugin setup deadline exceeded")
        ),
    )
    monkeypatch.setattr(strategy_module, "log_runtime_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report, **_kwargs: observed["reports"].append(dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "recycle_current_process_after_response",
        lambda: observed.__setitem__("recycles", observed["recycles"] + 1),
    )

    with strategy_module.app.test_request_context("/dry-run", method="POST"):
        body, status = strategy_module.handle_dry_run()

    assert status == 503
    assert body == "Error"
    assert observed["recycles"] == 1
    assert len(observed["reports"]) == 1
    assert observed["reports"][0]["status"] == "error"
    assert observed["reports"][0]["diagnostics"]["failure_category"] == "strategy_deadline"


def test_successful_dry_run_report_timeout_does_not_flip_strategy_result(strategy_module, monkeypatch):
    observed = {"persist_calls": 0, "recycles": 0}

    class DeadlineContext:
        def __init__(self, operation):
            self.operation = operation

        def __enter__(self):
            if "report persistence" in self.operation:
                raise strategy_module.RuntimeDeadlineExceeded("report persistence deadline exceeded")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        strategy_module,
        "enforce_runtime_deadline",
        lambda _seconds, *, operation: DeadlineContext(operation),
    )
    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda *_args, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(strategy_module, "log_runtime_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - dry-run")
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda *_args, **_kwargs: observed.__setitem__("persist_calls", observed["persist_calls"] + 1),
    )
    monkeypatch.setattr(
        strategy_module,
        "recycle_current_process_after_response",
        lambda: observed.__setitem__("recycles", observed["recycles"] + 1),
    )

    with strategy_module.app.test_request_context("/dry-run", method="POST"):
        body, status = strategy_module.handle_dry_run()

    assert status == 200
    assert body == "Dry Run OK"
    assert observed == {"persist_calls": 0, "recycles": 0}


def test_dry_run_composer_wires_dry_run_override_to_order_execution(strategy_module, monkeypatch):
    observed = {}

    class FakeBrokerAdapters:
        def execute_rebalance(self, *_args, **_kwargs):
            observed["called"] = True
            return (), {"mode": "dry_run"}

    def fake_build_broker_adapters(*, dry_run_only_override=None):
        observed["dry_run_only_override"] = dry_run_only_override
        return FakeBrokerAdapters()

    monkeypatch.setattr(strategy_module, "build_broker_adapters", fake_build_broker_adapters)

    runtime = strategy_module.build_composer(dry_run_only_override=True).build_rebalance_runtime()
    runtime.execute_rebalance("ib", {}, {}, {}, strategy_symbols=(), signal_metadata={})

    assert observed["called"] is True
    assert observed["dry_run_only_override"] is True


def test_handle_dry_run_ignores_paper_liquidate_only(strategy_module, monkeypatch):
    observed = {"called": False}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_kwargs: "/tmp/runtime-report.json")
    monkeypatch.setattr(strategy_module, "emit_runtime_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategy_module, "PAPER_LIQUIDATE_ONLY", True)

    def fake_run_strategy_core(**kwargs):
        observed["called"] = True
        assert kwargs.get("dry_run_only_override") is True
        return "OK - dry-run"

    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/dry-run", method="POST"):
        body, status = strategy_module.handle_dry_run()

    assert status == 200
    assert body == "Dry Run OK"
    assert observed["called"] is True


def test_handle_dry_run_get_does_not_execute(strategy_module, monkeypatch):
    observed = {"called": False}

    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: observed.__setitem__("called", True))

    with strategy_module.app.test_request_context("/dry-run", method="GET"):
        body, status = strategy_module.handle_dry_run()

    assert status == 200
    assert body == "Dry Run OK - use POST to run strategy dry-run"
    assert observed["called"] is False


def test_handle_monitor_dispatch_post_dispatches_due_targets(strategy_module, monkeypatch):
    observed = {"events": []}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "load_monitor_targets", lambda: [{"service_name": "svc"}])

    def fake_dispatch(targets, **kwargs):
        observed["targets"] = targets
        observed["kwargs"] = kwargs
        return {"ok": True, "dispatches_due": 1, "dispatches_sent": 1, "results": [{"service_name": "svc"}]}

    monkeypatch.setattr(strategy_module, "dispatch_due_monitor_targets", fake_dispatch)
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )

    with strategy_module.app.test_request_context("/monitor-dispatch", method="POST"):
        body, status = strategy_module.handle_monitor_dispatch()

    assert status == 200
    assert body["dispatches_sent"] == 1
    assert observed["targets"] == [{"service_name": "svc"}]
    assert observed["events"][0][0] == "monitor_dispatch_completed"


def test_handle_monitor_dispatch_returns_bad_gateway_when_a_target_fails(strategy_module, monkeypatch):
    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "load_monitor_targets", lambda: [{"service_name": "svc"}])
    monkeypatch.setattr(
        strategy_module,
        "dispatch_due_monitor_targets",
        lambda *_args, **_kwargs: {
            "ok": False,
            "dispatches_due": 1,
            "dispatches_sent": 1,
            "results": [{"service_name": "svc", "status_code": 503, "ok": False}],
        },
    )
    monkeypatch.setattr(strategy_module, "log_runtime_event", lambda *_args, **_kwargs: None)

    with strategy_module.app.test_request_context("/monitor-dispatch", method="POST"):
        body, status = strategy_module.handle_monitor_dispatch()

    assert status == 502
    assert body["ok"] is False


def test_handle_monitor_dispatch_recycles_only_after_local_timeout_is_aggregated(strategy_module, monkeypatch):
    observed = {"recycles": 0, "logged": False}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "load_monitor_targets", lambda: [{"service_name": "host-service"}])
    monkeypatch.setattr(
        strategy_module,
        "dispatch_due_monitor_targets",
        lambda *_args, **_kwargs: {
            "ok": False,
            "dispatches_due": 1,
            "dispatches_sent": 1,
            "results": [
                {
                    "service_name": "host-service",
                    "status_code": 503,
                    "ok": False,
                    "worker_recycle_required": True,
                }
            ],
        },
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda *_args, **_kwargs: observed.__setitem__("logged", True),
    )
    monkeypatch.setattr(
        strategy_module,
        "recycle_current_process_after_response",
        lambda: observed.__setitem__("recycles", observed["recycles"] + 1),
    )

    with strategy_module.app.test_request_context("/monitor-dispatch", method="POST"):
        body, status = strategy_module.handle_monitor_dispatch()

    assert status == 502
    assert body["results"][0]["worker_recycle_required"] is True
    assert observed == {"recycles": 1, "logged": True}


def test_local_precheck_defers_worker_recycle_to_monitor_dispatch(strategy_module, monkeypatch):
    observed = {"calls": 0}

    def fake_dry_run(*, recycle_on_timeout, timeout_state):
        observed["calls"] += 1
        assert recycle_on_timeout is False
        timeout_state["worker_recycle_required"] = True
        return "Error", 503

    monkeypatch.setattr(strategy_module, "_handle_dry_run_with_deadline", fake_dry_run)

    result = strategy_module._dispatch_local_monitor({"window": "precheck"})

    assert result == {
        "status_code": 503,
        "worker_recycle_required": True,
    }
    assert observed["calls"] == 1


def test_handle_probe_checks_account_snapshot_without_success_notification(strategy_module, monkeypatch):
    observed = {"events": [], "disconnects": 0, "notifications": [], "connection_options": []}

    class FakeIB:
        def disconnect(self):
            observed["disconnects"] += 1

    snapshot = types.SimpleNamespace(
        buying_power=123.0,
        total_equity=456.0,
        positions=(types.SimpleNamespace(symbol="SOXL"),),
    )

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(
        strategy_module,
        "load_strategy_plugin_signals",
        lambda: (_ for _ in ()).throw(AssertionError("health probe should not load strategy plugins")),
    )
    monkeypatch.setattr(
        strategy_module,
        "attach_strategy_plugin_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("health probe should not attach strategy plugin reports")),
    )
    def fake_connect_ib(*, read_only=False, validate_trading_permissions=True):
        observed["connection_options"].append(
            {
                "read_only": read_only,
                "validate_trading_permissions": validate_trading_permissions,
            }
        )
        return FakeIB()

    monkeypatch.setattr(strategy_module, "connect_ib", fake_connect_ib)
    monkeypatch.setattr(strategy_module, "build_portfolio_snapshot", lambda ib: snapshot)
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **_kwargs: observed["notifications"].append(_kwargs),
    )

    with strategy_module.app.test_request_context("/probe", method="POST"):
        body, status = strategy_module.handle_probe()

    assert status == 200
    assert body == "Probe OK"
    assert [event for event, _fields in observed["events"]] == [
        "health_probe_received",
        "health_probe_completed",
    ]
    assert observed["report"]["status"] == "ok"
    assert observed["report"]["summary"]["buying_power"] == 123.0
    assert observed["report"]["summary"]["total_equity"] == 456.0
    assert observed["report"]["summary"]["positions_count"] == 1
    assert observed["disconnects"] == 1
    assert observed["notifications"] == []
    assert observed["connection_options"] == [
        {"read_only": True, "validate_trading_permissions": False}
    ]


def test_handle_probe_connect_timeout_sends_concise_connection_notification(strategy_module, monkeypatch):
    observed = {"events": [], "notifications": []}
    timeout_message = (
        "IBKR API handshake timed out after TCP preflight succeeded "
        "for 172.16.159.2:4001 clientId=231"
    )

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(
        strategy_module,
        "load_strategy_plugin_signals",
        lambda: (_ for _ in ()).throw(AssertionError("health probe should not load strategy plugins")),
    )
    monkeypatch.setattr(
        strategy_module,
        "attach_strategy_plugin_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("health probe should not attach strategy plugin reports")),
    )
    monkeypatch.setattr(
        strategy_module,
        "connect_ib",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError(timeout_message)),
    )
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with strategy_module.app.test_request_context("/probe", method="POST"):
        body, status = strategy_module.handle_probe()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "health_probe"
    assert observed["report"]["errors"][0]["failure_category"] == "ibkr_connection"
    assert [event for event, _fields in observed["events"]] == [
        "health_probe_received",
        "health_probe_failed",
    ]
    assert observed["events"][1][1]["failure_category"] == "ibkr_connection"
    assert len(observed["notifications"]) == 1
    detailed_text = observed["notifications"][0]["detailed_text"]
    assert "IBKR" in detailed_text
    assert timeout_message in detailed_text
    assert "Traceback" not in detailed_text


def test_handle_probe_failure_sends_notification(strategy_module, monkeypatch):
    observed = {"events": [], "notifications": []}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json")
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        strategy_module,
        "connect_ib",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with strategy_module.app.test_request_context("/probe", method="POST"):
        body, status = strategy_module.handle_probe()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "health_probe"
    assert [event for event, _fields in observed["events"]] == [
        "health_probe_received",
        "health_probe_failed",
    ]
    assert len(observed["notifications"]) == 1
    assert "probe failed" in observed["notifications"][0]["detailed_text"]


def test_build_extra_notification_lines_includes_account_id(strategy_module):
    lines = strategy_module.build_extra_notification_lines(("plugin-line",))
    assert any("U1234567" in line for line in lines)
    assert all("plugin-line" not in line for line in lines)


def test_build_extra_notification_lines_includes_plugin_error(strategy_module_factory):
    strategy_module = strategy_module_factory(NOTIFY_LANG="zh")
    lines = strategy_module.build_extra_notification_lines(
        (),
        strategy_plugin_error="ValueError: bad config",
    )
    assert any("插件信号未加载" in line for line in lines)
    assert any("插件配置校验失败" in line for line in lines)


def test_handle_request_skips_overlapping_post(strategy_module, monkeypatch):
    observed = {}

    def fail_if_called():
        raise AssertionError("overlapping request should not execute strategy")

    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    strategy_module.STRATEGY_RUN_LOCK.acquire()
    try:
        with strategy_module.app.test_request_context("/run", method="POST"):
            body, status = strategy_module.handle_request()
    finally:
        strategy_module.STRATEGY_RUN_LOCK.release()

    assert status == 200
    assert body == "Already Running"
    assert observed["report"]["status"] == "skipped"
    assert observed["report"]["diagnostics"]["skip_reason"] == "already_running"


def test_handle_request_emits_structured_runtime_events(strategy_module, monkeypatch):
    observed = []

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(
        strategy_module,
        "emit_runtime_log",
        lambda context, event, **fields: observed.append((context.run_id, event, fields)),
    )
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - executed")

    with strategy_module.app.test_request_context(
        "/",
        method="POST",
        headers={"X-Cloud-Trace-Context": "trace-123/1;o=1"},
    ):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert [event for _run_id, event, _fields in observed] == [
        "strategy_cycle_received",
        "strategy_cycle_started",
        "strategy_cycle_completed",
    ]
    assert all(run_id == "run-001" for run_id, _event, _fields in observed)
    assert observed[0][2]["http_method"] == "POST"


def test_handle_request_persists_machine_readable_report(strategy_module, monkeypatch):
    observed = {}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - executed")
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["report"]["status"] == "ok"
    assert observed["report"]["strategy_profile"] == strategy_module.STRATEGY_PROFILE
    assert observed["report"]["summary"]["strategy_display_name"] == strategy_module.STRATEGY_DISPLAY_NAME
    assert observed["report"]["summary"]["strategy_display_name_localized"] == strategy_module.strategy_display_name
    assert observed["report"]["run_source"] == "cloud_run"
    assert observed["report"]["account_scope"] == strategy_module.ACCOUNT_GROUP
    assert observed["report"]["summary"]["signal_source"] == strategy_module.STRATEGY_SIGNAL_SOURCE
    assert observed["report"]["summary"]["execution_timing_contract"] == "next_trading_day"
    assert observed["report"]["summary"]["signal_date"]
    assert observed["report"]["summary"]["effective_date"]


def test_execution_report_prefers_configured_managed_symbols_without_ranking_pool(strategy_module_factory):
    module = strategy_module_factory(STRATEGY_PROFILE="soxl_soxx_trend_income")
    report = module.build_execution_report(module.RUNTIME_LOG_CONTEXT.with_run("run-001"))

    assert report["summary"]["managed_symbols"] == [
        "SOXL",
        "SOXX",
        "BOXX",
        "SCHD",
        "DGRO",
        "SGOV",
        "SPYI",
        "QQQI",
    ]
    assert report["summary"]["safe_haven"] == "BIL"
    assert report["summary"]["execution_timing_contract"] == "next_trading_day"


def test_handle_request_enriches_runtime_report_with_cycle_details(strategy_module_factory, monkeypatch):
    strategy_module = strategy_module_factory(IBKR_DRY_RUN_ONLY="false")
    observed = {}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)

    def fake_run_strategy_core(**_kwargs):
        return StrategyCycleResult(
            result="OK - executed",
            execution_summary={
                "execution_status": "executed",
                "orders_submitted": [{"symbol": "AAA"}],
                "orders_skipped": [],
                "price_source_mode": "mixed_market_quote_snapshot_close",
                "snapshot_price_fallback_used": True,
                "snapshot_price_fallback_count": 1,
                "snapshot_price_fallback_symbols": ["AAA"],
            },
            reconciliation_record_path="/tmp/reconciliation.json",
        )

    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["report"]["summary"]["execution_status"] == "executed"
    assert observed["report"]["summary"]["orders_submitted_count"] == 1
    assert observed["report"]["summary"]["orders_previewed_count"] == 0
    assert observed["report"]["summary"]["dry_run_order_preview_available"] is False
    assert observed["report"]["summary"]["snapshot_price_fallback_used"] is True
    assert observed["report"]["summary"]["snapshot_price_fallback_count"] == 1
    assert observed["report"]["diagnostics"]["price_source_mode"] == "mixed_market_quote_snapshot_close"
    assert observed["report"]["diagnostics"]["snapshot_price_fallback_symbols"] == ["AAA"]
    assert observed["report"]["artifacts"]["reconciliation_record_path"] == "/tmp/reconciliation.json"


def test_handle_request_marks_blocked_cycle_as_report_error_without_http_retry(
    strategy_module_factory,
    monkeypatch,
):
    strategy_module = strategy_module_factory(IBKR_DRY_RUN_ONLY="false")
    observed = {"events": []}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-blocked")
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: StrategyCycleResult(
            result="Blocked - no equity",
            execution_summary={
                "execution_status": "blocked",
                "no_op_reason": "no_equity",
            },
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda _context, event, **fields: observed["events"].append((event, fields)),
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "Blocked - no equity"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["summary"]["execution_status"] == "blocked"
    assert observed["report"]["diagnostics"]["failure_category"] == "strategy_execution_blocked"
    assert observed["events"][-1][0] == "strategy_cycle_blocked"
    assert observed["events"][-1][1]["severity"] == "ERROR"


def test_handle_request_keeps_expected_execution_guard_as_success(
    strategy_module_factory,
    monkeypatch,
):
    strategy_module = strategy_module_factory(IBKR_DRY_RUN_ONLY="false")
    observed = {"events": []}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-pending-order")
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: StrategyCycleResult(
            result="Blocked - pending order",
            execution_summary={
                "execution_status": "blocked",
                "no_op_reason": "pending_orders_detected:AAA",
            },
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda _context, event, **fields: observed["events"].append((event, fields)),
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "Blocked - pending order"
    assert observed["report"]["status"] == "ok"
    assert "failure_category" not in observed["report"]["diagnostics"]
    assert observed["events"][-1][0] == "strategy_cycle_completed"
    assert observed["events"][-1][1]["severity"] == "INFO"


def test_cycle_report_summary_counts_dry_run_order_previews(strategy_module):
    cycle_result = StrategyCycleResult(result="Precheck OK")
    execution_summary = {
        "execution_status": "dry_run",
        "orders_submitted": [{"symbol": "AAA"}, {"symbol": "BBB"}],
        "orders_skipped": [{"symbol": "CCC", "reason": "min_notional"}],
        "quote_snapshot": {"quotes": [{"symbol": "AAA", "last_price": 100.0}]},
    }

    summary = strategy_module._build_cycle_report_summary(
        cycle_result,
        execution_summary,
        {},
        dry_run=True,
    )

    assert summary["orders_submitted_count"] == 2
    assert summary["orders_previewed_count"] == 2
    assert summary["orders_skipped_count"] == 1
    assert summary["dry_run_order_preview_available"] is True
    assert summary["quote_snapshot"]["quotes"][0]["symbol"] == "AAA"


def test_notification_delivery_log_summary_records_sent_dry_run_without_raw_text(strategy_module):
    payload = strategy_module._build_notification_delivery_log_for_report(
        platform="interactive_brokers",
        strategy_profile="hk_low_vol_dividend_quality_snapshot",
        run_id="run-001",
        dry_run=True,
        orders_previewed_count=2,
        delivery_events=[
            {
                "sink": "telegram",
                "delivery_status": "sent",
                "compact_text_sha256": "a" * 64,
                "compact_text_length": 42,
            }
        ],
    )

    assert payload["notification_schema_version"] == "hk_live_enablement_notification.v1"
    assert payload["notification_event_type"] == "hk_snapshot_live_enablement_dry_run"
    assert payload["notification_correlation_id"] == "run-001"
    assert payload["locales"] == ["en", "zh-Hans"]
    assert payload["profile"] == "hk_low_vol_dividend_quality_snapshot"
    assert payload["platform"] == "interactive_brokers"
    assert payload["orders_previewed"] == 2
    assert payload["notification_redacts_sensitive_fields"] is True
    assert "compact_text" not in payload["delivery_events"][0]


def test_notification_delivery_log_summary_stays_empty_without_sent_event(strategy_module):
    payload = strategy_module._build_notification_delivery_log_for_report(
        platform="interactive_brokers",
        strategy_profile="hk_low_vol_dividend_quality_snapshot",
        run_id="run-001",
        dry_run=True,
        orders_previewed_count=2,
        delivery_events=[],
    )

    assert payload == {}


def test_notification_delivery_summary_keeps_failed_transport_receipt(strategy_module):
    payload = strategy_module._build_notification_delivery_summary(
        [
            {
                "sink": "telegram",
                "delivery_status": "failed",
                "transport_acknowledged": False,
                "error_type": "RuntimeError",
                "compact_text_sha256": "a" * 64,
                "compact_text_length": 42,
            }
        ]
    )

    assert payload["attempted_count"] == 1
    assert payload["sent_count"] == 0
    assert payload["failed_count"] == 1
    assert payload["all_acknowledged"] is False
    assert payload["delivery_events"][0]["error_type"] == "RuntimeError"
    assert "compact_text" not in payload["delivery_events"][0]


def test_handle_request_post_returns_market_closed_when_schedule_empty(strategy_module, monkeypatch):
    observed = {}

    def fail_if_called():
        raise AssertionError("Closed market should not execute strategy")

    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: False)
    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "Market Closed"
    assert observed["report"]["status"] == "skipped"
    assert observed["report"]["diagnostics"]["skip_reason"] == "market_closed"


def test_handle_request_error_persists_machine_readable_report(strategy_module, monkeypatch):
    observed = {"messages": []}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(strategy_module, "send_tg_message", lambda message: observed["messages"].append(message))

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "strategy_cycle"
    assert observed["report"]["errors"][0]["error_type"] == "RuntimeError"
    assert len(observed["messages"]) == 1


def test_handle_request_classifies_gateway_connection_failure_as_unavailable(strategy_module, monkeypatch):
    observed = {"events": [], "notifications": []}

    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: (_ for _ in ()).throw(
            strategy_module.IBKRGatewayUnavailableError(
                "TCP preflight failed: connection refused"
            )
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda _context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 503
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "ibkr_connect"
    assert observed["report"]["errors"][0]["failure_category"] == "ibkr_gateway_unavailable"
    assert observed["report"]["diagnostics"]["failure_category"] == "ibkr_gateway_unavailable"
    assert observed["events"][-1][0] == "ibkr_gateway_connect_failed"
    assert observed["events"][-1][1]["failure_category"] == "ibkr_gateway_unavailable"
    assert len(observed["notifications"]) == 1


def test_handle_request_classifies_gateway_read_only_as_trading_permission_failure(
    strategy_module,
    monkeypatch,
):
    observed = {"events": [], "notifications": []}

    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: (_ for _ in ()).throw(
            strategy_module.IBKRTradingPermissionError(
                "IB Gateway API is in Read-Only mode; live execution is disabled."
            )
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda _context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 503
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "ibkr_trading_permission"
    assert observed["report"]["diagnostics"]["failure_category"] == "ibkr_trading_permission"
    assert observed["events"][-1][0] == "ibkr_trading_permission_failed"
    assert observed["events"][-1][1]["severity"] == "ERROR"
    assert len(observed["notifications"]) == 1


def test_handle_request_does_not_misclassify_unrelated_timeout_as_gateway_failure(strategy_module, monkeypatch):
    observed = {"events": []}

    monkeypatch.setattr(strategy_module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("snapshot storage timed out")),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda _context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(strategy_module, "publish_notification", lambda **_kwargs: None)

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["errors"][0]["stage"] == "strategy_cycle"
    assert observed["events"][-1][0] == "strategy_cycle_failed"


def test_run_strategy_core_allows_multiple_runs_in_same_process(strategy_module, monkeypatch):
    observed = {"connect_calls": 0, "disconnect_calls": 0, "messages": []}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            observed["disconnect_calls"] += 1

    def fake_connect_ib():
        observed["connect_calls"] += 1
        return FakeIB()

    monkeypatch.setattr(strategy_module, "connect_ib", fake_connect_ib)
    monkeypatch.setattr(strategy_module, "get_current_portfolio", lambda ib: ({}, {"equity": 1000.0, "buying_power": 500.0}))
    monkeypatch.setattr(strategy_module, "compute_signals", lambda ib, holdings: (None, "daily-check", False, "SPY:✅"))
    monkeypatch.setattr(strategy_module, "send_tg_message", lambda message: observed["messages"].append(message))

    first = strategy_module.run_strategy_core()
    second = strategy_module.run_strategy_core()

    assert first.result == "OK - no-op"
    assert second.result == "OK - no-op"
    assert observed["connect_calls"] == 2
    assert observed["disconnect_calls"] == 2
    assert observed["messages"] == []


def test_send_tg_message_uses_cycle_channel_sender(strategy_module, monkeypatch):
    observed = {}
    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-id")
    monkeypatch.setattr(
        "quant_platform_kit.notifications.cycle_channel.build_cycle_sender",
        lambda **kwargs: (
            observed.setdefault("sender_config", kwargs),
            lambda message: (observed.setdefault("message", message), False)[1],
        )[1],
    )

    sent = strategy_module.send_tg_message("hello")

    assert sent is False
    assert observed == {
        "sender_config": {
            "channel": "telegram",
            "telegram_token": "token",
            "telegram_chat_id": "chat-id",
            "webhook_url": None,
        },
        "message": "hello",
    }


def test_global_telegram_chat_id_is_used(strategy_module_factory):
    module = strategy_module_factory(
        GLOBAL_TELEGRAM_CHAT_ID="shared-chat-id",
    )

    assert module.TG_CHAT_ID == "shared-chat-id"


def test_handle_request_runtime_error_fallback_sends_telegram(strategy_module, monkeypatch):
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(_url, *, json, timeout):
        sent_payloads.append((json, timeout))
        return FakeResponse()

    monkeypatch.setattr(strategy_module, "_handle_request", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token-1")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-1")
    monkeypatch.setenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN", "plugin-token")
    monkeypatch.setenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS", "plugin-chat")
    monkeypatch.setattr(strategy_module.requests, "post", fake_post)

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    assert len(sent_payloads) == 1
    assert sent_payloads[0][0]["chat_id"] == "chat-1"
    assert "IBKR strategy run failed" in sent_payloads[0][0]["text"]
    assert "RuntimeError: boom" in sent_payloads[0][0]["text"]


def test_handle_request_runtime_error_fallback_uses_chinese_copy(strategy_module_factory, monkeypatch):
    strategy_module = strategy_module_factory(NOTIFY_LANG="zh")
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(_url, *, json, timeout):
        sent_payloads.append((json, timeout))
        return FakeResponse()

    monkeypatch.setattr(strategy_module, "_handle_request", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token-1")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-1")
    monkeypatch.setattr(strategy_module.requests, "post", fake_post)

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    text = sent_payloads[0][0]["text"]
    assert "IBKR 策略运行失败" in text
    assert "账户组:" in text
    assert "错误: RuntimeError: boom" in text


def test_handle_request_runtime_error_fallback_prefixes_account_scope(strategy_module_factory, monkeypatch):
    strategy_module = strategy_module_factory(
        ACCOUNT_GROUP="live-u1234567",
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"live-u1234567":{"ib_gateway_instance_name":"127.0.0.1",'
            '"ib_gateway_mode":"live","ib_client_id":1,"account_ids":["U1234567"]}}}'
        ),
        RUNTIME_TARGET_JSON=json.dumps(
            {
                "platform_id": "ibkr",
                "strategy_profile": "global_etf_rotation",
                "dry_run_only": False,
                "execution_mode": "live",
                "account_selector": ["U1234567"],
                "account_scope": "live-u1234567",
            },
            separators=(",", ":"),
        ),
    )
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(_url, *, json, timeout):
        sent_payloads.append((json, timeout))
        return FakeResponse()

    monkeypatch.setattr(strategy_module, "_handle_request", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token-1")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-1")
    monkeypatch.setattr(strategy_module.requests, "post", fake_post)

    with strategy_module.app.test_request_context("/run", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    assert sent_payloads[0][0]["text"].startswith("[U1234567] IBKR strategy run failed")


def test_handle_reconciliation_persists_and_logs_only_redacted_failure_fields(
    strategy_module,
    monkeypatch,
    capsys,
):
    marker = "gateway.example.internal:4999 client_id=72 account=demo-account /private/config.json"
    observed = {"events": []}
    raw_report = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "deploy_target": "cloud_run",
        "service_name": "private-service",
        "strategy_profile": "global_etf_rotation",
        "runtime_target": {"account_selector": ["demo-account"]},
        "account_scope": "demo-account",
        "account_group": "demo-account",
        "project_id": "private-project",
        "instance_name": marker,
        "account_ids": ["demo-account"],
        "run_id": "run-001",
        "run_source": "cloud_run",
        "status": "started",
        "dry_run": True,
        "started_at": "2026-09-04T00:00:00Z",
        "finished_at": None,
        "summary": {},
        "diagnostics": {"ib_gateway_host": marker},
        "artifacts": {"strategy_config_path": marker},
        "errors": [],
    }

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda *_args, **_kwargs: raw_report)
    monkeypatch.setattr(
        strategy_module,
        "connect_ib",
        lambda **_kwargs: (_ for _ in ()).throw(
            strategy_module.IBKRGatewayUnavailableError(marker)
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda _context, event, **fields: observed["events"].append((event, fields)),
    )
    def persist_report(report, **_kwargs):
        observed["report"] = report
        return "gs://private-reports/reconciliation.json"

    monkeypatch.setattr(strategy_module, "persist_execution_report", persist_report)

    with strategy_module.app.test_request_context("/reconcile", method="POST"):
        body, status = strategy_module.handle_reconciliation()

    assert (body, status) == ("Error", 503)
    assert observed["report"]["errors"] == [
        {
            "stage": "broker_reconciliation",
            "error_type": "IBKRGatewayUnavailableError",
            "failure_category": "broker_reconciliation",
        }
    ]
    failed_event = observed["events"][-1]
    assert failed_event[0] == "broker_reconciliation_failed"
    assert "error_message" not in failed_event[1]
    serialized = json.dumps(observed, sort_keys=True)
    assert marker not in serialized
    assert "demo-account" not in serialized
    output = capsys.readouterr().out
    assert marker not in output
    assert output == "execution_report gs://private-reports/reconciliation.json\n"


def test_handle_reconciliation_hides_disconnect_and_persistence_error_details(
    strategy_module,
    monkeypatch,
    capsys,
):
    marker = "gateway.example.internal:4999 client_id=72 account=demo-account /private/config.json"

    class FakeIB:
        def disconnect(self):
            raise RuntimeError(marker)

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(
        strategy_module,
        "build_execution_report",
        lambda *_args, **_kwargs: {
            "schema_version": "runtime_report.v1",
            "platform": "ibkr",
            "deploy_target": "cloud_run",
            "strategy_profile": "global_etf_rotation",
            "run_id": "run-001",
            "run_source": "cloud_run",
            "status": "started",
            "started_at": "2026-09-04T00:00:00Z",
            "summary": {},
            "diagnostics": {},
            "artifacts": {},
            "errors": [],
        },
    )
    monkeypatch.setattr(strategy_module, "connect_ib", lambda **_kwargs: FakeIB())
    monkeypatch.setattr(
        strategy_module,
        "collect_read_only_reconciliation_observations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            strategy_module.IBKRReconciliationReadError(marker)
        ),
    )
    monkeypatch.setattr(strategy_module, "log_runtime_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(marker)),
    )

    with strategy_module.app.test_request_context("/reconcile", method="POST"):
        body, status = strategy_module.handle_reconciliation()

    assert (body, status) == ("Error", 503)
    output = capsys.readouterr().out
    assert marker not in output
    assert "failed to disconnect IBKR reconciliation client (error_type=RuntimeError)" in output
    assert "failed to persist reconciliation report (error_type=RuntimeError)" in output
