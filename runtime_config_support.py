from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from quant_platform_kit.cloud import get_secret_store
from quant_platform_kit.common.runtime_config import (
    first_non_empty,
    resolve_bool_value,
    resolve_cash_only_execution_env,
    resolve_dry_run_env,
    resolve_float_env,
    resolve_optional_bool_env,
    resolve_optional_dca_mode_env,
    resolve_optional_ibit_zscore_exit_mode_env,
    resolve_optional_positive_float_env,
    resolve_optional_symbol_env,
    resolve_split_env_list,
    resolve_strategy_runtime_path_settings,
)
from quant_platform_kit.common.runtime_target import (
    RuntimeTarget,
    resolve_runtime_target_from_env,
)
from quant_platform_kit.common.live_continuity import runtime_target_permits_standard_execution
try:
    from quant_platform_kit.common.broker_costs import (
        BrokerCostProfile,
        minimum_economic_order_notional_usd,
    )
except ImportError:  # pragma: no cover - compatibility with older pinned shared wheels
    @dataclass(frozen=True)
    class BrokerCostProfile:
        fixed_order_fee_usd: float = 0.0
        minimum_order_fee_usd: float = 0.0
        max_fixed_fee_bps: float = 100.0
        explicit_min_order_notional_usd: float = 0.0

    def minimum_economic_order_notional_usd(profile: BrokerCostProfile | None) -> float:
        if profile is None:
            return 0.0
        explicit_floor = max(0.0, float(profile.explicit_min_order_notional_usd or 0.0))
        fee_floor = max(
            max(0.0, float(profile.fixed_order_fee_usd or 0.0)),
            max(0.0, float(profile.minimum_order_fee_usd or 0.0)),
        )
        max_fee_bps = max(0.0, float(profile.max_fixed_fee_bps or 0.0))
        if fee_floor <= 0.0 or max_fee_bps <= 0.0:
            return explicit_floor
        return max(explicit_floor, fee_floor / (max_fee_bps / 10_000.0))
from strategy_registry import (
    IBKR_PLATFORM,
    STRATEGY_CATALOG,
    resolve_strategy_definition,
    resolve_strategy_metadata,
)

DEFAULT_ACCOUNT_GROUP = "default"
DEFAULT_MARKET = "US"
DEFAULT_MARKET_CALENDAR = "NYSE"
DEFAULT_MARKET_CURRENCY = "USD"
DEFAULT_MARKET_DATA_SYMBOL_SUFFIX = ""
DEFAULT_MARKET_EXCHANGE = "SMART"
DEFAULT_MARKET_TIMEZONE = "America/New_York"
HK_MARKET = "HK"
HK_MARKET_CALENDAR = "XHKG"
HK_MARKET_CURRENCY = "HKD"
HK_MARKET_DATA_SYMBOL_SUFFIX = ".HK"
HK_MARKET_EXCHANGE = "SEHK"
HK_MARKET_TIMEZONE = "Asia/Hong_Kong"
DEFAULT_RESERVED_CASH_FLOOR_USD = 0.0
DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD = 1000.0
DEFAULT_IBKR_MIN_ORDER_FEE_USD = 0.35
DEFAULT_IBKR_MAX_FIXED_FEE_BPS = 50.0
DEFAULT_IBKR_MIN_ORDER_NOTIONAL_USD = minimum_economic_order_notional_usd(
    BrokerCostProfile(
        minimum_order_fee_usd=DEFAULT_IBKR_MIN_ORDER_FEE_USD,
        max_fixed_fee_bps=DEFAULT_IBKR_MAX_FIXED_FEE_BPS,
    )
)
EXECUTION_BACKEND_GATEWAY = "gateway"
EXECUTION_BACKEND_QUANTCONNECT = "quantconnect"
SUPPORTED_EXECUTION_BACKENDS = frozenset(
    {
        EXECUTION_BACKEND_GATEWAY,
        EXECUTION_BACKEND_QUANTCONNECT,
    }
)


def resolve_market(raw_value: str | None, *, account_group: str) -> str:
    for candidate in (raw_value, account_group):
        value = str(candidate or "").strip().upper()
        if not value:
            continue
        normalized = value.replace("-", "_")
        parts = {part for part in normalized.split("_") if part}
        if value in {HK_MARKET, "HONG_KONG", "HONGKONG"} or HK_MARKET in parts:
            return HK_MARKET
        if value in {DEFAULT_MARKET, "USA", "NYSE", "NASDAQ"} or DEFAULT_MARKET in parts:
            return DEFAULT_MARKET
    return DEFAULT_MARKET


def market_default_settings(market: str) -> dict[str, str]:
    if market == HK_MARKET:
        return {
            "market_calendar": HK_MARKET_CALENDAR,
            "market_currency": HK_MARKET_CURRENCY,
            "market_data_symbol_suffix": HK_MARKET_DATA_SYMBOL_SUFFIX,
            "market_exchange": HK_MARKET_EXCHANGE,
            "market_timezone": HK_MARKET_TIMEZONE,
        }
    return {
        "market_calendar": DEFAULT_MARKET_CALENDAR,
        "market_currency": DEFAULT_MARKET_CURRENCY,
        "market_data_symbol_suffix": DEFAULT_MARKET_DATA_SYMBOL_SUFFIX,
        "market_exchange": DEFAULT_MARKET_EXCHANGE,
        "market_timezone": DEFAULT_MARKET_TIMEZONE,
    }


def normalize_market_data_symbol_suffix(raw_value: str | None) -> str:
    value = str(raw_value or "").strip().upper()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


@dataclass(frozen=True)
class AccountGroupConfig:
    execution_backend: str | None = None
    ib_gateway_instance_name: str | None = None
    ib_gateway_zone: str | None = None
    ib_gateway_mode: str | None = None
    ib_gateway_port: int | None = None
    ib_gateway_ip_mode: str | None = None
    ib_client_id: int | None = None
    service_name: str | None = None
    account_ids: tuple[str, ...] = ()
    quantconnect_project_id: int | None = None
    quantconnect_node_id: str | None = None
    quantconnect_compile_id: str | None = None
    quantconnect_version_id: str | None = None
    quantconnect_credentials_secret_name: str | None = None
    quantconnect_brokerage_secret_name: str | None = None


@dataclass(frozen=True)
class PlatformRuntimeSettings:
    project_id: str | None
    ib_gateway_instance_name: str
    ib_gateway_zone: str
    ib_gateway_mode: str
    ib_gateway_port: int
    ib_gateway_ip_mode: str
    ib_client_id: int
    strategy_profile: str
    strategy_display_name: str
    strategy_domain: str
    strategy_target_mode: str | None
    strategy_artifact_root: str | None
    strategy_artifact_dir: str | None
    feature_snapshot_path: str | None
    feature_snapshot_manifest_path: str | None
    strategy_config_path: str | None
    strategy_config_source: str | None
    reconciliation_output_path: str | None
    dry_run_only: bool
    feature_snapshot_fallback_mode: str | None = None
    feature_snapshot_fallback_cache_dir: str | None = None
    feature_snapshot_fallback_max_stale_days: int | None = None
    runtime_target_enabled: bool = False
    market: str = DEFAULT_MARKET
    market_calendar: str = DEFAULT_MARKET_CALENDAR
    market_currency: str = DEFAULT_MARKET_CURRENCY
    market_data_symbol_suffix: str = DEFAULT_MARKET_DATA_SYMBOL_SUFFIX
    market_exchange: str = DEFAULT_MARKET_EXCHANGE
    market_timezone: str = DEFAULT_MARKET_TIMEZONE
    quantity_step: float = 1.0
    min_order_notional: float = DEFAULT_IBKR_MIN_ORDER_NOTIONAL_USD
    reserved_cash_floor_usd: float = DEFAULT_RESERVED_CASH_FLOOR_USD
    reserved_cash_ratio: float | None = None
    safe_haven_cash_substitute_threshold_usd: float = DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD
    cash_only_execution: bool = True
    income_layer_enabled: bool | None = None
    income_layer_start_usd: float | None = None
    income_layer_max_ratio: float | None = None
    dca_mode: str | None = None
    dca_base_investment_usd: float | None = None
    ibit_zscore_exit_enabled: bool | None = None
    ibit_zscore_exit_mode: str | None = None
    ibit_zscore_exit_parking_symbol: str | None = None
    ibit_zscore_exit_risk_reduced_exposure: float | None = None
    ibit_zscore_exit_risk_off_exposure: float | None = None
    ibit_zscore_exit_allow_outside_execution_window: bool | None = None
    market_signal_handoff_index_uri: str | None = None
    market_signal_handoff_manifest_uri: str | None = None
    market_signal_consumption_audit_uri: str | None = None
    market_signal_cache_dir: str | None = None
    market_signal_required: bool = False
    market_signal_fallback_mode: str | None = None
    market_signal_max_stale_days: int | None = None
    account_group: str = DEFAULT_ACCOUNT_GROUP
    service_name: str | None = None
    account_ids: tuple[str, ...] = ()
    tg_token: str | None = None
    tg_chat_id: str | None = None
    notify_lang: str = "en"
    strategy_plugin_mounts_json: str | None = None
    quantconnect_project_id: int | None = None
    quantconnect_node_id: str | None = None
    quantconnect_compile_id: str | None = None
    quantconnect_version_id: str | None = None
    quantconnect_credentials_secret_name: str | None = None
    quantconnect_brokerage_secret_name: str | None = None
    strategy_plugin_alert_channels: tuple[str, ...] = ()
    strategy_plugin_alert_email_recipients: tuple[str, ...] = ()
    strategy_plugin_alert_email_sender_email: str | None = None
    strategy_plugin_alert_email_sender_password: str | None = None
    strategy_plugin_alert_email_smtp_host: str | None = None
    strategy_plugin_alert_email_smtp_port: str | None = None
    strategy_plugin_alert_email_smtp_security: str | None = None
    strategy_plugin_alert_sms_recipients: tuple[str, ...] = ()
    strategy_plugin_alert_sms_provider: str | None = None
    strategy_plugin_alert_sms_account_id: str | None = None
    strategy_plugin_alert_sms_auth_token: str | None = None
    strategy_plugin_alert_sms_sender: str | None = None
    strategy_plugin_alert_sms_messaging_service_id: str | None = None
    strategy_plugin_alert_sms_api_base_url: str | None = None
    strategy_plugin_alert_sms_body_max_chars: str | None = None
    strategy_plugin_alert_push_recipients: tuple[str, ...] = ()
    strategy_plugin_alert_push_provider: str | None = None
    strategy_plugin_alert_push_app_token: str | None = None
    strategy_plugin_alert_push_access_token: str | None = None
    strategy_plugin_alert_push_api_base_url: str | None = None
    strategy_plugin_alert_push_device: str | None = None
    strategy_plugin_alert_push_priority: str | None = None
    strategy_plugin_alert_push_tags: str | None = None
    strategy_plugin_alert_push_body_max_chars: str | None = None
    strategy_plugin_alert_telegram_chat_ids: tuple[str, ...] = ()
    strategy_plugin_alert_telegram_bot_token: str | None = None
    strategy_plugin_alert_telegram_api_base_url: str | None = None
    strategy_plugin_alert_telegram_parse_mode: str | None = None
    strategy_plugin_alert_telegram_disable_web_page_preview: str | None = None
    strategy_plugin_alert_telegram_body_max_chars: str | None = None
    notification_channel: str = "telegram"
    wecom_webhook_url: str | None = None
    dingtalk_webhook_url: str | None = None
    feishu_webhook_url: str | None = None
    serverchan_webhook_url: str | None = None
    runtime_target: RuntimeTarget | None = None
    strategy_metadata: Any = None
    execution_backend: str = EXECUTION_BACKEND_GATEWAY


def _runtime_target_market_value(runtime_target: RuntimeTarget, field: str) -> str | None:
    value = getattr(runtime_target, field, None)
    if value is not None and str(value).strip():
        return str(value).strip()
    raw = os.getenv("QSL_RUNTIME_TARGET_JSON") or os.getenv("RUNTIME_TARGET_JSON")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    value = payload.get(field) if isinstance(payload, dict) else None
    return str(value).strip() if value is not None and str(value).strip() else None


def load_platform_runtime_settings(
    *,
    project_id_resolver: Callable[[], str | None],
    logger: Callable[[str], None] = print,
    secret_client_factory: Callable[[], Any] | None = None,
) -> PlatformRuntimeSettings:
    project_id = project_id_resolver()
    account_group = resolve_account_group(os.getenv("ACCOUNT_GROUP"))
    group_config = load_account_group_config(
        project_id=project_id,
        account_group=account_group,
        raw_json=os.getenv("IB_ACCOUNT_GROUP_CONFIG_JSON"),
        secret_name=os.getenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME"),
        secret_client_factory=secret_client_factory,
    )
    runtime_target = resolve_runtime_target_from_env(env=os.environ, expected_platform_id=IBKR_PLATFORM)
    strategy_definition = resolve_strategy_definition(
        runtime_target.strategy_profile,
        platform_id=IBKR_PLATFORM,
    )
    strategy_metadata = resolve_strategy_metadata(
        strategy_definition.profile,
        platform_id=IBKR_PLATFORM,
    )
    runtime_paths = resolve_strategy_runtime_path_settings(
        strategy_catalog=STRATEGY_CATALOG,
        strategy_definition=strategy_definition,
        strategy_metadata=strategy_metadata,
        platform_env_prefix="IBKR",
        env=os.environ,
        repo_root=Path(__file__).resolve().parent,
        include_reconciliation_output=True,
    )

    execution_backend = resolve_execution_backend(
        first_non_empty(
            group_config.execution_backend,
            os.getenv("IBKR_EXECUTION_BACKEND"),
        )
    )
    if execution_backend == EXECUTION_BACKEND_GATEWAY:
        instance_name = require_group_string(
            group_config.ib_gateway_instance_name,
            field_name="ib_gateway_instance_name",
            account_group=account_group,
        )

        ib_gateway_mode = resolve_ib_gateway_mode(
            require_group_string(
                group_config.ib_gateway_mode,
                field_name="ib_gateway_mode",
                account_group=account_group,
            )
        )
        ib_gateway_port = resolve_ib_gateway_port(
            (
                group_config.ib_gateway_port
                if group_config.ib_gateway_port is not None
                else parse_optional_int(os.getenv("IB_GATEWAY_PORT"))
            ),
            gateway_mode=ib_gateway_mode,
        )
        ib_client_id = require_group_int(
            group_config.ib_client_id,
            field_name="ib_client_id",
            account_group=account_group,
        )
    else:
        instance_name = first_non_empty(group_config.ib_gateway_instance_name) or ""
        ib_gateway_mode = resolve_ib_gateway_mode(
            first_non_empty(group_config.ib_gateway_mode, os.getenv("IB_GATEWAY_MODE"), "live")
        )
        ib_gateway_port = (
            group_config.ib_gateway_port
            if group_config.ib_gateway_port is not None
            else parse_optional_int(os.getenv("IB_GATEWAY_PORT")) or 0
        )
        ib_client_id = group_config.ib_client_id or 0

    market = resolve_market(
        first_non_empty(
            os.getenv("IBKR_MARKET"),
            _runtime_target_market_value(runtime_target, "market"),
        ),
        account_group=account_group,
    )
    market_defaults = market_default_settings(market)
    return PlatformRuntimeSettings(
        project_id=project_id,
        execution_backend=execution_backend,
        ib_gateway_instance_name=instance_name,
        ib_gateway_zone=first_non_empty(
            group_config.ib_gateway_zone,
            os.getenv("IB_GATEWAY_ZONE", "").strip(),
        )
        or "",
        ib_gateway_mode=ib_gateway_mode,
        ib_gateway_port=ib_gateway_port,
        ib_gateway_ip_mode=resolve_ib_gateway_ip_mode(
            first_non_empty(group_config.ib_gateway_ip_mode, os.getenv("IB_GATEWAY_IP_MODE")),
            logger=logger,
        ),
        ib_client_id=ib_client_id,
        strategy_profile=runtime_paths.strategy_profile,
        strategy_display_name=runtime_paths.strategy_display_name,
        strategy_domain=runtime_paths.strategy_domain,
        strategy_target_mode=runtime_paths.strategy_target_mode,
        strategy_artifact_root=runtime_paths.strategy_artifact_root,
        strategy_artifact_dir=runtime_paths.strategy_artifact_dir,
        feature_snapshot_path=runtime_paths.feature_snapshot_path,
        feature_snapshot_manifest_path=runtime_paths.feature_snapshot_manifest_path,
        strategy_config_path=runtime_paths.strategy_config_path,
        strategy_config_source=runtime_paths.strategy_config_source,
        reconciliation_output_path=runtime_paths.reconciliation_output_path,
        dry_run_only=resolve_dry_run_env(os.environ, "IBKR_DRY_RUN_ONLY"),
        feature_snapshot_fallback_mode=first_non_empty(
            os.getenv("IBKR_FEATURE_SNAPSHOT_FALLBACK_MODE"),
            os.getenv("FEATURE_SNAPSHOT_FALLBACK_MODE"),
        ),
        feature_snapshot_fallback_cache_dir=first_non_empty(
            os.getenv("IBKR_FEATURE_SNAPSHOT_FALLBACK_CACHE_DIR"),
            os.getenv("FEATURE_SNAPSHOT_FALLBACK_CACHE_DIR"),
        ),
        feature_snapshot_fallback_max_stale_days=parse_optional_int(
            first_non_empty(
                os.getenv("IBKR_FEATURE_SNAPSHOT_MAX_STALE_DAYS"),
                os.getenv("IBKR_FEATURE_SNAPSHOT_FALLBACK_MAX_STALE_DAYS"),
                os.getenv("FEATURE_SNAPSHOT_MAX_STALE_DAYS"),
                os.getenv("FEATURE_SNAPSHOT_FALLBACK_MAX_STALE_DAYS"),
            )
        ),
        runtime_target_enabled=(
            resolve_runtime_target_enabled_env()
            and runtime_target_permits_standard_execution(runtime_target)
        ),
        market=market,
        market_calendar=first_non_empty(
            os.getenv("IBKR_MARKET_CALENDAR"),
            _runtime_target_market_value(runtime_target, "market_calendar"),
            market_defaults["market_calendar"],
        ),
        market_currency=first_non_empty(
            os.getenv("IBKR_MARKET_CURRENCY"),
            market_defaults["market_currency"],
        ).upper(),
        market_data_symbol_suffix=normalize_market_data_symbol_suffix(
            first_non_empty(
                os.getenv("IBKR_MARKET_DATA_SYMBOL_SUFFIX"),
                market_defaults["market_data_symbol_suffix"],
            )
        ),
        market_exchange=first_non_empty(
            os.getenv("IBKR_MARKET_EXCHANGE"),
            market_defaults["market_exchange"],
        ).upper(),
        market_timezone=first_non_empty(
            os.getenv("IBKR_MARKET_TIMEZONE"),
            _runtime_target_market_value(runtime_target, "market_timezone"),
            market_defaults["market_timezone"],
        ),
        quantity_step=1.0,
        min_order_notional=resolve_float_env(
            os.environ,
            "IBKR_MIN_ORDER_NOTIONAL_USD",
            default=DEFAULT_IBKR_MIN_ORDER_NOTIONAL_USD,
        ),
        reserved_cash_floor_usd=resolve_non_negative_float_env(
            "IBKR_MIN_RESERVED_CASH_USD",
            default=DEFAULT_RESERVED_CASH_FLOOR_USD,
        ),
        reserved_cash_ratio=resolve_optional_ratio_env("IBKR_RESERVED_CASH_RATIO"),
        safe_haven_cash_substitute_threshold_usd=max(
            0.0,
            resolve_float_env(
                os.environ,
                "IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD",
                default=DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD,
            ),
        ),
        cash_only_execution=resolve_cash_only_execution_env(
            os.environ,
            platform_env_prefix="IBKR",
        ),
        income_layer_enabled=resolve_optional_bool_env_value("INCOME_LAYER_ENABLED"),
        income_layer_start_usd=resolve_optional_non_negative_float_env("INCOME_LAYER_START_USD"),
        income_layer_max_ratio=resolve_optional_ratio_env("INCOME_LAYER_MAX_RATIO"),
        dca_mode=resolve_optional_dca_mode_env("DCA_MODE"),
        dca_base_investment_usd=resolve_optional_positive_float_env("DCA_BASE_INVESTMENT_USD"),
        ibit_zscore_exit_enabled=resolve_optional_bool_env_value("IBIT_ZSCORE_EXIT_ENABLED"),
        ibit_zscore_exit_mode=resolve_optional_ibit_zscore_exit_mode_env("IBIT_ZSCORE_EXIT_MODE"),
        ibit_zscore_exit_parking_symbol=resolve_optional_symbol_env("IBIT_ZSCORE_EXIT_PARKING_SYMBOL"),
        ibit_zscore_exit_risk_reduced_exposure=resolve_optional_ratio_env(
            "IBIT_ZSCORE_EXIT_RISK_REDUCED_EXPOSURE"
        ),
        ibit_zscore_exit_risk_off_exposure=resolve_optional_ratio_env(
            "IBIT_ZSCORE_EXIT_RISK_OFF_EXPOSURE"
        ),
        ibit_zscore_exit_allow_outside_execution_window=resolve_optional_bool_env_value(
            "IBIT_ZSCORE_EXIT_ALLOW_OUTSIDE_EXECUTION_WINDOW"
        ),
        market_signal_handoff_index_uri=first_non_empty(
            os.getenv("IBKR_MARKET_SIGNAL_HANDOFF_INDEX_URI"),
            os.getenv("MARKET_SIGNAL_HANDOFF_INDEX_URI"),
        ),
        market_signal_handoff_manifest_uri=first_non_empty(
            os.getenv("IBKR_MARKET_SIGNAL_HANDOFF_MANIFEST_URI"),
            os.getenv("MARKET_SIGNAL_HANDOFF_MANIFEST_URI"),
        ),
        market_signal_consumption_audit_uri=first_non_empty(
            os.getenv("IBKR_MARKET_SIGNAL_CONSUMPTION_AUDIT_URI"),
            os.getenv("MARKET_SIGNAL_CONSUMPTION_AUDIT_URI"),
        ),
        market_signal_cache_dir=first_non_empty(
            os.getenv("IBKR_MARKET_SIGNAL_CACHE_DIR"),
            os.getenv("MARKET_SIGNAL_CACHE_DIR"),
        ),
        market_signal_required=resolve_bool_value(
            first_non_empty(
                os.getenv("IBKR_MARKET_SIGNAL_REQUIRED"),
                os.getenv("MARKET_SIGNAL_REQUIRED"),
                "false",
            )
        ),
        market_signal_fallback_mode=first_non_empty(
            os.getenv("IBKR_MARKET_SIGNAL_FALLBACK_MODE"),
            os.getenv("MARKET_SIGNAL_FALLBACK_MODE"),
        ),
        market_signal_max_stale_days=parse_optional_int(
            first_non_empty(
                os.getenv("IBKR_MARKET_SIGNAL_MAX_STALE_DAYS"),
                os.getenv("IBKR_MARKET_SIGNAL_FALLBACK_MAX_STALE_DAYS"),
                os.getenv("MARKET_SIGNAL_MAX_STALE_DAYS"),
                os.getenv("MARKET_SIGNAL_FALLBACK_MAX_STALE_DAYS"),
            )
        ),
        account_group=account_group,
        service_name=first_non_empty(
            os.getenv("K_SERVICE"),
            runtime_target.service_name,
            group_config.service_name,
        ),
        account_ids=group_config.account_ids,
        tg_token=os.getenv("TELEGRAM_TOKEN"),
        tg_chat_id=os.getenv("QSL_GLOBAL_TELEGRAM_CHAT_ID") or os.getenv("GLOBAL_TELEGRAM_CHAT_ID"),
        notify_lang=os.getenv("QSL_NOTIFY_LANG") or os.getenv("NOTIFY_LANG", "en"),
        strategy_plugin_mounts_json=(
            os.getenv("IBKR_STRATEGY_PLUGIN_MOUNTS_JSON")
            or os.getenv("QSL_STRATEGY_PLUGIN_MOUNTS_JSON")
            or os.getenv("STRATEGY_PLUGIN_MOUNTS_JSON")
        ),
        quantconnect_project_id=group_config.quantconnect_project_id,
        quantconnect_node_id=group_config.quantconnect_node_id,
        quantconnect_compile_id=group_config.quantconnect_compile_id,
        quantconnect_version_id=group_config.quantconnect_version_id,
        quantconnect_credentials_secret_name=group_config.quantconnect_credentials_secret_name,
        quantconnect_brokerage_secret_name=group_config.quantconnect_brokerage_secret_name,
        strategy_plugin_alert_channels=resolve_split_env_list("STRATEGY_PLUGIN_ALERT_CHANNELS"),
        strategy_plugin_alert_email_recipients=resolve_split_env_list(
            "STRATEGY_PLUGIN_ALERT_EMAIL_RECIPIENTS"
        ),
        strategy_plugin_alert_email_sender_email=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_EMAIL_SENDER_EMAIL")
        ),
        strategy_plugin_alert_email_sender_password=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_EMAIL_SENDER_PASSWORD")
        ),
        strategy_plugin_alert_email_smtp_host=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_EMAIL_SMTP_HOST")
        ),
        strategy_plugin_alert_email_smtp_port=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_EMAIL_SMTP_PORT")
        ),
        strategy_plugin_alert_email_smtp_security=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_EMAIL_SMTP_SECURITY")
        ),
        strategy_plugin_alert_sms_recipients=resolve_split_env_list(
            "STRATEGY_PLUGIN_ALERT_SMS_RECIPIENTS"
        ),
        strategy_plugin_alert_sms_provider=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_PROVIDER")
        ),
        strategy_plugin_alert_sms_account_id=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_ACCOUNT_ID")
        ),
        strategy_plugin_alert_sms_auth_token=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_AUTH_TOKEN")
        ),
        strategy_plugin_alert_sms_sender=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_SENDER")
        ),
        strategy_plugin_alert_sms_messaging_service_id=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_MESSAGING_SERVICE_ID")
        ),
        strategy_plugin_alert_sms_api_base_url=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_API_BASE_URL")
        ),
        strategy_plugin_alert_sms_body_max_chars=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_SMS_BODY_MAX_CHARS")
        ),
        strategy_plugin_alert_push_recipients=resolve_split_env_list(
            "STRATEGY_PLUGIN_ALERT_PUSH_RECIPIENTS"
        ),
        strategy_plugin_alert_push_provider=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_PROVIDER")
        ),
        strategy_plugin_alert_push_app_token=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_APP_TOKEN")
        ),
        strategy_plugin_alert_push_access_token=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_ACCESS_TOKEN")
        ),
        strategy_plugin_alert_push_api_base_url=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_API_BASE_URL")
        ),
        strategy_plugin_alert_push_device=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_DEVICE")
        ),
        strategy_plugin_alert_push_priority=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_PRIORITY")
        ),
        strategy_plugin_alert_push_tags=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_TAGS")
        ),
        strategy_plugin_alert_push_body_max_chars=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_PUSH_BODY_MAX_CHARS")
        ),
        strategy_plugin_alert_telegram_chat_ids=resolve_split_env_list(
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS"
        ),
        strategy_plugin_alert_telegram_bot_token=first_non_empty(
            os.getenv("QSL_STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN")
        ),
        strategy_plugin_alert_telegram_api_base_url=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_API_BASE_URL")
        ),
        strategy_plugin_alert_telegram_parse_mode=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_PARSE_MODE")
        ),
        strategy_plugin_alert_telegram_disable_web_page_preview=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_DISABLE_WEB_PAGE_PREVIEW")
        ),
        strategy_plugin_alert_telegram_body_max_chars=first_non_empty(
            os.getenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_BODY_MAX_CHARS")
        ),
        notification_channel=os.getenv("NOTIFICATION_CHANNEL", "telegram"),
        wecom_webhook_url=os.getenv("NOTIFICATION_WECOM_WEBHOOK_URL"),
        dingtalk_webhook_url=os.getenv("NOTIFICATION_DINGTALK_WEBHOOK_URL"),
        feishu_webhook_url=os.getenv("NOTIFICATION_FEISHU_WEBHOOK_URL"),
        serverchan_webhook_url=os.getenv("NOTIFICATION_SERVERCHAN_WEBHOOK_URL"),
        runtime_target=runtime_target,
        strategy_metadata=strategy_metadata,
    )


def resolve_strategy_profile(raw_value: str | None) -> str:
    return resolve_strategy_definition(
        raw_value,
        platform_id=IBKR_PLATFORM,
    ).profile


def resolve_non_negative_float_env(name: str, *, default: float) -> float:
    value = resolve_float_env(os.environ, name, default=default)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value}")
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return float(value)


def resolve_optional_non_negative_float_env(name: str) -> float | None:
    raw_value = os.getenv(name)
    if raw_value is None or str(raw_value).strip() == "":
        return None
    return resolve_non_negative_float_env(name, default=0.0)


def resolve_optional_bool_env_value(name: str) -> bool | None:
    raw_value = os.getenv(f"QSL_{name}") or os.getenv(name)
    if raw_value is None or str(raw_value).strip() == "":
        return None
    return resolve_optional_bool_env(name)


def resolve_optional_ratio_env(name: str, default: float | None = None) -> float | None:
    raw_value = os.getenv(f"QSL_{name}") or os.getenv(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    value = float(raw_value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value}")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0,1], got {value}")
    return value


def resolve_runtime_target_enabled_env() -> bool:
    return resolve_optional_bool_env("RUNTIME_TARGET_ENABLED", default=False)


def resolve_account_group(raw_value: str | None) -> str:
    value = (raw_value or "").strip()
    if not value:
        raise EnvironmentError("ACCOUNT_GROUP is required")
    return value


def load_account_group_config(
    *,
    project_id: str | None,
    account_group: str,
    raw_json: str | None,
    secret_name: str | None,
    secret_client_factory: Callable[[], Any] | None = None,
) -> AccountGroupConfig:
    payload = None
    if secret_name:
        if not project_id:
            raise EnvironmentError(
                "GOOGLE_CLOUD_PROJECT is required when IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME is set"
            )
        payload = load_secret_payload(
            project_id,
            secret_name,
            secret_client_factory=secret_client_factory,
        )
    elif raw_json:
        payload = raw_json

    if not payload:
        raise EnvironmentError(
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME or IB_ACCOUNT_GROUP_CONFIG_JSON is required"
        )

    configs = parse_account_group_configs(payload)
    if account_group not in configs:
        available = ", ".join(sorted(configs))
        raise ValueError(
            f"ACCOUNT_GROUP={account_group!r} not found in account-group config; available groups: {available}"
        )
    return configs[account_group]


def parse_account_group_configs(payload: str) -> dict[str, AccountGroupConfig]:
    raw_data = json.loads(payload)
    groups = raw_data.get("groups", raw_data) if isinstance(raw_data, dict) else None
    if not isinstance(groups, dict):
        raise ValueError("IB account-group config must be a JSON object or {\"groups\": {...}}")

    parsed: dict[str, AccountGroupConfig] = {}
    for group_name, group_payload in groups.items():
        if not isinstance(group_payload, dict):
            raise ValueError(f"Account group {group_name!r} must be a JSON object")
        parsed[str(group_name)] = AccountGroupConfig(
            execution_backend=normalize_optional_string(group_payload.get("execution_backend")),
            ib_gateway_instance_name=normalize_optional_string(group_payload.get("ib_gateway_instance_name")),
            ib_gateway_zone=normalize_optional_string(group_payload.get("ib_gateway_zone")),
            ib_gateway_mode=normalize_optional_string(group_payload.get("ib_gateway_mode")),
            ib_gateway_port=parse_optional_int(group_payload.get("ib_gateway_port")),
            ib_gateway_ip_mode=normalize_optional_string(group_payload.get("ib_gateway_ip_mode")),
            ib_client_id=parse_optional_int(group_payload.get("ib_client_id")),
            service_name=normalize_optional_string(group_payload.get("service_name")),
            account_ids=parse_account_ids(group_payload.get("account_ids")),
            quantconnect_project_id=parse_optional_int(group_payload.get("quantconnect_project_id")),
            quantconnect_node_id=normalize_optional_string(group_payload.get("quantconnect_node_id")),
            quantconnect_compile_id=normalize_optional_string(group_payload.get("quantconnect_compile_id")),
            quantconnect_version_id=normalize_optional_string(group_payload.get("quantconnect_version_id")),
            quantconnect_credentials_secret_name=normalize_optional_string(
                group_payload.get("quantconnect_credentials_secret_name")
            ),
            quantconnect_brokerage_secret_name=normalize_optional_string(
                group_payload.get("quantconnect_brokerage_secret_name")
            ),
        )
    return parsed


def load_secret_payload(
    project_id: str,
    secret_name: str,
    *,
    secret_client_factory: Callable[[], Any] | None = None,
) -> str:
    if secret_client_factory is not None:
        client = secret_client_factory()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")
    return get_secret_store().get_secret(secret_name, project_id=project_id)


def parse_account_ids(raw_value: Any) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, (list, tuple)):
        raise ValueError("account_ids must be a JSON array of strings")
    parsed = []
    for item in raw_value:
        value = normalize_optional_string(item)
        if value is None:
            continue
        parsed.append(value)
    return tuple(parsed)


def parse_optional_int(raw_value: Any) -> int | None:
    if raw_value is None or raw_value == "":
        return None
    return int(raw_value)


def normalize_optional_string(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    return value or None


def require_group_string(
    raw_value: str | None,
    *,
    field_name: str,
    account_group: str,
) -> str:
    value = normalize_optional_string(raw_value)
    if value is None:
        raise EnvironmentError(
            f"Account group {account_group!r} requires {field_name}"
        )
    return value


def require_group_int(
    raw_value: int | None,
    *,
    field_name: str,
    account_group: str,
) -> int:
    if raw_value is None:
        raise EnvironmentError(
            f"Account group {account_group!r} requires {field_name}"
        )
    return int(raw_value)


def resolve_execution_backend(raw_value: str | None) -> str:
    backend = (raw_value or EXECUTION_BACKEND_GATEWAY).strip().lower()
    if backend in SUPPORTED_EXECUTION_BACKENDS:
        return backend
    supported = ", ".join(sorted(SUPPORTED_EXECUTION_BACKENDS))
    raise EnvironmentError(f"IBKR_EXECUTION_BACKEND must be one of: {supported}")


def resolve_ib_gateway_mode(raw_value: str | None) -> str:
    mode = (raw_value or "").strip().lower()
    if not mode:
        raise EnvironmentError("IB_GATEWAY_MODE is required and must be either 'live' or 'paper'")
    if mode in {"live", "paper"}:
        return mode
    raise EnvironmentError("IB_GATEWAY_MODE must be either 'live' or 'paper'")


def resolve_ib_gateway_port(raw_value: Any, *, gateway_mode: str) -> int:
    if raw_value is None or raw_value == "":
        return 4002 if gateway_mode == "paper" else 4001
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise EnvironmentError("ib_gateway_port must be an integer between 1 and 65535") from exc
    if port < 1 or port > 65535:
        raise EnvironmentError("ib_gateway_port must be an integer between 1 and 65535")
    return port


def resolve_ib_gateway_ip_mode(
    raw_value: str | None,
    *,
    logger: Callable[[str], None] = print,
) -> str:
    mode = (raw_value or "internal").strip().lower()
    if mode in {"internal", "external"}:
        return mode
    logger(f"Invalid IB_GATEWAY_IP_MODE={mode!r}, defaulting to internal")
    return "internal"
