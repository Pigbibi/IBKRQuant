import importlib
import sys
import types
import unittest
from datetime import timezone as datetime_timezone
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))
UES_SRC = ROOT.parent / "UsEquityStrategies" / "src"
if str(UES_SRC) not in sys.path:
    sys.path.insert(0, str(UES_SRC))


@contextmanager
def install_stub_modules():
    flask_module = types.ModuleType("flask")

    class Flask:
        def __init__(self, _name):
            self._routes = {}

        def route(self, path, methods=None):
            def decorator(func):
                self._routes[(path, tuple(methods or []))] = func
                return func

            return decorator

        def test_request_context(self, *_args, **_kwargs):
            class _Context:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, exc_type, exc, tb):
                    return False

            return _Context()

        def run(self, *args, **kwargs):
            return None

    flask_module.Flask = Flask
    flask_module.request = types.SimpleNamespace(method="POST", headers={})

    requests_module = types.ModuleType("requests")
    requests_module.post = lambda *args, **kwargs: None

    google_module = types.ModuleType("google")
    google_module.__path__ = []
    google_auth_module = types.ModuleType("google.auth")
    google_auth_module.default = lambda *args, **kwargs: (None, None)
    google_module.auth = google_auth_module

    google_cloud_module = types.ModuleType("google.cloud")
    google_cloud_module.__path__ = []
    compute_v1_module = types.ModuleType("google.cloud.compute_v1")
    google_cloud_module.compute_v1 = compute_v1_module

    pandas_module = types.ModuleType("pandas")
    pandas_module.Timestamp = lambda value=None: value

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None

    pytz_module = types.ModuleType("pytz")
    pytz_module.utc = datetime_timezone.utc
    pytz_module.timezone = lambda _name: datetime_timezone.utc

    ib_insync_module = types.ModuleType("ib_insync")
    ib_insync_module.IB = type("IB", (), {})
    ib_insync_module.Stock = type("Stock", (), {})
    ib_insync_module.MarketOrder = type("MarketOrder", (), {})
    ib_insync_module.LimitOrder = type("LimitOrder", (), {})

    strategy_runtime_module = types.ModuleType("strategy_runtime")
    strategy_runtime_module.load_strategy_runtime = lambda *_args, **_kwargs: types.SimpleNamespace(
        entrypoint=lambda **_kwargs: None,
        required_inputs=frozenset({"portfolio_snapshot"}),
        status_icon="📈",
        merged_runtime_config={"trend_ma_window": 150},
        runtime_config={"trend_ma_window": 150},
        managed_symbols=("TQQQ", "BOXX"),
        runtime_adapter=types.SimpleNamespace(
            runtime_policy=types.SimpleNamespace(signal_effective_after_trading_days=None),
            available_inputs=frozenset({"derived_indicators", "portfolio_snapshot"})
        ),
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        evaluate=lambda **_kwargs: None,
    )

    runtime_config_support_module = types.ModuleType("runtime_config_support")
    runtime_config_support_module.load_platform_runtime_settings = lambda **_kwargs: types.SimpleNamespace(
        project_id=None,
        execution_backend="gateway",
        secret_name="secret",
        strategy_profile="tqqq_growth_income",
        strategy_display_name="TQQQ Growth Income",
        strategy_domain="us_equity",
        market="US",
        market_calendar="NYSE",
        market_currency="USD",
        market_data_symbol_suffix="",
        market_exchange="SMART",
        market_timezone="America/New_York",
        account_group="default",
        account_ids=("U1234567",),
        service_name="interactive-brokers-platform",
        ib_gateway_instance_name="127.0.0.1",
        ib_gateway_zone=None,
        ib_gateway_mode="paper",
        ib_gateway_ip_mode="internal",
        ib_client_id=1,
        ib_connect_timeout_seconds=60,
        connect_attempts=3,
        connect_retry_delay_seconds=0,
        client_id_retry_offset=100,
        strategy_target_mode="paper",
        strategy_artifact_dir="/tmp",
        feature_snapshot_path=None,
        feature_snapshot_manifest_path=None,
        strategy_config_path=None,
        strategy_config_source=None,
        reconciliation_output_path=None,
        dry_run_only=False,
        quantity_step=1,
        min_order_notional=0.0,
        safe_haven_cash_substitute_threshold_usd=1000.0,
        runtime_target={},
        notify_lang="en",
        tg_token=None,
        tg_chat_id="chat-id",
        notification_channel="telegram",
        wecom_webhook_url=None,
        dingtalk_webhook_url=None,
        feishu_webhook_url=None,
        serverchan_webhook_url=None,
        strategy_metadata=None,
        ibkr_feature_snapshot_manifest_path=None,
        ibkr_reconciliation_output_path=None,
        market_hours_source="cloud_run",
    )
    runtime_config_support_module.EXECUTION_BACKEND_GATEWAY = "gateway"
    runtime_config_support_module.resolve_ib_gateway_ip_mode = lambda *_args, **_kwargs: "internal"

    modules = {
        "flask": flask_module,
        "requests": requests_module,
        "google": google_module,
        "google.auth": google_auth_module,
        "google.cloud": google_cloud_module,
        "google.cloud.compute_v1": compute_v1_module,
        "pandas": pandas_module,
        "pandas_market_calendars": market_calendars_module,
        "pytz": pytz_module,
        "ib_insync": ib_insync_module,
        "strategy_runtime": strategy_runtime_module,
        "runtime_config_support": runtime_config_support_module,
    }
    original = {name: sys.modules.get(name) for name in modules}
    preexisting_modules = set(sys.modules)
    sys.modules.update(modules)
    try:
        yield
    finally:
        project_module_prefixes = (
            "application",
            "decision_mapper",
            "entrypoints",
            "main",
            "notifications",
            "quant_platform_kit",
            "runtime_logging",
        )
        for name in list(sys.modules):
            if name in preexisting_modules:
                continue
            if name == "main" or name.startswith(project_module_prefixes):
                sys.modules.pop(name, None)
        for name, previous in original.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class IBRKConnectTimeoutAlertTests(unittest.TestCase):
    def test_handle_request_sends_ibkr_connect_timeout_notification(self):
        with install_stub_modules():
            sys.modules.pop("main", None)
            module = importlib.import_module("main")
            observed = {"messages": []}

            module.is_market_open_now = lambda **_kwargs: True
            module.run_strategy_core = lambda **_kwargs: (_ for _ in ()).throw(
                module.IBKRGatewayUnavailableError("IBKR API handshake timed out")
            )
            module.persist_execution_report = lambda report: observed.setdefault("report", dict(report)) or "/tmp/report.json"
            module.publish_notification = lambda *, detailed_text, compact_text: observed["messages"].append(
                (detailed_text, compact_text)
            )
            module.build_run_id = lambda: "run-001"

            with module.app.test_request_context("/", method="POST"):
                body, status = module.handle_request()

        self.assertEqual(status, 503)
        self.assertEqual(body, "Error")
        self.assertEqual(observed["report"]["errors"][0]["stage"], "ibkr_connect")
        self.assertIn("IBKR 连接异常", observed["messages"][0][0])


if __name__ == "__main__":
    unittest.main()
