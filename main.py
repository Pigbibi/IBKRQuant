"""IBKR strategy runner for shared us_equity strategy profiles."""

import hashlib
import json
import importlib
import os
import re
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, request

try:
    from quant_platform_kit.cloud import get_compute_discovery
except ImportError:
    get_compute_discovery = None

from application.cycle_result import coerce_strategy_cycle_result
from application.execution_receipt_adapter import (
    attach_cycle_execution_receipt,
    attach_terminal_fallback_execution_receipt,
)
from application.broker_reconciliation import (
    IBKRReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from application.reconciliation_reporting import (
    build_persistable_reconciliation_report,
    normalize_reconciliation_request_id,
    normalize_reconciliation_scheduler_job_sha256,
)
from application.runtime_broker_adapters import (
    IBKRGatewayUnavailableError,
    IBKRTradingPermissionError,
    build_runtime_broker_adapters,
)
from application.runtime_composer import build_runtime_composer
from application.runtime_deadline import (
    RuntimeDeadlineExceeded,
    enforce_runtime_deadline,
    recycle_current_process_after_response,
)
from application.runtime_strategy_adapters import (
    build_runtime_strategy_adapters,
    fetch_yfinance_historical_candles,
)
from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from application.signal_snapshot import build_signal_snapshot
from decision_mapper import map_strategy_decision
from entrypoints.cloud_run import is_market_open_now
from runtime_execution_policy import (
    IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON,
    dca_execution_unsupported_reason,
)
from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from quant_platform_kit.notifications.strategy_plugin_alerts import (
    StrategyPluginAlertStateSettings,
    build_strategy_plugin_alert_context_label as build_alert_context_label,
    publish_strategy_plugin_alerts as dispatch_strategy_plugin_alerts,
)
from quant_platform_kit.common.runtime_assembly import build_runtime_assembly
from quant_platform_kit.common.execution_commands import build_execution_command_store_from_env
from quant_platform_kit.common.strategy_release import build_runtime_loaded_receipt
from quant_platform_kit.common.runtime_reports import (
    append_runtime_report_error,
    build_runtime_report_base,
    finalize_runtime_report,
    persist_runtime_report,
)
from quant_platform_kit.common.strategy_plugins import (
    build_strategy_plugin_report_payload,
    load_configured_strategy_plugin_signals,
    parse_strategy_plugin_mounts,
)
from quant_platform_kit.ibkr import (
    connect_ib as ibkr_connect_ib,
    ensure_event_loop as ibkr_ensure_event_loop,
    fetch_historical_price_candles,
    fetch_historical_price_series,
    fetch_quote_snapshots as qpk_fetch_quote_snapshots,
)
from application.ibkr_market_data import fetch_quote_snapshots_with_expected_error_summary
from application.ibkr_order_execution import probe_order_write_access, submit_order_intent
from application.monitor_dispatcher import (
    dispatch_due_monitor_targets,
    load_monitor_targets,
    lookback_minutes_from_env,
    max_workers_from_env,
    timeout_seconds_from_env,
)
from application.ibkr_portfolio import (
    IBKRPortfolioSnapshotUnavailableError,
    fetch_reconciled_paper_portfolio_snapshot,
    fetch_portfolio_snapshot,
)
from application.execution_service import (
    check_order_submitted as application_check_order_submitted,
    execute_rebalance as application_execute_rebalance,
    get_market_prices as application_get_market_prices,
)
from application.paper_liquidation_service import execute_paper_liquidation
from application.paper_execution_admission import resolve_paper_execution_admission_enabled
from application.paper_execution_command_consumer import (
    consume_due_paper_execution_commands,
    resolve_paper_execution_command_consumer_enabled,
)
from runtime_logging import build_run_id, emit_runtime_log, extract_cloud_trace
from runtime_config_support import (
    EXECUTION_BACKEND_GATEWAY,
    load_platform_runtime_settings,
    resolve_ib_gateway_ip_mode,
)
from strategy_runtime import load_strategy_runtime

app = Flask(__name__)
ensure_event_loop = ibkr_ensure_event_loop
NEW_YORK_TZ = ZoneInfo("America/New_York")
STRATEGY_RUN_LOCK = threading.Lock()
EXPECTED_EXECUTION_GUARD_REASON_PREFIXES = (
    "pending_orders_detected:",
    "same_day_fills_detected:",
    "same_day_execution_locked:",
)


def _is_execution_failure(execution_status, no_op_reason) -> bool:
    status = str(execution_status or "").strip().lower()
    if status in {"error", "failed", "failure"}:
        return True
    if status != "blocked":
        return False
    reason = str(no_op_reason or "").strip().lower()
    return not reason.startswith(EXPECTED_EXECUTION_GUARD_REASON_PREFIXES)


def get_project_id():
    try:
        from quant_platform_kit.cloud import get_deployment_context

        return get_deployment_context().project_id
    except Exception:
        return os.getenv("GOOGLE_CLOUD_PROJECT")


def get_ib_gateway_ip_mode():
    raw_value = os.getenv("IB_GATEWAY_IP_MODE")
    if raw_value is None and "RUNTIME_SETTINGS" in globals():
        raw_value = RUNTIME_SETTINGS.ib_gateway_ip_mode
    return resolve_ib_gateway_ip_mode(
        raw_value,
        logger=lambda message: print(message, flush=True),
    )


def resolve_gce_instance_ip(instance_name, zone):
    if not get_compute_discovery:
        print(f"quant_platform_kit.cloud not installed, using {instance_name} as host directly", flush=True)
        return instance_name
    try:
        ip_mode = get_ib_gateway_ip_mode()
        project = get_project_id()
        return get_compute_discovery().resolve_instance_ip(
            instance_name,
            zone,
            project_id=project,
            prefer_internal=ip_mode == "internal",
        )
    except Exception as exc:
        print(f"GCE resolve failed for {instance_name}: {exc}, using as hostname", flush=True)
    return instance_name


def get_ib_host():
    global IB_HOST
    if IB_HOST:
        return IB_HOST
    host = RUNTIME_SETTINGS.ib_gateway_instance_name
    zone = RUNTIME_SETTINGS.ib_gateway_zone
    if zone:
        host = resolve_gce_instance_ip(host, zone)
    IB_HOST = host
    return host


def refresh_ib_host():
    """Refresh a zoned gateway address while retaining last-known-good state."""
    global IB_HOST
    instance_name = RUNTIME_SETTINGS.ib_gateway_instance_name
    zone = RUNTIME_SETTINGS.ib_gateway_zone
    if not zone:
        return get_ib_host()

    cached_host = IB_HOST
    try:
        resolved_host = resolve_gce_instance_ip(instance_name, zone)
    except Exception as exc:
        print(f"GCE host refresh failed for {instance_name}: {exc}", flush=True)
        resolved_host = cached_host or instance_name
    if cached_host and resolved_host == instance_name:
        resolved_host = cached_host
    IB_HOST = resolved_host
    return resolved_host


def get_ib_gateway_mode():
    return RUNTIME_SETTINGS.ib_gateway_mode


def get_ib_port():
    configured_port = getattr(RUNTIME_SETTINGS, "ib_gateway_port", None)
    if configured_port is not None:
        return configured_port
    return 4002 if get_ib_gateway_mode() == "paper" else 4001


def get_ib_connect_timeout_seconds():
    raw_value = os.getenv("IBKR_CONNECT_TIMEOUT_SECONDS", "60")
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError):
        print(f"Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60", flush=True)
        return 60
    if timeout_seconds <= 0:
        print(f"Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60", flush=True)
        return 60
    return timeout_seconds


def get_positive_int_env(name, default):
    raw_value = os.getenv(name, str(default))
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    if parsed <= 0:
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    return parsed


def get_non_negative_float_env(name, default):
    raw_value = os.getenv(name, str(default))
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    if parsed < 0:
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    return parsed


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_env_list(value: str | None) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(value or "").replace(";", ",").split(",")
        if item.strip()
    )


RUNTIME_SETTINGS = load_platform_runtime_settings(project_id_resolver=get_project_id)
IB_HOST = None
IB_PORT = get_ib_port()
IB_CLIENT_ID = RUNTIME_SETTINGS.ib_client_id
IB_CONNECT_TIMEOUT_SECONDS = get_ib_connect_timeout_seconds()
IB_CONNECT_ATTEMPTS = get_positive_int_env("IBKR_CONNECT_ATTEMPTS", 3)
IB_CONNECT_RETRY_DELAY_SECONDS = get_non_negative_float_env("IBKR_CONNECT_RETRY_DELAY_SECONDS", 5.0)
IB_CLIENT_ID_RETRY_OFFSET = get_positive_int_env("IBKR_CLIENT_ID_RETRY_OFFSET", 100)
IBKR_DRY_RUN_DEADLINE_SECONDS = get_positive_int_env("IBKR_DRY_RUN_DEADLINE_SECONDS", 105)
IBKR_DEADLINE_REPORT_GRACE_SECONDS = get_positive_int_env("IBKR_DEADLINE_REPORT_GRACE_SECONDS", 5)
STRATEGY_PROFILE = RUNTIME_SETTINGS.strategy_profile
STRATEGY_DISPLAY_NAME = RUNTIME_SETTINGS.strategy_display_name


def _normalize_plugin_mounts_strategy(raw_mounts_json: str | None) -> str | None:
    if not raw_mounts_json:
        return raw_mounts_json
    try:
        mounts = json.loads(raw_mounts_json)
    except json.JSONDecodeError:
        return raw_mounts_json
    if not isinstance(mounts, dict):
        return raw_mounts_json
    plugins = mounts.get("strategy_plugins")
    if not isinstance(plugins, list):
        return raw_mounts_json
    changed = False
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        old = entry.get("strategy")
        if old and old != STRATEGY_PROFILE:
            entry["strategy"] = STRATEGY_PROFILE
            changed = True
            print(f"[config-sync] Plugin mount strategy corrected: {old} → {STRATEGY_PROFILE}", flush=True)
    return json.dumps(mounts, ensure_ascii=False) if changed else raw_mounts_json


if hasattr(RUNTIME_SETTINGS, "strategy_plugin_mounts_json"):
    normalized = _normalize_plugin_mounts_strategy(RUNTIME_SETTINGS.strategy_plugin_mounts_json)
    if normalized != RUNTIME_SETTINGS.strategy_plugin_mounts_json:
        object.__setattr__(RUNTIME_SETTINGS, "strategy_plugin_mounts_json", normalized)

for _env_name in ("MONITOR_DISPATCH_TARGETS_JSON", "IBKR_MONITOR_DISPATCH_TARGETS_JSON"):
    _raw = os.environ.get(_env_name)
    if _raw:
        try:
            _targets = json.loads(_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(_targets, dict):
            continue
        _entries = _targets.get("targets")
        if not isinstance(_entries, list):
            continue
        _changed = False
        for _entry in _entries:
            if isinstance(_entry, dict):
                _old = _entry.get("strategy_profile")
                if _old and _old != STRATEGY_PROFILE:
                    _entry["strategy_profile"] = STRATEGY_PROFILE
                    _changed = True
                    print(f"[config-sync] {_env_name} strategy_profile corrected: {_old} → {STRATEGY_PROFILE}", flush=True)
        if _changed:
            os.environ[_env_name] = json.dumps(_targets, ensure_ascii=False)

ACCOUNT_GROUP = RUNTIME_SETTINGS.account_group
SERVICE_NAME = RUNTIME_SETTINGS.service_name
ACCOUNT_IDS = RUNTIME_SETTINGS.account_ids
PROJECT_ID = RUNTIME_SETTINGS.project_id
EXECUTION_BACKEND = RUNTIME_SETTINGS.execution_backend
QUANTCONNECT_PROJECT_ID = getattr(RUNTIME_SETTINGS, "quantconnect_project_id", None)
QUANTCONNECT_NODE_ID = getattr(RUNTIME_SETTINGS, "quantconnect_node_id", None)
MARKET = RUNTIME_SETTINGS.market
MARKET_CALENDAR = RUNTIME_SETTINGS.market_calendar
MARKET_CURRENCY = RUNTIME_SETTINGS.market_currency
MARKET_DATA_SYMBOL_SUFFIX = RUNTIME_SETTINGS.market_data_symbol_suffix
MARKET_EXCHANGE = RUNTIME_SETTINGS.market_exchange
MARKET_TIMEZONE = RUNTIME_SETTINGS.market_timezone

STRATEGY_RUNTIME = load_strategy_runtime(
    STRATEGY_PROFILE,
    runtime_settings=RUNTIME_SETTINGS,
    logger=lambda message: print(message, flush=True),
)
STRATEGY_ENTRYPOINT = STRATEGY_RUNTIME.entrypoint
STRATEGY_SIGNAL_SOURCE = (
    "feature_snapshot"
    if "feature_snapshot" in STRATEGY_RUNTIME.required_inputs
    else "market_data"
)
STRATEGY_STATUS_ICON = STRATEGY_RUNTIME.status_icon
SIGNAL_EFFECTIVE_AFTER_TRADING_DAYS = getattr(
    getattr(STRATEGY_RUNTIME.runtime_adapter, "runtime_policy", None),
    "signal_effective_after_trading_days",
    None,
)
FEATURE_RUNTIME_PARAMETERS = dict(STRATEGY_RUNTIME.runtime_config)
STRATEGY_RUNTIME_CONFIG = dict(STRATEGY_RUNTIME.merged_runtime_config)
SAFE_HAVEN = str(STRATEGY_RUNTIME_CONFIG.get("safe_haven") or "BIL")
RANKING_POOL = list(STRATEGY_RUNTIME_CONFIG.get("ranking_pool", ()))
CANARY_ASSETS = list(STRATEGY_RUNTIME_CONFIG.get("canary_assets", ()))
TOP_N = STRATEGY_RUNTIME_CONFIG.get("top_n")
SMA_PERIOD = int(STRATEGY_RUNTIME_CONFIG.get("sma_period", 200))
CANARY_BAD_THRESHOLD = STRATEGY_RUNTIME_CONFIG.get("canary_bad_threshold")
REBALANCE_MONTHS = STRATEGY_RUNTIME_CONFIG.get("rebalance_months")
FEATURE_SNAPSHOT_PATH = RUNTIME_SETTINGS.feature_snapshot_path
FEATURE_SNAPSHOT_MANIFEST_PATH = RUNTIME_SETTINGS.feature_snapshot_manifest_path
FEATURE_RUNTIME_CONFIG_PATH = (
    STRATEGY_RUNTIME_CONFIG.get("runtime_config_path")
    or RUNTIME_SETTINGS.strategy_config_path
)
FEATURE_RUNTIME_CONFIG_SOURCE = (
    STRATEGY_RUNTIME_CONFIG.get("runtime_config_source")
    or RUNTIME_SETTINGS.strategy_config_source
)
RECONCILIATION_OUTPUT_PATH = RUNTIME_SETTINGS.reconciliation_output_path
PAPER_LIQUIDATE_ONLY = _env_flag("IBKR_PAPER_LIQUIDATE_ONLY")

TG_TOKEN = RUNTIME_SETTINGS.tg_token
TG_CHAT_ID = RUNTIME_SETTINGS.tg_chat_id
from quant_platform_kit.notifications.cycle_channel import resolve_cycle_channel_and_url

NOTIFY_LANG = RUNTIME_SETTINGS.notify_lang

_NOTIFICATION_CHANNEL, _NOTIFICATION_WEBHOOK_URL = resolve_cycle_channel_and_url(
    explicit_channel=RUNTIME_SETTINGS.notification_channel,
    telegram_token=TG_TOKEN,
    telegram_chat_id=TG_CHAT_ID,
    wecom_url=RUNTIME_SETTINGS.wecom_webhook_url,
    dingtalk_url=RUNTIME_SETTINGS.dingtalk_webhook_url,
    feishu_url=RUNTIME_SETTINGS.feishu_webhook_url,
    serverchan_url=RUNTIME_SETTINGS.serverchan_webhook_url,
)

CASH_RESERVE_RATIO = STRATEGY_RUNTIME.cash_reserve_ratio
CASH_ONLY_EXECUTION = getattr(RUNTIME_SETTINGS, "cash_only_execution", True)
CASH_RESERVE_FLOOR_USD = getattr(STRATEGY_RUNTIME, "cash_reserve_floor_usd", 0.0)
REBALANCE_THRESHOLD_RATIO = STRATEGY_RUNTIME.rebalance_threshold_ratio
LIMIT_BUY_PREMIUM = 1.005
DEFAULT_LIMIT_BUY_PREMIUM_BY_SYMBOL = {"SOXL": 1.015, "TQQQ": 1.010}
SELL_SETTLE_DELAY_SEC = 3
HIST_DATA_PACING_SEC = 0.5
SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def _load_limit_buy_premium_by_symbol(*env_names: str) -> dict[str, float]:
    raw_value = ""
    for env_name in env_names:
        value = os.getenv(env_name)
        if value and value.strip():
            raw_value = value.strip()
            break
    if not raw_value:
        return dict(DEFAULT_LIMIT_BUY_PREMIUM_BY_SYMBOL)
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid limit buy premium map JSON: {raw_value!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Limit buy premium map must be a JSON object keyed by symbol.")
    parsed: dict[str, float] = {}
    for symbol, premium in payload.items():
        symbol_text = str(symbol or "").strip().upper()
        if not symbol_text:
            continue
        premium_value = float(premium)
        if premium_value <= 0.0:
            raise ValueError(f"Limit buy premium for {symbol_text} must be positive.")
        parsed[symbol_text] = premium_value
    return parsed


LIMIT_BUY_PREMIUM_BY_SYMBOL = _load_limit_buy_premium_by_symbol(
    "IBKR_LIMIT_BUY_PREMIUM_BY_SYMBOL_JSON",
    "LIMIT_BUY_PREMIUM_BY_SYMBOL_JSON",
)


def t(key, **kwargs):
    return build_translator(NOTIFY_LANG)(key, **kwargs)


strategy_display_name = build_strategy_display_name(t, NOTIFY_LANG)(
    STRATEGY_PROFILE,
    fallback_name=STRATEGY_DISPLAY_NAME,
    metadata=RUNTIME_SETTINGS.strategy_metadata,
)

RUNTIME_LOG_CONTEXT = build_runtime_assembly(
    platform="interactive_brokers",
    deploy_target="cloud_run",
    service_name=SERVICE_NAME or os.getenv("K_SERVICE", "interactive-brokers-platform"),
    strategy_profile=STRATEGY_PROFILE,
    runtime_target=RUNTIME_SETTINGS.runtime_target,
    account_scope=ACCOUNT_GROUP,
    account_group=ACCOUNT_GROUP,
    project_id=PROJECT_ID,
    instance_name=RUNTIME_SETTINGS.ib_gateway_instance_name,
    extra_context_fields={
        "account_ids": list(ACCOUNT_IDS),
        "strategy_target_mode": RUNTIME_SETTINGS.strategy_target_mode,
        "strategy_artifact_dir": RUNTIME_SETTINGS.strategy_artifact_dir,
        "strategy_display_name": STRATEGY_DISPLAY_NAME,
        "strategy_display_name_localized": strategy_display_name,
        "execution_backend": EXECUTION_BACKEND,
        "ib_connect_attempts": IB_CONNECT_ATTEMPTS,
        "ib_client_id_retry_offset": IB_CLIENT_ID_RETRY_OFFSET,
        "quantconnect_project_id": QUANTCONNECT_PROJECT_ID,
        "quantconnect_node_id": QUANTCONNECT_NODE_ID,
        "market": MARKET,
        "market_calendar": MARKET_CALENDAR,
        "market_currency": MARKET_CURRENCY,
        "market_data_symbol_suffix": MARKET_DATA_SYMBOL_SUFFIX,
        "market_exchange": MARKET_EXCHANGE,
        "market_timezone": MARKET_TIMEZONE,
    },
).build_log_context(run_id="")


def resolve_reporting_managed_symbols() -> tuple[str, ...]:
    configured_managed_symbols = STRATEGY_RUNTIME_CONFIG.get("managed_symbols")
    fallback_managed_symbols = tuple(dict.fromkeys([*RANKING_POOL, SAFE_HAVEN])) if RANKING_POOL else (SAFE_HAVEN,)
    return tuple(
        str(symbol)
        for symbol in (configured_managed_symbols or fallback_managed_symbols)
        if str(symbol or "").strip()
    )


def build_strategy_adapters():
    return build_runtime_strategy_adapters(
        strategy_runtime=STRATEGY_RUNTIME,
        strategy_profile=STRATEGY_PROFILE,
        translator=t,
        pacing_sec=HIST_DATA_PACING_SEC,
        resolve_run_as_of_date_fn=resolve_run_as_of_date,
        fetch_historical_price_series_fn=fetch_market_historical_price_series,
        fetch_historical_price_candles_fn=fetch_market_historical_price_candles,
        map_strategy_decision_fn=map_strategy_decision,
        fallback_historical_candles_fn=fetch_market_fallback_historical_candles,
        build_strategy_plugin_report_payload_fn=build_strategy_plugin_report_payload,
        load_configured_strategy_plugin_signals_fn=load_configured_strategy_plugin_signals,
        parse_strategy_plugin_mounts_fn=parse_strategy_plugin_mounts,
    )


def format_market_data_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value or not MARKET_DATA_SYMBOL_SUFFIX or "." in value:
        return value
    return f"{value}{MARKET_DATA_SYMBOL_SUFFIX}"


def fetch_market_historical_price_series(ib, symbol, **kwargs):
    return fetch_historical_price_series(
        ib,
        str(symbol).strip().upper(),
        exchange=MARKET_EXCHANGE,
        currency=MARKET_CURRENCY,
        **kwargs,
    )


def fetch_market_historical_price_candles(ib, symbol, **kwargs):
    return fetch_historical_price_candles(
        ib,
        str(symbol).strip().upper(),
        exchange=MARKET_EXCHANGE,
        currency=MARKET_CURRENCY,
        **kwargs,
    )


def fetch_market_fallback_historical_candles(symbol, **kwargs):
    return fetch_yfinance_historical_candles(format_market_data_symbol(symbol), **kwargs)


def fetch_market_quote_snapshots(ib, symbols, **kwargs):
    return fetch_quote_snapshots_with_expected_error_summary(
        ib,
        symbols,
        fetch_quote_snapshots=qpk_fetch_quote_snapshots,
        exchange=MARKET_EXCHANGE,
        currency=MARKET_CURRENCY,
        **kwargs,
    )


def submit_market_order_intent(ib, order_intent, **kwargs):
    return submit_order_intent(
        ib,
        order_intent,
        stock_exchange=MARKET_EXCHANGE,
        stock_currency=MARKET_CURRENCY,
        **kwargs,
    )


def probe_market_order_write_access(ib):
    managed_symbols = resolve_reporting_managed_symbols()
    if len(ACCOUNT_IDS) != 1 or not managed_symbols:
        raise RuntimeError(
            "IBKR what-if permission probe requires one account_id and one managed symbol"
        )
    return probe_order_write_access(
        ib,
        symbol=managed_symbols[0],
        account_id=ACCOUNT_IDS[0],
        stock_exchange=MARKET_EXCHANGE,
        stock_currency=MARKET_CURRENCY,
    )


def fetch_market_portfolio_snapshot(ib, **kwargs):
    return fetch_portfolio_snapshot(
        ib,
        currency=MARKET_CURRENCY,
        cash_only_execution=CASH_ONLY_EXECUTION,
        **kwargs,
    )


def build_broker_adapters(*, dry_run_only_override: bool | None = None):
    effective_dry_run_only = RUNTIME_SETTINGS.dry_run_only if dry_run_only_override is None else bool(dry_run_only_override)
    effective_execution_mode = "dry_run" if effective_dry_run_only else RUNTIME_SETTINGS.ib_gateway_mode
    expected_strategy_release = (
        RUNTIME_SETTINGS.runtime_target.strategy_release
        if RUNTIME_SETTINGS.runtime_target is not None
        else None
    )

    def connect_runtime_ib(host, port, client_id, *, timeout):
        # Shadow and dry-run cycles need account and market reads only.  Mark
        # their Gateway sessions read-only so they cannot trigger an IBKR
        # write-access confirmation or acquire trading authority.
        return ibkr_connect_ib(
            host,
            port,
            client_id,
            timeout=timeout,
            readonly=effective_dry_run_only,
        )

    return build_runtime_broker_adapters(
        host_resolver=get_ib_host,
        refresh_host_fn=refresh_ib_host,
        trading_permission_probe_fn=probe_market_order_write_access,
        ib_port=IB_PORT,
        ib_client_id=IB_CLIENT_ID,
        connect_timeout_seconds=IB_CONNECT_TIMEOUT_SECONDS,
        connect_attempts=IB_CONNECT_ATTEMPTS,
        connect_retry_delay_seconds=IB_CONNECT_RETRY_DELAY_SECONDS,
        client_id_retry_offset=IB_CLIENT_ID_RETRY_OFFSET,
        ensure_event_loop_fn=ensure_event_loop,
        connect_ib_fn=connect_runtime_ib,
        fetch_portfolio_snapshot_fn=fetch_market_portfolio_snapshot,
        fetch_quote_snapshots_fn=fetch_market_quote_snapshots,
        submit_order_intent_fn=submit_market_order_intent,
        application_get_market_prices_fn=application_get_market_prices,
        application_check_order_submitted_fn=application_check_order_submitted,
        application_execute_rebalance_fn=application_execute_rebalance,
        execute_paper_liquidation_fn=execute_paper_liquidation,
        translator=t,
        strategy_profile=STRATEGY_PROFILE,
        account_group=ACCOUNT_GROUP,
        service_name=SERVICE_NAME,
        account_ids=tuple(ACCOUNT_IDS),
        dry_run_only=effective_dry_run_only,
        cash_only_execution=CASH_ONLY_EXECUTION,
        cash_reserve_ratio=CASH_RESERVE_RATIO,
        cash_reserve_floor_usd=CASH_RESERVE_FLOOR_USD,
        rebalance_threshold_ratio=REBALANCE_THRESHOLD_RATIO,
        limit_buy_premium=LIMIT_BUY_PREMIUM,
        limit_buy_premium_by_symbol=LIMIT_BUY_PREMIUM_BY_SYMBOL,
        quantity_step=RUNTIME_SETTINGS.quantity_step,
        min_order_notional=RUNTIME_SETTINGS.min_order_notional,
        safe_haven_cash_substitute_threshold_usd=RUNTIME_SETTINGS.safe_haven_cash_substitute_threshold_usd,
        sell_settle_delay_sec=SELL_SETTLE_DELAY_SEC,
        separator=SEPARATOR,
        strategy_display_name=strategy_display_name,
        sleep_fn=time.sleep,
        market_currency=MARKET_CURRENCY,
        execution_mode=effective_execution_mode,
        paper_execution_admission_enabled=resolve_paper_execution_admission_enabled(
            env_reader=os.getenv,
            dry_run_only=effective_dry_run_only,
            execution_mode=effective_execution_mode,
            execution_environment=(
                RUNTIME_SETTINGS.runtime_target.execution_environment
                if RUNTIME_SETTINGS.runtime_target is not None
                else None
            ),
        ),
        runtime_release_receipt=build_runtime_loaded_receipt(
            strategy_release=expected_strategy_release,
        ),
        expected_strategy_release=expected_strategy_release,
        printer=print,
    )


def build_composer(*, dry_run_only_override: bool | None = None, strategy_plugin_signals=()):
    effective_dry_run_only = RUNTIME_SETTINGS.dry_run_only if dry_run_only_override is None else bool(dry_run_only_override)

    def compute_signals_fn(ib, current_holdings):
        if strategy_plugin_signals:
            return compute_signals(
                ib,
                current_holdings,
                strategy_plugin_signals=strategy_plugin_signals,
            )
        return compute_signals(ib, current_holdings)

    return build_runtime_composer(
        service_name=SERVICE_NAME or os.getenv("K_SERVICE", "interactive-brokers-platform"),
        strategy_profile=STRATEGY_PROFILE,
        strategy_domain=RUNTIME_SETTINGS.strategy_domain,
        account_group=ACCOUNT_GROUP,
        project_id=PROJECT_ID,
        instance_name=RUNTIME_SETTINGS.ib_gateway_instance_name,
        account_ids=tuple(ACCOUNT_IDS),
        strategy_target_mode=RUNTIME_SETTINGS.strategy_target_mode,
        strategy_artifact_dir=RUNTIME_SETTINGS.strategy_artifact_dir,
        strategy_display_name=STRATEGY_DISPLAY_NAME,
        strategy_display_name_localized=strategy_display_name,
        managed_symbols=resolve_reporting_managed_symbols(),
        signal_effective_after_trading_days=SIGNAL_EFFECTIVE_AFTER_TRADING_DAYS,
        signal_source=STRATEGY_SIGNAL_SOURCE,
        status_icon=STRATEGY_STATUS_ICON,
        safe_haven=SAFE_HAVEN,
        dry_run_only=effective_dry_run_only,
        strategy_config_source=FEATURE_RUNTIME_CONFIG_SOURCE,
        ib_gateway_host_resolver=get_ib_host,
        ib_gateway_port=IB_PORT,
        ib_gateway_mode=RUNTIME_SETTINGS.ib_gateway_mode,
        ib_gateway_ip_mode=RUNTIME_SETTINGS.ib_gateway_ip_mode,
        ib_client_id=IB_CLIENT_ID,
        ib_connect_timeout_seconds=IB_CONNECT_TIMEOUT_SECONDS,
        feature_snapshot_path=FEATURE_SNAPSHOT_PATH,
        feature_snapshot_manifest_path=FEATURE_SNAPSHOT_MANIFEST_PATH,
        strategy_config_path=FEATURE_RUNTIME_CONFIG_PATH,
        reconciliation_output_path=RECONCILIATION_OUTPUT_PATH,
        translator=t,
        separator=SEPARATOR,
        send_message=send_tg_message,
        notification_channel=_NOTIFICATION_CHANNEL,
        connect_ib_fn=connect_ib,
        build_portfolio_snapshot_fn=build_portfolio_snapshot,
        compute_signals_fn=compute_signals_fn,
        execute_rebalance_fn=lambda ib, target_weights, positions, account_values, **kwargs: execute_rebalance(
            ib,
            target_weights,
            positions,
            account_values,
            dry_run_only_override=effective_dry_run_only,
            **kwargs,
        ),
        run_id_builder=build_run_id,
        event_logger=emit_runtime_log,
        report_builder=build_runtime_report_base,
        report_persister=persist_runtime_report,
        trace_extractor=extract_cloud_trace,
        env_reader=os.getenv,
        printer=print,
        runtime_target=RUNTIME_SETTINGS.runtime_target,
        extra_reporting_fields={
            "execution_backend": EXECUTION_BACKEND,
            "quantconnect_project_id": QUANTCONNECT_PROJECT_ID,
            "quantconnect_node_id": QUANTCONNECT_NODE_ID,
            "market": MARKET,
            "market_calendar": MARKET_CALENDAR,
            "market_currency": MARKET_CURRENCY,
            "market_data_symbol_suffix": MARKET_DATA_SYMBOL_SUFFIX,
            "market_exchange": MARKET_EXCHANGE,
            "market_timezone": MARKET_TIMEZONE,
        },
    )


def send_tg_message(message):
    from quant_platform_kit.notifications.cycle_channel import build_cycle_sender
    sender = build_cycle_sender(
        channel=_NOTIFICATION_CHANNEL,
        telegram_token=TG_TOKEN,
        telegram_chat_id=TG_CHAT_ID,
        webhook_url=_NOTIFICATION_WEBHOOK_URL,
    )
    return sender(_with_platform_notification_prefix(message))


def _platform_notification_prefix() -> str:
    runtime_target = getattr(RUNTIME_SETTINGS, "runtime_target", None)
    account_selector = getattr(runtime_target, "account_selector", None) or ()
    for selector in account_selector:
        label = _normalize_account_prefix_label(selector)
        if label:
            return f"[{label}]"
    raw_scope = (
        getattr(runtime_target, "account_scope", None)
        or ACCOUNT_GROUP
    )
    label = _normalize_account_prefix_label(raw_scope)
    if not label:
        return ""
    return f"[{label}]"


def _normalize_account_prefix_label(value: object) -> str:
    label = str(value or "").strip().upper()
    if not label or label in {"DEFAULT", "LIVE", "US"}:
        return ""
    if label.startswith("LIVE-U") and len(label) > len("LIVE-U"):
        return label.removeprefix("LIVE-")
    return label


def _with_platform_notification_prefix(message: str) -> str:
    prefix = _platform_notification_prefix()
    text = str(message or "")
    if not prefix or text.lstrip().startswith(prefix):
        return text
    return f"{prefix} {text}"


def publish_notification(*, detailed_text, compact_text):
    return build_composer().build_notification_adapters().publish_cycle_notification(
        detailed_text=detailed_text,
        compact_text=compact_text,
    )


def _runtime_error_notification_targets() -> tuple[tuple[str, str], ...]:
    targets: list[tuple[str, str]] = []
    if TG_TOKEN and TG_CHAT_ID:
        targets.append((TG_TOKEN, TG_CHAT_ID))

    seen: set[tuple[str, str]] = set()
    unique_targets: list[tuple[str, str]] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        unique_targets.append(target)
    return tuple(unique_targets)


def _runtime_error_notification_message(exc: Exception, *, route_label: str | None = None) -> str:
    error_text = f"{type(exc).__name__}: {exc}"
    if len(error_text) > 1200:
        error_text = error_text[:1197] + "..."
    route = route_label or f"{request.method} {request.path}"
    if str(NOTIFY_LANG or "").strip().lower().startswith("zh"):
        return "\n".join(
            (
                "IBKR 策略运行失败",
                f"服务: {SERVICE_NAME or os.getenv('K_SERVICE', 'interactive-brokers-platform')}",
                f"版本: {os.getenv('K_REVISION') or '<unknown>'}",
                f"路由: {route}",
                f"策略: {STRATEGY_PROFILE}",
                f"账户组: {ACCOUNT_GROUP}",
                f"错误: {error_text}",
            )
        )
    return "\n".join(
        (
            "IBKR strategy run failed",
            f"service: {SERVICE_NAME or os.getenv('K_SERVICE', 'interactive-brokers-platform')}",
            f"revision: {os.getenv('K_REVISION') or '<unknown>'}",
            f"route: {route}",
            f"strategy: {STRATEGY_PROFILE}",
            f"account_group: {ACCOUNT_GROUP}",
            f"error: {error_text}",
        )
    )


def _notify_runtime_error(exc: Exception, *, route_label: str | None = None) -> bool:
    targets = _runtime_error_notification_targets()
    if not targets:
        print("IBKR runtime error notification skipped: no Telegram target configured.", flush=True)
        return False
    message = _runtime_error_notification_message(exc, route_label=route_label)
    outcomes = []
    for token, chat_id in targets:
        outcomes.append(send_telegram_message(
            _with_platform_notification_prefix(message),
            token=token,
            chat_id=chat_id,
            requests_module=requests,
        ))
    return bool(outcomes) and all(outcomes)


def _publish_runtime_failure_notification(*, detailed_text: str, compact_text: str, exc: Exception) -> bool:
    try:
        if publish_notification(detailed_text=detailed_text, compact_text=compact_text):
            return True
        return _notify_runtime_error(exc)
    except Exception as notification_exc:
        print(f"IBKR runtime error notification fallback: {notification_exc}", flush=True)
        return _notify_runtime_error(exc)


def _handle_route_runtime_error(exc: Exception, *, route_label: str | None = None):
    print(f"IBKR route failed before strategy-cycle handling: {type(exc).__name__}: {exc}", flush=True)
    traceback.print_exc()
    _notify_runtime_error(exc, route_label=route_label)
    return "Error", 500


def _route_with_runtime_error_fallback(handler, *args, route_label: str | None = None, **kwargs):
    try:
        return handler(*args, **kwargs)
    except Exception as exc:
        return _handle_route_runtime_error(exc, route_label=route_label)


def require_gateway_execution_backend():
    if EXECUTION_BACKEND == EXECUTION_BACKEND_GATEWAY:
        return
    raise RuntimeError(
        f"IBKR execution_backend={EXECUTION_BACKEND!r} is configured; "
        "Gateway connection and direct order execution are disabled for this service. "
        "Run the QuantConnect deployment/algorithm path for this account group."
    )


def connect_ib(
    *,
    read_only: bool = False,
    validate_trading_permissions: bool = True,
):
    """Connect to the Gateway with an optional read-only session boundary."""
    require_gateway_execution_backend()
    # A probe must not merely skip the local what-if permission check.  Its
    # Gateway session itself must be read-only, so a future probe change cannot
    # accidentally acquire broker write authority.
    adapter_dry_run_override = True if read_only else None
    return build_broker_adapters(
        dry_run_only_override=adapter_dry_run_override,
    ).connect_ib(
        validate_trading_permissions=validate_trading_permissions,
        redact_connection_diagnostics=read_only,
    )


def _build_health_probe_connection_error_message(exc: Exception) -> str:
    return f"{t('health_probe_title')}\n{t('ibkr_connection_error_prefix')}{str(exc)}"


def log_runtime_event(log_context, event, **fields):
    return build_composer().build_reporting_adapters().log_event(log_context, event, **fields)


def build_execution_report(log_context, *, dry_run_only_override: bool | None = None):
    return build_composer(dry_run_only_override=dry_run_only_override).build_reporting_adapters().build_report(log_context)


def persist_execution_report(report, *, dry_run_only_override: bool | None = None):
    return build_composer(dry_run_only_override=dry_run_only_override).build_reporting_adapters().persist_execution_report(report)


def persist_reconciliation_report(report):
    return persist_execution_report(
        build_persistable_reconciliation_report(report),
        dry_run_only_override=True,
    )


def build_request_log_context():
    return build_composer().build_reporting_adapters().build_log_context(
        trace_header=request.headers.get("X-Cloud-Trace-Context"),
    )


def resolve_run_as_of_date() -> pd.Timestamp:
    explicit = os.getenv("IBKR_RUN_AS_OF_DATE")
    if explicit:
        return pd.Timestamp(explicit).normalize()
    return pd.Timestamp(datetime.now(NEW_YORK_TZ).date())


def get_historical_close(ib, symbol, duration="2 Y", bar_size="1 day"):
    return build_strategy_adapters().get_historical_close(
        ib,
        symbol,
        duration=duration,
        bar_size=bar_size,
    )


def get_historical_candles(ib, symbol, duration="2 Y", bar_size="1 day"):
    return build_strategy_adapters().get_historical_candles(
        ib,
        symbol,
        duration=duration,
        bar_size=bar_size,
    )


def compute_signals(ib, current_holdings, *, strategy_plugin_signals=()):
    return build_strategy_adapters().compute_signals(
        ib,
        current_holdings,
        strategy_plugin_signals=strategy_plugin_signals,
    )


def load_strategy_plugin_signals():
    return build_strategy_adapters().load_strategy_plugin_signals(
        getattr(RUNTIME_SETTINGS, "strategy_plugin_mounts_json", None)
    )


def attach_strategy_plugin_report(report, *, signals, error: str | None = None):
    build_strategy_adapters().attach_strategy_plugin_report(report, signals=signals, error=error)


def build_strategy_plugin_notification_lines(signals) -> tuple[str, ...]:
    return build_strategy_adapters().build_strategy_plugin_notification_lines(signals)


def build_strategy_plugin_error_notification_lines(error) -> tuple[str, ...]:
    return build_strategy_adapters().build_strategy_plugin_error_notification_lines(error)


def build_strategy_plugin_alert_messages(signals):
    return build_strategy_adapters().build_strategy_plugin_alert_messages(signals)


def build_strategy_plugin_alert_state_settings():
    return StrategyPluginAlertStateSettings.from_env(
        project_id=PROJECT_ID,
    )


def build_strategy_plugin_alert_context_label() -> str:
    return build_alert_context_label(
        platform_id="ibkr",
        strategy_profile=STRATEGY_PROFILE,
        account_scope=ACCOUNT_GROUP,
        service_name=SERVICE_NAME,
        runtime_target=RUNTIME_SETTINGS.runtime_target,
    )


def _has_signal_snapshot_details(snapshot: dict[str, object]) -> bool:
    return any(
        snapshot.get(field_name)
        for field_name in (
            "signal_as_of",
            "market_date",
            "latest_price_source",
            "target_weights",
            "target_values",
            "indicators",
            "signal",
            "status",
        )
    )


def _count_orders(*candidates) -> int:
    for candidate in candidates:
        if isinstance(candidate, (list, tuple)):
            return len(candidate)
    return 0


def _build_cycle_report_summary(cycle_result, execution_summary, reconciliation_record, *, dry_run: bool) -> dict:
    orders_submitted_count = _count_orders(
        execution_summary.get("orders_submitted"),
        reconciliation_record.get("orders_submitted"),
    )
    orders_skipped_count = _count_orders(
        execution_summary.get("orders_skipped"),
        reconciliation_record.get("orders_skipped"),
    )
    orders_previewed_count = orders_submitted_count if dry_run else 0
    summary = {
        "result": cycle_result.result,
        "execution_status": execution_summary.get("execution_status") or reconciliation_record.get("execution_status"),
        "no_op_reason": execution_summary.get("no_op_reason") or reconciliation_record.get("no_op_reason"),
        "orders_submitted_count": orders_submitted_count,
        "orders_previewed_count": orders_previewed_count,
        "orders_skipped_count": orders_skipped_count,
        "dry_run_order_preview_available": bool(dry_run and orders_previewed_count > 0),
        "snapshot_price_fallback_used": bool(
            execution_summary.get("snapshot_price_fallback_used")
            or reconciliation_record.get("snapshot_price_fallback_used")
        ),
        "snapshot_price_fallback_count": int(
            execution_summary.get("snapshot_price_fallback_count")
            or reconciliation_record.get("snapshot_price_fallback_count")
            or 0
        ),
    }
    quote_snapshot = execution_summary.get("quote_snapshot") or reconciliation_record.get("quote_snapshot")
    if quote_snapshot:
        summary["quote_snapshot"] = quote_snapshot
    return summary


def _build_notification_delivery_log_for_report(
    *,
    platform: str,
    strategy_profile: str,
    run_id: str,
    dry_run: bool,
    orders_previewed_count: int,
    delivery_events: list[dict],
) -> dict:
    events = [dict(event) for event in delivery_events if dict(event).get("delivery_status") == "sent"]
    if not dry_run or orders_previewed_count <= 0 or not events:
        return {}
    return {
        "notification_schema_version": "hk_live_enablement_notification.v1",
        "notification_event_type": "hk_snapshot_live_enablement_dry_run",
        "notification_correlation_id": str(run_id or ""),
        "locales": ["en", "zh-Hans"],
        "profile": str(strategy_profile or ""),
        "platform": str(platform or ""),
        "validation_status": "passed",
        "orders_previewed": int(orders_previewed_count),
        "delivery_events": events,
        "notification_contains_profile": True,
        "notification_contains_platform": True,
        "notification_contains_validation_status": True,
        "notification_contains_order_preview_summary": True,
        "notification_redacts_sensitive_fields": True,
        "redaction_policy": "raw notification text is not persisted; only sha256 and length are recorded",
    }


def _build_notification_delivery_summary(delivery_events: list[dict]) -> dict:
    safe_fields = (
        "sink",
        "delivery_status",
        "transport_acknowledged",
        "error_type",
        "compact_text_sha256",
        "compact_text_length",
    )
    events = [
        {key: event[key] for key in safe_fields if key in event}
        for event in (dict(item) for item in delivery_events)
    ]
    if not events:
        return {}
    sent_count = sum(event.get("delivery_status") == "sent" for event in events)
    failed_count = sum(event.get("delivery_status") == "failed" for event in events)
    return {
        "attempted_count": len(events),
        "sent_count": sent_count,
        "failed_count": failed_count,
        "all_acknowledged": failed_count == 0 and sent_count == len(events),
        "delivery_events": events,
    }


def publish_strategy_plugin_alerts(signals, *, report=None):
    result = dispatch_strategy_plugin_alerts(
        signals,
        notification_settings=RUNTIME_SETTINGS,
        translator=t,
        strategy_label=STRATEGY_PROFILE,
        context_label=build_strategy_plugin_alert_context_label(),
        state_settings=build_strategy_plugin_alert_state_settings(),
        log_message=print,
    )
    if report is not None:
        result.attach_to_report(report)
    return result


def build_account_notification_lines() -> tuple[str, ...]:
    account_ids = tuple(str(account_id).strip() for account_id in ACCOUNT_IDS if str(account_id).strip())
    if not account_ids:
        return ()
    return (t("account_ids_detail", account_ids=", ".join(account_ids)),)


def build_extra_notification_lines(
    strategy_plugin_signals=(),
    *,
    strategy_plugin_error: str | None = None,
) -> tuple[str, ...]:
    return (
        t(
            "market_scope_detail",
            market=MARKET,
            currency=MARKET_CURRENCY,
            exchange=MARKET_EXCHANGE,
            calendar=MARKET_CALENDAR,
        ),
        *build_account_notification_lines(),
        *build_strategy_plugin_notification_lines(strategy_plugin_signals),
        *build_strategy_plugin_error_notification_lines(strategy_plugin_error),
    )


def get_current_portfolio(ib):
    return build_broker_adapters().get_current_portfolio(ib)


def build_portfolio_snapshot(ib):
    return build_broker_adapters().build_portfolio_snapshot(
        ib,
        get_current_portfolio_fallback=get_current_portfolio,
    )


def _paper_command_consumer_session_date() -> str:
    return datetime.now(ZoneInfo(MARKET_TIMEZONE)).date().isoformat()


def _paper_command_consumer_runtime_is_isolated() -> bool:
    """Require a disabled, paper-only runtime before any Gateway read occurs."""

    runtime_target = getattr(RUNTIME_SETTINGS, "runtime_target", None)
    return bool(
        runtime_target is not None
        and bool(getattr(RUNTIME_SETTINGS, "dry_run_only", False))
        and not bool(getattr(RUNTIME_SETTINGS, "runtime_target_enabled", True))
        and str(getattr(RUNTIME_SETTINGS, "ib_gateway_mode", "") or "").strip().lower() == "paper"
        and str(getattr(runtime_target, "execution_mode", "") or "").strip().lower() == "paper"
        and bool(CASH_ONLY_EXECUTION)
    )


def run_paper_execution_command_consumer() -> dict[str, object]:
    """Explicitly reconcile due commands without creating a broker order.

    This isolated route deliberately avoids the normal rebalance and execution
    adapters.  It opens the Gateway only after command release and delivery
    binding checks pass, then reads portfolio and quote evidence to simulate
    what would have been submitted.
    """

    if not _paper_command_consumer_runtime_is_isolated():
        raise RuntimeError(
            "paper command consumer requires disabled runtime target, IBKR_DRY_RUN_ONLY=true, "
            "paper Gateway mode, paper runtime target, and CASH_ONLY_EXECUTION=true"
        )
    if not resolve_paper_execution_command_consumer_enabled(
        env_reader=os.getenv,
        dry_run_only=bool(RUNTIME_SETTINGS.dry_run_only),
    ):
        raise RuntimeError("paper command consumer is not enabled")

    runtime_target = RUNTIME_SETTINGS.runtime_target
    expected_release = getattr(runtime_target, "strategy_release", None)
    expected_binding = {
        "platform": "ibkr",
        "account_scope": str(getattr(runtime_target, "account_scope", "") or "unknown"),
        "strategy_profile": str(getattr(runtime_target, "strategy_profile", "") or "unknown"),
    }
    store = build_execution_command_store_from_env(
        platform_env_prefix="IBKR",
        env_reader=os.getenv,
        project_id=PROJECT_ID,
    )
    if not store.cloud_prefix_uri and not store.local_dir:
        raise RuntimeError("IBKR paper command consumer requires an execution command store")

    reporting_adapters = build_composer(dry_run_only_override=True).build_reporting_adapters()
    log_context = reporting_adapters.build_log_context(
        trace_header=request.headers.get("X-Cloud-Trace-Context"),
    )
    report = reporting_adapters.build_report(log_context)
    ib = None
    quote_cache: dict[str, object] = {}

    def _ib():
        nonlocal ib
        if ib is None:
            # This uses a dry-run-only adapter; its permission validation does
            # not perform the normal live what-if order check.
            ib = build_broker_adapters(dry_run_only_override=True).connect_ib()
        return ib

    def _load_portfolio():
        return fetch_reconciled_paper_portfolio_snapshot(
            _ib(),
            account_ids=ACCOUNT_IDS,
            currency=MARKET_CURRENCY,
        )

    def _load_quote(symbol: str):
        normalized = str(symbol or "").strip().upper()
        if normalized not in quote_cache:
            quotes = fetch_market_quote_snapshots(_ib(), (normalized,))
            quote = quotes.get(normalized)
            if quote is None:
                raise IBKRPortfolioSnapshotUnavailableError(
                    "IBKR paper command reconciliation is missing a current quote."
                )
            quote_cache[normalized] = quote
        return quote_cache[normalized]

    try:
        reporting_adapters.log_event(
            log_context,
            "paper_execution_command_consumer_started",
            message="Starting isolated read-only IBKR paper command consumer",
        )
        result = consume_due_paper_execution_commands(
            store=store,
            as_of_session=_paper_command_consumer_session_date(),
            claimant=str(os.getenv("K_SERVICE") or "ibkr-paper-command-consumer"),
            portfolio_loader=_load_portfolio,
            quote_loader=_load_quote,
            managed_symbols=resolve_reporting_managed_symbols(),
            runtime_release_receipt=build_runtime_loaded_receipt(
                strategy_release=expected_release,
            ),
            expected_strategy_release=expected_release,
            expected_command_binding=expected_binding,
        )
        finalize_runtime_report(
            report,
            status="ok" if result.get("status") == "ok" else "skipped",
            summary={"paper_execution_command_consumer": result},
        )
        reporting_adapters.log_event(
            log_context,
            "paper_execution_command_consumer_completed",
            message="IBKR paper command consumer completed",
            result_status=result.get("status"),
            commands_count=len(tuple(result.get("commands") or ())),
        )
        return result
    except Exception as exc:
        append_runtime_report_error(
            report,
            stage="paper_execution_command_consumer",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(report, status="error")
        reporting_adapters.log_event(
            log_context,
            "paper_execution_command_consumer_failed",
            message="IBKR paper command consumer failed",
            severity="ERROR",
            error_type=type(exc).__name__,
        )
        raise
    finally:
        try:
            report_path = reporting_adapters.persist_execution_report(report)
            print(f"execution_report {report_path}", flush=True)
        except Exception as persist_exc:
            print(f"failed to persist execution report: {persist_exc}", flush=True)
        disconnect_fn = getattr(ib, "disconnect", None)
        if callable(disconnect_fn):
            disconnect_fn()


def get_market_prices(ib, symbols):
    return build_broker_adapters().get_market_prices(ib, symbols)


def check_order_submitted(report):
    return build_broker_adapters().check_order_submitted(report)


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    strategy_symbols=None,
    signal_metadata=None,
    acquire_execution_claim=None,
    dry_run_only_override: bool | None = None,
):
    return build_broker_adapters(dry_run_only_override=dry_run_only_override).execute_rebalance(
        ib,
        target_weights,
        positions,
        account_values,
        strategy_symbols=strategy_symbols,
        signal_metadata=signal_metadata,
        acquire_execution_claim=acquire_execution_claim,
    )


def _format_liquidation_orders(orders) -> str:
    return build_broker_adapters().format_liquidation_orders(orders)


def run_paper_liquidation_cycle():
    require_gateway_execution_backend()
    if RUNTIME_SETTINGS.ib_gateway_mode != "paper":
        raise RuntimeError("IBKR_PAPER_LIQUIDATE_ONLY is only allowed when ib_gateway_mode=paper")
    return build_broker_adapters().run_paper_liquidation_cycle(
        connect_ib_fn=connect_ib,
        get_current_portfolio_fn=get_current_portfolio,
        publish_notification_fn=publish_notification,
    )


def run_strategy_core(
    *,
    strategy_plugin_signals=(),
    strategy_plugin_error: str | None = None,
    dry_run_only_override: bool | None = None,
    notification_delivery_events: list[dict] | None = None,
):
    if PAPER_LIQUIDATE_ONLY and dry_run_only_override is None:
        return run_paper_liquidation_cycle()
    composer = build_composer(
        dry_run_only_override=dry_run_only_override,
        strategy_plugin_signals=strategy_plugin_signals,
    )
    try:
        rebalance_runtime = composer.build_rebalance_runtime(
            silent_cycle_notifications=bool(dry_run_only_override),
            notification_delivery_events=notification_delivery_events,
        )
    except TypeError as exc:
        if "notification_delivery_events" not in str(exc):
            raise
        rebalance_runtime = composer.build_rebalance_runtime(
            silent_cycle_notifications=bool(dry_run_only_override),
        )
    return run_rebalance_cycle(
        runtime=rebalance_runtime,
        config=composer.build_rebalance_config(
            extra_notification_lines=build_extra_notification_lines(
                strategy_plugin_signals,
                strategy_plugin_error=strategy_plugin_error,
            ),
            cash_only_execution=CASH_ONLY_EXECUTION,
        ),
    )


def _handle_request(
    *,
    dry_run_only_override: bool | None = None,
    response_body: str = "OK",
    dry_run_label: str = "strategy dry-run",
    persist_report: bool = True,
    report_collector: list[dict] | None = None,
):
    if request.method == "GET":
        if dry_run_only_override is None:
            return "OK - use POST to execute strategy", 200
        return f"{response_body} - use POST to run {dry_run_label}", 200
    if dry_run_only_override is None and not getattr(RUNTIME_SETTINGS, "runtime_target_enabled", True):
        return "Runtime Target Disabled", 200

    log_context = build_request_log_context()
    report = build_execution_report(log_context, dry_run_only_override=dry_run_only_override)
    if report_collector is not None:
        # Register the mutable report before any deadline-bound setup work so a
        # timeout while loading strategy plugins still leaves a report to persist.
        report_collector.append(report)
    execution_window = "dry_run" if dry_run_only_override else "execution"
    lock_acquired = False
    deadline_exceeded = False
    try:
        strategy_plugin_signals, strategy_plugin_error = load_strategy_plugin_signals()
        attach_strategy_plugin_report(
            report,
            signals=strategy_plugin_signals,
            error=strategy_plugin_error,
        )
        lock_acquired = STRATEGY_RUN_LOCK.acquire(blocking=False)
        log_runtime_event(
            log_context,
            "strategy_cycle_received",
            message="Received strategy dry-run request" if dry_run_only_override else "Received strategy execution request",
            http_method=request.method,
            execution_window=execution_window,
        )
        if not lock_acquired:
            log_runtime_event(
                log_context,
                "strategy_cycle_already_running",
                message="Another strategy execution is already running; skip overlapping request",
                severity="WARNING",
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": "already_running"},
            )
            return "Already Running", 200
        market_open = is_market_open_now(
            calendar_name=MARKET_CALENDAR,
            timezone_name=MARKET_TIMEZONE,
            logger=lambda message: print(message, flush=True),
        )
        force_run = (os.getenv("IBKR_FORCE_RUN") or "").strip().lower() == "true"
        if not market_open and not force_run:
            log_runtime_event(
                log_context,
                "market_closed",
                message="Market closed; skip strategy execution",
                execution_window=execution_window,
                market=MARKET,
                market_calendar=MARKET_CALENDAR,
                market_timezone=MARKET_TIMEZONE,
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": "market_closed"},
            )
            return "Market Closed", 200
        if force_run and not market_open:
            log_runtime_event(
                log_context,
                "market_hours_bypassed",
                message="Market hours bypassed for strategy execution",
                execution_window=execution_window,
                market=MARKET,
                market_calendar=MARKET_CALENDAR,
                market_timezone=MARKET_TIMEZONE,
            )
        unsupported_reason = dca_execution_unsupported_reason(STRATEGY_PROFILE)
        if unsupported_reason is not None:
            unsupported_message = (
                "Strategy requires fractional-share execution; IBKR TWS API does not support "
                "fractional equity orders (use desktop TWS for manual DCA or choose another platform)"
                if unsupported_reason == IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON
                else "Strategy requires fractional-share execution; skip"
            )
            log_runtime_event(
                log_context,
                "strategy_execution_unsupported",
                message=unsupported_message,
                execution_window=execution_window,
                skip_reason=unsupported_reason,
                strategy_profile=STRATEGY_PROFILE,
                market=MARKET,
                market_calendar=MARKET_CALENDAR,
                market_timezone=MARKET_TIMEZONE,
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": unsupported_reason},
            )
            return "Unsupported Strategy", 200
        log_runtime_event(
            log_context,
            "strategy_cycle_started",
            message="Starting strategy dry-run" if dry_run_only_override else "Starting strategy execution",
            execution_window=execution_window,
        )
        if dry_run_only_override is None:
            publish_strategy_plugin_alerts(strategy_plugin_signals, report=report)
        notification_delivery_events: list[dict] = []
        cycle_result = coerce_strategy_cycle_result(
            run_strategy_core(
                strategy_plugin_signals=strategy_plugin_signals,
                strategy_plugin_error=strategy_plugin_error,
                dry_run_only_override=dry_run_only_override,
                notification_delivery_events=notification_delivery_events,
            )
        )
        execution_summary = dict(cycle_result.execution_summary or {})
        reconciliation_record = dict(cycle_result.reconciliation_record or {})
        signal_metadata = dict(cycle_result.signal_metadata or {})
        signal_snapshot = dict(signal_metadata.get("signal_snapshot") or {})
        if not signal_snapshot:
            signal_snapshot = build_signal_snapshot(
                platform="ibkr",
                strategy_profile=signal_metadata.get("strategy_profile") or STRATEGY_PROFILE,
                metadata=signal_metadata,
                target_weights=cycle_result.target_weights,
            )
        if execution_summary.get("price_source_mode") or reconciliation_record.get("price_source_mode"):
            signal_snapshot["latest_price_source"] = (
                execution_summary.get("price_source_mode")
                or reconciliation_record.get("price_source_mode")
            )
        fallback_used = bool(
            execution_summary.get("snapshot_price_fallback_used")
            or reconciliation_record.get("snapshot_price_fallback_used")
        )
        if fallback_used:
            signal_snapshot["data_freshness_warning"] = "snapshot_price_fallback_used"
        has_signal_snapshot = _has_signal_snapshot_details(signal_snapshot)
        if has_signal_snapshot:
            log_runtime_event(
                log_context,
                "strategy_signal_snapshot",
                message="Strategy signal snapshot",
                execution_window=execution_window,
                **signal_snapshot,
            )
        report_summary = _build_cycle_report_summary(
            cycle_result,
            execution_summary,
            reconciliation_record,
            dry_run=bool(report.get("dry_run")),
        )
        notification_delivery_log = _build_notification_delivery_log_for_report(
            platform="interactive_brokers",
            strategy_profile=STRATEGY_PROFILE,
            run_id=str(report.get("run_id") or ""),
            dry_run=bool(report.get("dry_run")),
            orders_previewed_count=int(report_summary.get("orders_previewed_count") or 0),
            delivery_events=notification_delivery_events,
        )
        if notification_delivery_log:
            report_summary["notification_delivery_log"] = notification_delivery_log
        notification_delivery_summary = _build_notification_delivery_summary(
            notification_delivery_events
        )
        if notification_delivery_summary:
            report_summary["notification_delivery_summary"] = notification_delivery_summary
        execution_status = str(report_summary.get("execution_status") or "").strip().lower()
        execution_failed = _is_execution_failure(
            execution_status,
            report_summary.get("no_op_reason"),
        )
        if execution_failed:
            append_runtime_report_error(
                report,
                stage="strategy_execution",
                message=(
                    f"Strategy execution status is {execution_status}; "
                    f"reason={report_summary.get('no_op_reason') or 'unspecified'}"
                ),
                error_type="StrategyExecutionBlocked",
                failure_category="strategy_execution_blocked",
            )
        report_diagnostics = {
            "result": cycle_result.result,
            "price_source_mode": execution_summary.get("price_source_mode") or reconciliation_record.get("price_source_mode"),
            "snapshot_price_fallback_symbols": execution_summary.get("snapshot_price_fallback_symbols")
            or reconciliation_record.get("snapshot_price_fallback_symbols")
            or [],
            **({"signal_snapshot": signal_snapshot} if has_signal_snapshot else {}),
        }
        if execution_failed:
            report_diagnostics["failure_category"] = "strategy_execution_blocked"
        attach_cycle_execution_receipt(
            report,
            execution_summary,
            reconciliation_record,
            execution_failed=execution_failed,
        )
        finalize_runtime_report(
            report,
            status="error" if execution_failed else "ok",
            summary=report_summary,
            diagnostics=report_diagnostics,
            artifacts={
                "reconciliation_record_path": cycle_result.reconciliation_record_path,
            },
        )
        log_runtime_event(
            log_context,
            "strategy_cycle_blocked" if execution_failed else "strategy_cycle_completed",
            message=(
                "Strategy execution blocked"
                if execution_failed
                else "Strategy dry-run completed"
                if dry_run_only_override
                else "Strategy execution completed"
            ),
            severity="ERROR" if execution_failed else "INFO",
            execution_window=execution_window,
            result=cycle_result.result,
            execution_status=report_summary.get("execution_status"),
            no_op_reason=report_summary.get("no_op_reason"),
        )
        return (response_body if dry_run_only_override else cycle_result.result), 200
    except RuntimeDeadlineExceeded as exc:
        deadline_exceeded = True
        append_runtime_report_error(
            report,
            stage="strategy_deadline",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(
            report,
            status="error",
            diagnostics={
                "failure_category": "strategy_deadline",
                "timeout_seconds": IBKR_DRY_RUN_DEADLINE_SECONDS,
            },
        )
        raise
    except IBKRGatewayUnavailableError as exc:
        append_runtime_report_error(
            report,
            stage="ibkr_connect",
            message=str(exc),
            error_type=type(exc).__name__,
            failure_category="ibkr_gateway_unavailable",
        )
        finalize_runtime_report(
            report,
            status="error",
            diagnostics={"failure_category": "ibkr_gateway_unavailable"},
        )
        log_runtime_event(
            log_context,
            "ibkr_gateway_connect_failed",
            message="IBKR gateway connection attempts exhausted",
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message=str(exc),
            failure_category="ibkr_gateway_unavailable",
        )
        error_msg = f"🚨 【IBKR 连接异常】\n{str(exc)}"
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 503
    except (IBKRTradingPermissionError, IBKRPortfolioSnapshotUnavailableError) as exc:
        failure_category = (
            "ibkr_trading_permission"
            if isinstance(exc, IBKRTradingPermissionError)
            else "ibkr_portfolio_snapshot"
        )
        append_runtime_report_error(
            report,
            stage=failure_category,
            message=str(exc),
            error_type=type(exc).__name__,
            failure_category=failure_category,
        )
        finalize_runtime_report(
            report,
            status="error",
            diagnostics={"failure_category": failure_category},
        )
        log_runtime_event(
            log_context,
            f"{failure_category}_failed",
            message="IBKR execution readiness validation failed",
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message=str(exc),
            failure_category=failure_category,
        )
        error_msg = f"🚨 【IBKR 执行条件异常】\n{str(exc)}"
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 503
    except Exception as exc:
        append_runtime_report_error(
            report,
            stage="strategy_cycle",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(report, status="error")
        log_runtime_event(
            log_context,
            "strategy_cycle_failed",
            message="Strategy execution failed",
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        error_msg = f"{t('error_title')}\n{traceback.format_exc()}"
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 500
    finally:
        if lock_acquired:
            STRATEGY_RUN_LOCK.release()
        if "execution_receipt" not in report:
            try:
                attach_terminal_fallback_execution_receipt(report)
            except ValueError:
                # A legacy runtime target remains visible as evidence-missing;
                # reporting metadata must never change trading behavior.
                pass
        if not deadline_exceeded and persist_report:
            try:
                if dry_run_only_override is None:
                    report_path = persist_execution_report(report)
                else:
                    report_path = persist_execution_report(report, dry_run_only_override=dry_run_only_override)
                print(f"execution_report {report_path}", flush=True)
            except Exception as persist_exc:
                print(f"failed to persist execution report: {persist_exc}", flush=True)


def _handle_probe(*, response_body: str = "Probe OK"):
    ib = None
    log_context = None
    report = None
    try:
        log_context = build_request_log_context()
        report = build_execution_report(log_context, dry_run_only_override=True)
        log_runtime_event(
            log_context,
            "health_probe_received",
            message="Received health probe request",
            http_method=request.method,
            execution_window="probe",
        )
        ib = connect_ib(
            read_only=True,
            validate_trading_permissions=False,
        )
        snapshot = build_portfolio_snapshot(ib)
        positions = tuple(getattr(snapshot, "positions", ()) or ())
        buying_power = float(getattr(snapshot, "buying_power", 0.0) or 0.0)
        total_equity = float(getattr(snapshot, "total_equity", 0.0) or 0.0)
        finalize_runtime_report(
            report,
            status="ok",
            summary={
                "buying_power": buying_power,
                "total_equity": total_equity,
                "positions_count": len(positions),
            },
        )
        log_runtime_event(
            log_context,
            "health_probe_completed",
            message="Health probe completed",
            execution_window="probe",
            buying_power=buying_power,
            total_equity=total_equity,
            positions_count=len(positions),
        )
        return response_body, 200
    except (ConnectionError, TimeoutError) as exc:
        if report is not None:
            append_runtime_report_error(
                report,
                stage="health_probe",
                message=str(exc),
                error_type=type(exc).__name__,
                failure_category="ibkr_connection",
            )
            finalize_runtime_report(
                report,
                status="error",
                diagnostics={"probe_failure_category": "ibkr_connection"},
            )
        if log_context is not None:
            log_runtime_event(
                log_context,
                "health_probe_failed",
                message="Health probe IBKR connection failed",
                severity="ERROR",
                execution_window="probe",
                error_type=type(exc).__name__,
                error_message=str(exc),
                failure_category="ibkr_connection",
            )
        error_msg = _build_health_probe_connection_error_message(exc)
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 500
    except Exception as exc:
        if report is not None:
            append_runtime_report_error(
                report,
                stage="health_probe",
                message=str(exc),
                error_type=type(exc).__name__,
            )
            finalize_runtime_report(report, status="error")
        if log_context is not None:
            log_runtime_event(
                log_context,
                "health_probe_failed",
                message="Health probe failed",
                severity="ERROR",
                execution_window="probe",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        error_msg = f"{t('health_probe_title')}\n{t('health_probe_error_prefix')}{traceback.format_exc()}"
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 500
    finally:
        if ib is not None and hasattr(ib, "disconnect"):
            try:
                ib.disconnect()
            except Exception as disconnect_exc:
                print(f"failed to disconnect IBKR probe client: {disconnect_exc}", flush=True)
        try:
            if report is not None:
                report_path = persist_execution_report(report, dry_run_only_override=True)
                print(f"execution_report {report_path}", flush=True)
        except Exception as persist_exc:
            print(f"failed to persist execution report: {persist_exc}", flush=True)


_SCHEDULER_JOB_NAME_PATTERN = re.compile(
    r"projects/[A-Za-z0-9-]+/locations/[A-Za-z0-9-]+/jobs/[A-Za-z0-9_-]+"
)


def _scheduler_job_identity_sha256() -> str | None:
    job_name = request.headers.get("X-CloudScheduler-JobName")
    if not isinstance(job_name, str):
        return None
    normalized = job_name.strip()
    if not _SCHEDULER_JOB_NAME_PATTERN.fullmatch(normalized):
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _handle_reconciliation():
    """Build a read-only, fail-closed candidate for one frozen live baseline."""

    ib = None
    log_context = None
    report = None
    scheduler_job_sha256 = _scheduler_job_identity_sha256()
    if scheduler_job_sha256 is None:
        return "Error", 400
    reconciliation_request_id = normalize_reconciliation_request_id(
        request.headers.get("X-QSL-Reconciliation-Request-Id")
    )
    try:
        log_context = build_request_log_context()
        report = build_execution_report(log_context, dry_run_only_override=True)
        report.setdefault("diagnostics", {})["reconciliation_scheduler_job_sha256"] = scheduler_job_sha256
        if reconciliation_request_id is not None:
            report.setdefault("diagnostics", {})["reconciliation_request_id"] = reconciliation_request_id
        runtime_target = RUNTIME_SETTINGS.runtime_target
        if runtime_target is None:
            raise IBKRReconciliationReadError(
                "IBKR reconciliation requires an explicit runtime target."
            )
        log_runtime_event(
            log_context,
            "broker_reconciliation_received",
            message="Received broker reconciliation request",
            execution_window="reconciliation",
        )
        ib = connect_ib(
            read_only=True,
            validate_trading_permissions=False,
        )
        observations = collect_read_only_reconciliation_observations(
            ib,
            account_ids=ACCOUNT_IDS,
            fetch_portfolio_snapshot=fetch_portfolio_snapshot,
            market_currency=MARKET_CURRENCY,
            cash_only_execution=CASH_ONLY_EXECUTION,
        )
        candidate = build_reconciliation_candidate(
            observations=observations,
            runtime_target=runtime_target,
            platform_id=runtime_target.platform_id,
            strategy_profile=STRATEGY_PROFILE,
            account_group=ACCOUNT_GROUP,
            project_id=PROJECT_ID,
        )
        payload = candidate.to_safe_dict()
        finalize_runtime_report(
            report,
            status="ok",
            summary={
                "broker_reconciliation_permits_active_lkg": candidate.permits_active_lkg,
                "broker_reconciliation_blockers_count": len(candidate.recovery_blockers),
                "broker_reconciliation_ledger_records_count": candidate.execution_ledger_records_count,
            },
            diagnostics={"broker_reconciliation": payload},
        )
        log_runtime_event(
            log_context,
            "broker_reconciliation_completed",
            message="Broker reconciliation candidate completed",
            execution_window="reconciliation",
            permits_active_lkg=candidate.permits_active_lkg,
            blockers=[finding.value for finding in candidate.recovery_blockers],
            expected_digests_configured=candidate.expected_digests_configured,
            execution_ledger_records_count=candidate.execution_ledger_records_count,
        )
        return json.dumps(payload, ensure_ascii=False), 200, {"Content-Type": "application/json"}
    except (IBKRReconciliationReadError, ConnectionError, TimeoutError) as exc:
        if report is not None:
            append_runtime_report_error(
                report,
                stage="broker_reconciliation",
                message="Broker reconciliation failed.",
                error_type=type(exc).__name__,
                failure_category="broker_reconciliation",
            )
            finalize_runtime_report(
                report,
                status="error",
                diagnostics={"broker_reconciliation_failure": type(exc).__name__},
            )
        if log_context is not None:
            log_runtime_event(
                log_context,
                "broker_reconciliation_failed",
                message="Broker reconciliation failed",
                severity="ERROR",
                execution_window="reconciliation",
                error_type=type(exc).__name__,
            )
        return "Error", 503
    except Exception as exc:
        if report is not None:
            append_runtime_report_error(
                report,
                stage="broker_reconciliation",
                message="Broker reconciliation failed.",
                error_type=type(exc).__name__,
            )
            finalize_runtime_report(report, status="error")
        if log_context is not None:
            log_runtime_event(
                log_context,
                "broker_reconciliation_failed",
                message="Broker reconciliation failed",
                severity="ERROR",
                execution_window="reconciliation",
                error_type=type(exc).__name__,
            )
        return "Error", 500
    finally:
        if ib is not None and hasattr(ib, "disconnect"):
            try:
                ib.disconnect()
            except Exception as disconnect_exc:
                print(
                    "failed to disconnect IBKR reconciliation client "
                    f"(error_type={type(disconnect_exc).__name__})",
                    flush=True,
                )
        try:
            if report is not None:
                report_path = persist_reconciliation_report(report)
                if (
                    isinstance(report_path, str)
                    and report_path.startswith("gs://")
                    and not any(character.isspace() for character in report_path)
                ):
                    print(f"execution_report {report_path}", flush=True)
                    print(
                        "reconciliation_receipt_ready "
                        f"scheduler_job_sha256={scheduler_job_sha256} report_uri={report_path}",
                        flush=True,
                    )
                else:
                    print("broker reconciliation report persisted", flush=True)
        except Exception as persist_exc:
            print(
                "failed to persist reconciliation report "
                f"(error_type={type(persist_exc).__name__})",
                flush=True,
            )


def _handle_monitor_dispatch():
    if request.method == "GET":
        return "Monitor Dispatch OK - use POST to dispatch due monitor checks", 200

    log_context = build_request_log_context()
    targets = load_monitor_targets()
    result = dispatch_due_monitor_targets(
        targets,
        lookback_minutes=lookback_minutes_from_env(),
        timeout_seconds=timeout_seconds_from_env(),
        max_workers=max_workers_from_env(),
        local_service_name=SERVICE_NAME or os.getenv("K_SERVICE"),
        local_dispatch_fn=_dispatch_local_monitor,
    )
    log_runtime_event(
        log_context,
        "monitor_dispatch_completed",
        message="Monitor dispatch completed",
        monitor_targets_count=len(targets),
        dispatches_due=result.get("dispatches_due"),
        dispatches_sent=result.get("dispatches_sent"),
        dispatch_results=result.get("results") or [],
    )
    if any(bool(item.get("worker_recycle_required")) for item in (result.get("results") or [])):
        recycle_current_process_after_response()
    return result, 200 if result.get("ok") else 502


def _dispatch_local_monitor(dispatch):
    window = str(dispatch.get("window") or "").strip()
    if window == "probe":
        _body, status_code = _handle_probe()
        recycle_required = False
    elif window == "precheck":
        timeout_state: dict[str, bool] = {}
        _body, status_code = _handle_dry_run_with_deadline(
            recycle_on_timeout=False,
            timeout_state=timeout_state,
        )
        recycle_required = bool(timeout_state.get("worker_recycle_required"))
    else:
        raise ValueError(f"Unsupported local monitor window: {window!r}")
    return {
        "status_code": int(status_code),
        "worker_recycle_required": recycle_required,
    }


@app.route("/run", methods=["POST"])
def handle_request():
    if request.method != "POST":
        return "Method Not Allowed", 405
    return _route_with_runtime_error_fallback(_handle_request)


def _handle_dry_run_with_deadline(
    *,
    recycle_on_timeout: bool = True,
    timeout_state: dict[str, bool] | None = None,
):
    reports: list[dict] = []
    try:
        with enforce_runtime_deadline(
            IBKR_DRY_RUN_DEADLINE_SECONDS,
            operation="IBKR strategy dry-run request",
        ):
            response = _route_with_runtime_error_fallback(
                _handle_request,
                dry_run_only_override=True,
                response_body="Dry Run OK",
                dry_run_label="strategy dry-run",
                persist_report=False,
                report_collector=reports,
            )
    except RuntimeDeadlineExceeded as exc:
        _persist_dry_run_report_with_grace(reports, timeout_context=True)
        print(
            json.dumps(
                {
                    "severity": "ERROR",
                    "event": "strategy_cycle_deadline_exceeded",
                    "message": str(exc),
                    "service_name": SERVICE_NAME or os.getenv("K_SERVICE"),
                    "strategy_profile": STRATEGY_PROFILE,
                    "timeout_seconds": IBKR_DRY_RUN_DEADLINE_SECONDS,
                    "worker_recycle_required": True,
                    "worker_recycle_scheduled": recycle_on_timeout,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        if timeout_state is not None:
            timeout_state["worker_recycle_required"] = True
        if recycle_on_timeout:
            recycle_current_process_after_response()
        return "Error", 503
    else:
        _persist_dry_run_report_with_grace(reports, timeout_context=False)
        return response


def _persist_dry_run_report_with_grace(
    reports: list[dict],
    *,
    timeout_context: bool,
) -> None:
    if not reports:
        return
    operation = "IBKR timeout report persistence" if timeout_context else "IBKR dry-run report persistence"
    try:
        with enforce_runtime_deadline(
            IBKR_DEADLINE_REPORT_GRACE_SECONDS,
            operation=operation,
        ):
            report_path = persist_execution_report(
                reports[-1],
                dry_run_only_override=True,
            )
            print(f"execution_report {report_path}", flush=True)
    except RuntimeDeadlineExceeded as persist_exc:
        print(f"dry-run report persistence timed out: {persist_exc}", flush=True)
    except Exception as persist_exc:
        print(f"failed to persist dry-run report: {persist_exc}", flush=True)


@app.route("/dry-run", methods=["POST", "GET"])
def handle_dry_run():
    return _handle_dry_run_with_deadline()


@app.route("/paper-command-consumer", methods=["POST"])
def handle_paper_execution_command_consumer():
    """Manual-only endpoint for paper command reconciliation evidence."""

    try:
        result = run_paper_execution_command_consumer()
    except Exception as exc:
        return _handle_route_runtime_error(exc, route_label="POST /paper-command-consumer")
    return json.dumps(result, ensure_ascii=False), 200, {"Content-Type": "application/json"}


@app.route("/probe", methods=["POST"])
def handle_probe():
    if request.method != "POST":
        return "Method Not Allowed", 405
    return _route_with_runtime_error_fallback(_handle_probe)


@app.route("/reconcile", methods=["POST"])
def handle_reconciliation():
    return _route_with_runtime_error_fallback(
        _handle_reconciliation,
        route_label="broker-reconciliation",
    )


@app.route("/monitor-dispatch", methods=["POST", "GET"])
def handle_monitor_dispatch():
    return _route_with_runtime_error_fallback(
        _handle_monitor_dispatch,
        route_label="monitor-dispatch",
    )


@app.route("/healthz", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    critical_errors: list[str] = []
    for module_path in (
        "application.runtime_composer",
        "application.runtime_reporting_adapters",
        "application.runtime_strategy_adapters",
        "runtime_execution_policy",
    ):
        try:
            importlib.import_module(module_path)
        except Exception as exc:
            critical_errors.append(f"{module_path}: {type(exc).__name__}: {exc}")
    if critical_errors:
        return json.dumps({"status": "unhealthy", "errors": critical_errors}, ensure_ascii=False), 500
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
