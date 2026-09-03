import importlib
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def strategy_module_factory(monkeypatch):
    def load_strategy_module(**env_overrides):
        defaults = {
            "IB_GATEWAY_INSTANCE_NAME": "127.0.0.1",
            "IB_GATEWAY_ZONE": None,
            "IB_GATEWAY_MODE": "live",
            "STRATEGY_PROFILE": "global_etf_rotation",
            "ACCOUNT_GROUP": "default",
            "IB_CLIENT_ID": "1",
            "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH": None,
            "IBKR_RECONCILIATION_OUTPUT_PATH": None,
            # Request-handling tests exercise post-gate behavior; individual
            # disabled-gate tests override this value explicitly.
            "RUNTIME_TARGET_ENABLED": "true",
            "IB_ACCOUNT_GROUP_CONFIG_JSON": (
                '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
                '"ib_gateway_mode":"live","ib_client_id":1,"account_ids":["U1234567"]}}}'
            ),
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": None,
            "GLOBAL_TELEGRAM_CHAT_ID": None,
            "STRATEGY_PLUGIN_ALERT_CHANNELS": None,
            "STRATEGY_PLUGIN_ALERT_EMAIL_RECIPIENTS": None,
            "STRATEGY_PLUGIN_ALERT_EMAIL_SENDER_EMAIL": None,
            "STRATEGY_PLUGIN_ALERT_EMAIL_SENDER_PASSWORD": None,
            "STRATEGY_PLUGIN_ALERT_EMAIL_SMTP_HOST": None,
            "STRATEGY_PLUGIN_ALERT_EMAIL_SMTP_PORT": None,
            "STRATEGY_PLUGIN_ALERT_EMAIL_SMTP_SECURITY": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_RECIPIENTS": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_PROVIDER": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_APP_TOKEN": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_ACCESS_TOKEN": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_API_BASE_URL": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_DEVICE": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_PRIORITY": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_TAGS": None,
            "STRATEGY_PLUGIN_ALERT_PUSH_BODY_MAX_CHARS": None,
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS": None,
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN": None,
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_API_BASE_URL": None,
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_PARSE_MODE": None,
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_DISABLE_WEB_PAGE_PREVIEW": None,
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_BODY_MAX_CHARS": None,
        }
        defaults.update(env_overrides)
        if "RUNTIME_TARGET_JSON" not in defaults:
            dry_run_only = str(defaults.get("IBKR_DRY_RUN_ONLY") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "on",
            }
            defaults["RUNTIME_TARGET_JSON"] = json.dumps(
                {
                    "platform_id": "ibkr",
                    "strategy_profile": str(defaults.get("STRATEGY_PROFILE") or "global_etf_rotation"),
                    "dry_run_only": dry_run_only,
                    "execution_mode": "paper" if dry_run_only else "live",
                },
                separators=(",", ":"),
            )

        for key, value in defaults.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

        google_module = types.ModuleType("google")
        google_module.__path__ = []

        google_auth_module = types.ModuleType("google.auth")
        google_auth_module.__path__ = []
        google_auth_module.default = lambda *args, **kwargs: (None, None)

        google_auth_transport_module = types.ModuleType("google.auth.transport")
        google_auth_transport_module.__path__ = []
        google_auth_transport_requests_module = types.ModuleType("google.auth.transport.requests")
        google_auth_transport_requests_module.AuthorizedSession = type("AuthorizedSession", (), {})
        google_auth_transport_requests_module.Request = type("Request", (), {})
        google_oauth2_module = types.ModuleType("google.oauth2")
        google_oauth2_module.__path__ = []
        google_oauth2_id_token_module = types.ModuleType("google.oauth2.id_token")
        google_oauth2_id_token_module.fetch_id_token = lambda *_args, **_kwargs: "test-token"

        google_cloud_module = types.ModuleType("google.cloud")
        google_cloud_module.__path__ = []
        compute_v1_module = types.ModuleType("google.cloud.compute_v1")

        google_module.auth = google_auth_module
        google_module.oauth2 = google_oauth2_module
        google_auth_module.transport = google_auth_transport_module
        google_auth_transport_module.requests = google_auth_transport_requests_module
        google_oauth2_module.id_token = google_oauth2_id_token_module
        google_cloud_module.compute_v1 = compute_v1_module

        monkeypatch.setitem(sys.modules, "google", google_module)
        monkeypatch.setitem(sys.modules, "google.auth", google_auth_module)
        monkeypatch.setitem(sys.modules, "google.auth.transport", google_auth_transport_module)
        monkeypatch.setitem(sys.modules, "google.auth.transport.requests", google_auth_transport_requests_module)
        monkeypatch.setitem(sys.modules, "google.oauth2", google_oauth2_module)
        monkeypatch.setitem(sys.modules, "google.oauth2.id_token", google_oauth2_id_token_module)
        monkeypatch.setitem(sys.modules, "google.cloud", google_cloud_module)
        monkeypatch.setitem(sys.modules, "google.cloud.compute_v1", compute_v1_module)

        market_calendars_module = types.ModuleType("pandas_market_calendars")
        market_calendars_module.get_calendar = lambda name: None
        monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

        ib_insync_module = types.ModuleType("ib_insync")

        class PlaceholderIB:
            def connect(self, *args, **kwargs):
                raise AssertionError("test should patch IB before use")

        ib_insync_module.IB = PlaceholderIB
        ib_insync_module.Stock = type("Stock", (), {})
        ib_insync_module.MarketOrder = type("MarketOrder", (), {})
        ib_insync_module.LimitOrder = type("LimitOrder", (), {})
        monkeypatch.setitem(sys.modules, "ib_insync", ib_insync_module)

        sys.modules.pop("main", None)
        module = importlib.import_module("main")
        return importlib.reload(module)

    return load_strategy_module


@pytest.fixture
def strategy_module(strategy_module_factory):
    return strategy_module_factory()
