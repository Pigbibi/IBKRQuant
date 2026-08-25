"""Builder helpers for IBKR broker-side runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from application.cycle_result import StrategyCycleResult
from quant_platform_kit.common.models import OrderIntent, PortfolioSnapshot, Position


class IBKRGatewayUnavailableError(ConnectionError):
    """Raised after retryable IBKR gateway connection attempts are exhausted."""


class IBKRTradingPermissionError(RuntimeError):
    """Raised when a live Gateway connection cannot verify order-write access."""


@dataclass(frozen=True)
class IBKRRuntimeBrokerAdapters:
    host_resolver: Any
    ib_port: int
    ib_client_id: int
    connect_timeout_seconds: int
    connect_attempts: int
    connect_retry_delay_seconds: float
    client_id_retry_offset: int
    ensure_event_loop_fn: Any
    connect_ib_fn: Any
    fetch_portfolio_snapshot_fn: Any
    fetch_quote_snapshots_fn: Any
    submit_order_intent_fn: Any
    order_intent_cls: Any
    application_get_market_prices_fn: Any
    application_check_order_submitted_fn: Any
    application_execute_rebalance_fn: Any
    execute_paper_liquidation_fn: Any
    translator: Any
    strategy_profile: str
    account_group: str
    service_name: str | None
    account_ids: tuple[str, ...]
    dry_run_only: bool
    cash_reserve_ratio: float
    cash_reserve_floor_usd: float
    rebalance_threshold_ratio: float
    limit_buy_premium: float
    quantity_step: float
    min_order_notional: float
    safe_haven_cash_substitute_threshold_usd: float
    sell_settle_delay_sec: float
    separator: str
    strategy_display_name: str
    sleep_fn: Any
    cash_only_execution: bool = True
    market_currency: str = "USD"
    execution_mode: str = "paper"
    limit_buy_premium_by_symbol: dict[str, float] | None = None
    printer: Any = print
    refresh_host_fn: Any = None
    trading_permission_probe_fn: Any = None
    paper_execution_admission_enabled: bool = False
    runtime_release_receipt: Any = None
    expected_strategy_release: Any = None

    def validate_configured_accounts(self, ib):
        if not self.account_ids:
            if str(self.execution_mode or "").strip().lower() == "live":
                raise RuntimeError(
                    "IBKR live execution requires configured account_ids "
                    f"for account_group={self.account_group!r}."
                )
            return
        managed_accounts_fn = getattr(ib, "managedAccounts", None)
        if not callable(managed_accounts_fn):
            return
        managed_accounts = {str(account_id).strip() for account_id in (managed_accounts_fn() or ())}
        missing_accounts = tuple(
            account_id for account_id in self.account_ids if str(account_id).strip() not in managed_accounts
        )
        if not missing_accounts:
            return
        raise RuntimeError(
            "Configured IBKR account_ids are not available to the current Gateway username "
            f"for account_group={self.account_group!r}; "
            f"missing_count={len(missing_accounts)}; managed_count={len(managed_accounts)}."
        )

    def fetch_account_portfolio_snapshot(self, ib):
        if self.account_ids:
            return self.fetch_portfolio_snapshot_fn(ib, account_ids=self.account_ids)
        return self.fetch_portfolio_snapshot_fn(ib)

    def validate_trading_permissions(self, ib):
        if self.dry_run_only or str(self.execution_mode or "").strip().lower() != "live":
            return
        permission_probe = self.trading_permission_probe_fn
        if not callable(permission_probe):
            raise IBKRTradingPermissionError(
                "IB Gateway live execution cannot verify non-transmitting order-write access."
            )

        original_raise_request_errors = getattr(ib, "RaiseRequestErrors", False)
        original_request_timeout = getattr(ib, "RequestTimeout", 0)
        read_only_errors: list[tuple[Any, str]] = []
        error_event = getattr(ib, "errorEvent", None)
        error_handler_registered = False

        def is_read_only_error(message: Any) -> bool:
            normalized = str(message).lower().replace("-", " ").replace("_", " ")
            return "read only" in " ".join(normalized.split())

        def capture_api_error(_request_id, error_code, error_message, _contract):
            if is_read_only_error(error_message):
                read_only_errors.append((error_code, str(error_message)))

        try:
            if error_event is not None:
                error_event += capture_api_error
                error_handler_registered = True
            # IBKR what-if validation exercises order-write access without creating a live order.
            ib.RaiseRequestErrors = True
            ib.RequestTimeout = self.connect_timeout_seconds
            probe_result = permission_probe(ib)
            probe_warning = getattr(probe_result, "warningText", "")
            if read_only_errors or is_read_only_error(probe_warning):
                raise IBKRTradingPermissionError(
                    "IB Gateway API is in Read-Only mode; live execution is disabled."
                )
        except TimeoutError as exc:
            if read_only_errors:
                raise IBKRTradingPermissionError(
                    "IB Gateway API is in Read-Only mode; live execution is disabled."
                ) from exc
            raise
        except (ConnectionError, OSError) as exc:
            if read_only_errors or is_read_only_error(exc):
                raise IBKRTradingPermissionError(
                    "IB Gateway API is in Read-Only mode; live execution is disabled."
                ) from exc
            raise
        except Exception as exc:
            if is_read_only_error(exc):
                raise IBKRTradingPermissionError(
                    "IB Gateway API is in Read-Only mode; live execution is disabled."
                ) from exc
            raise IBKRTradingPermissionError(
                "IB Gateway live execution could not verify non-transmitting order-write access "
                f"(error_type={type(exc).__name__})."
            ) from exc
        finally:
            if error_handler_registered:
                error_event -= capture_api_error
            ib.RaiseRequestErrors = original_raise_request_errors
            ib.RequestTimeout = original_request_timeout

    def connect_ib(self):
        self.ensure_event_loop_fn()
        host = self.host_resolver()
        last_error = None
        for attempt in range(1, self.connect_attempts + 1):
            client_id = self.ib_client_id + ((attempt - 1) * self.client_id_retry_offset)
            self.printer(
                "Connecting to IB gateway "
                f"{host}:{self.ib_port} "
                f"(client_id={client_id}, "
                f"attempt={attempt}/{self.connect_attempts}, "
                f"timeout={self.connect_timeout_seconds}s)",
                flush=True,
            )
            try:
                ib = self.connect_ib_fn(
                    host,
                    self.ib_port,
                    client_id,
                    timeout=self.connect_timeout_seconds,
                )
                try:
                    self.validate_configured_accounts(ib)
                    self.validate_trading_permissions(ib)
                except Exception:
                    disconnect_fn = getattr(ib, "disconnect", None)
                    if callable(disconnect_fn):
                        disconnect_fn()
                    raise
                return ib
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_error = exc
                self.printer(
                    "IB gateway connection attempt failed "
                    f"(attempt={attempt}/{self.connect_attempts}, "
                    f"client_id={client_id}, "
                    f"error_type={type(exc).__name__}, "
                    f"error={exc})",
                    flush=True,
                )
                if attempt < self.connect_attempts:
                    if callable(self.refresh_host_fn):
                        try:
                            host = self.refresh_host_fn()
                        except Exception as refresh_exc:
                            self.printer(
                                "IB gateway host refresh failed; retrying last-known-good host "
                                f"(host={host}, error_type={type(refresh_exc).__name__}, "
                                f"error={refresh_exc})",
                                flush=True,
                            )
                    if self.connect_retry_delay_seconds > 0:
                        self.sleep_fn(self.connect_retry_delay_seconds)
        raise IBKRGatewayUnavailableError(
            "IB gateway unavailable after "
            f"{self.connect_attempts} attempt(s) to {host}:{self.ib_port}: {last_error}"
        ) from last_error

    def get_current_portfolio(self, ib):
        snapshot = self.fetch_account_portfolio_snapshot(ib)
        positions = {}
        for position in snapshot.positions:
            positions[position.symbol] = {
                "quantity": float(position.quantity),
                "avg_cost": float(position.average_cost or 0.0),
            }
        account_values = {
            "equity": snapshot.total_equity,
            "buying_power": snapshot.buying_power or 0.0,
        }
        return positions, account_values

    def build_portfolio_snapshot(self, ib, *, get_current_portfolio_fallback=None):
        if hasattr(ib, "reqPositions"):
            return self.fetch_account_portfolio_snapshot(ib)
        positions, account_values = get_current_portfolio_fallback(ib)
        return PortfolioSnapshot(
            as_of=datetime.now(timezone.utc),
            total_equity=float(account_values.get("equity") or 0.0),
            buying_power=float(account_values.get("buying_power") or 0.0),
            positions=tuple(
                Position(
                    symbol=str(symbol).strip().upper(),
                    quantity=float(details.get("quantity") or 0),
                    market_value=float(details.get("quantity") or 0) * float(details.get("avg_cost") or 0.0),
                    average_cost=float(details.get("avg_cost") or 0.0),
                    currency=self.market_currency,
                )
                for symbol, details in dict(positions or {}).items()
            ),
        )

    def get_market_prices(self, ib, symbols):
        return self.application_get_market_prices_fn(
            ib,
            symbols,
            fetch_quote_snapshots=self.fetch_quote_snapshots_fn,
        )

    def check_order_submitted(self, report):
        return self.application_check_order_submitted_fn(report, translator=self.translator)

    def execute_rebalance(
        self,
        ib,
        target_weights,
        positions,
        account_values,
        *,
        strategy_symbols=None,
        signal_metadata=None,
    ):
        return self.application_execute_rebalance_fn(
            ib,
            target_weights,
            positions,
            account_values,
            fetch_quote_snapshots=self.fetch_quote_snapshots_fn,
            submit_order_intent=self.submit_order_intent_fn,
            order_intent_cls=self.order_intent_cls,
            translator=self.translator,
            strategy_symbols=strategy_symbols,
            signal_metadata=signal_metadata or {},
            strategy_profile=self.strategy_profile,
            account_group=self.account_group,
            service_name=self.service_name,
            account_ids=self.account_ids,
            dry_run_only=self.dry_run_only,
            execution_mode=self.execution_mode,
            cash_reserve_ratio=self.cash_reserve_ratio,
            cash_reserve_floor_usd=self.cash_reserve_floor_usd,
            rebalance_threshold_ratio=self.rebalance_threshold_ratio,
            limit_buy_premium=self.limit_buy_premium,
            limit_buy_premium_by_symbol=self.limit_buy_premium_by_symbol or {},
            quantity_step=self.quantity_step,
            min_order_notional=self.min_order_notional,
            safe_haven_cash_substitute_threshold_usd=self.safe_haven_cash_substitute_threshold_usd,
            market_currency=self.market_currency,
            sell_settle_delay_sec=self.sell_settle_delay_sec,
            return_summary=True,
            cash_only_execution=self.cash_only_execution,
            paper_execution_admission_enabled=self.paper_execution_admission_enabled,
            runtime_release_receipt=self.runtime_release_receipt,
            expected_strategy_release=self.expected_strategy_release,
        )

    def format_liquidation_orders(self, orders) -> str:
        preview = []
        for order in orders or ():
            symbol = str(order.get("symbol") or "").strip().upper()
            side = str(order.get("side") or "").strip().lower()
            quantity = float(order.get("quantity") or 0.0)
            status = str(order.get("status") or "").strip()
            if symbol:
                preview.append(f"{symbol} {side} {quantity:g} {status}".strip())
        return ", ".join(preview) if preview else self.translator("no_trades")

    def run_paper_liquidation_cycle(
        self,
        *,
        connect_ib_fn,
        get_current_portfolio_fn,
        publish_notification_fn,
    ):
        ib = connect_ib_fn()
        try:
            positions, _account_values = get_current_portfolio_fn(ib)
            if not positions:
                self.printer("paper_liquidation_positions_empty_retry", flush=True)
                self.sleep_fn(2.0)
                positions, _account_values = get_current_portfolio_fn(ib)
            summary = self.execute_paper_liquidation_fn(
                ib,
                positions,
                submit_order_intent=self.submit_order_intent_fn,
                order_intent_cls=self.order_intent_cls,
                dry_run_only=self.dry_run_only,
            )
            message = (
                f"{self.translator('rebalance_title')}\n"
                f"{self.translator('strategy_label', name=self.strategy_display_name)}\n"
                f"{self.translator('paper_liquidation_only')}\n"
                f"{self.translator('paper_liquidation_status', mode=summary['mode'], status=summary['execution_status'])}\n"
                f"{self.translator('paper_liquidation_positions_seen', count=summary['positions_seen'])}\n"
                f"{self.separator}\n"
                f"{self.format_liquidation_orders(summary.get('orders_submitted'))}"
            )
            publish_notification_fn(detailed_text=message, compact_text=message)
            return StrategyCycleResult(
                result="OK",
                execution_summary=dict(summary or {}),
            )
        finally:
            if ib is not None and hasattr(ib, "disconnect"):
                ib.disconnect()


def build_runtime_broker_adapters(
    *,
    host_resolver,
    ib_port: int,
    ib_client_id: int,
    connect_timeout_seconds: int,
    connect_attempts: int,
    connect_retry_delay_seconds: float,
    client_id_retry_offset: int,
    ensure_event_loop_fn,
    connect_ib_fn,
    fetch_portfolio_snapshot_fn,
    fetch_quote_snapshots_fn,
    submit_order_intent_fn,
    order_intent_cls=OrderIntent,
    application_get_market_prices_fn,
    application_check_order_submitted_fn,
    application_execute_rebalance_fn,
    execute_paper_liquidation_fn,
    translator,
    strategy_profile: str,
    account_group: str,
    service_name: str | None,
    account_ids: tuple[str, ...],
    dry_run_only: bool,
    cash_only_execution: bool = True,
    cash_reserve_ratio: float,
    cash_reserve_floor_usd: float,
    rebalance_threshold_ratio: float,
    limit_buy_premium: float,
    quantity_step: float,
    min_order_notional: float,
    safe_haven_cash_substitute_threshold_usd: float,
    sell_settle_delay_sec: float,
    separator: str,
    strategy_display_name: str,
    sleep_fn,
    market_currency: str = "USD",
    execution_mode: str = "paper",
    limit_buy_premium_by_symbol: dict[str, float] | None = None,
    printer=print,
    refresh_host_fn=None,
    trading_permission_probe_fn=None,
    paper_execution_admission_enabled: bool = False,
    runtime_release_receipt=None,
    expected_strategy_release=None,
) -> IBKRRuntimeBrokerAdapters:
    return IBKRRuntimeBrokerAdapters(
        host_resolver=host_resolver,
        ib_port=int(ib_port),
        ib_client_id=int(ib_client_id),
        connect_timeout_seconds=int(connect_timeout_seconds),
        connect_attempts=int(connect_attempts),
        connect_retry_delay_seconds=float(connect_retry_delay_seconds),
        client_id_retry_offset=int(client_id_retry_offset),
        ensure_event_loop_fn=ensure_event_loop_fn,
        connect_ib_fn=connect_ib_fn,
        fetch_portfolio_snapshot_fn=fetch_portfolio_snapshot_fn,
        fetch_quote_snapshots_fn=fetch_quote_snapshots_fn,
        submit_order_intent_fn=submit_order_intent_fn,
        order_intent_cls=order_intent_cls,
        application_get_market_prices_fn=application_get_market_prices_fn,
        application_check_order_submitted_fn=application_check_order_submitted_fn,
        application_execute_rebalance_fn=application_execute_rebalance_fn,
        execute_paper_liquidation_fn=execute_paper_liquidation_fn,
        translator=translator,
        strategy_profile=str(strategy_profile),
        account_group=str(account_group or ""),
        service_name=service_name,
        account_ids=tuple(account_ids),
        dry_run_only=bool(dry_run_only),
        cash_only_execution=bool(cash_only_execution),
        cash_reserve_ratio=float(cash_reserve_ratio),
        cash_reserve_floor_usd=float(cash_reserve_floor_usd),
        rebalance_threshold_ratio=float(rebalance_threshold_ratio),
        limit_buy_premium=float(limit_buy_premium),
        limit_buy_premium_by_symbol=dict(limit_buy_premium_by_symbol or {}),
        quantity_step=float(quantity_step),
        min_order_notional=float(min_order_notional),
        safe_haven_cash_substitute_threshold_usd=float(safe_haven_cash_substitute_threshold_usd),
        sell_settle_delay_sec=float(sell_settle_delay_sec),
        separator=str(separator or ""),
        strategy_display_name=str(strategy_display_name or ""),
        sleep_fn=sleep_fn,
        market_currency=str(market_currency or "USD").upper(),
        execution_mode=str(execution_mode or "paper").strip().lower().replace("-", "_"),
        printer=printer,
        refresh_host_fn=refresh_host_fn,
        trading_permission_probe_fn=trading_permission_probe_fn,
        paper_execution_admission_enabled=bool(paper_execution_admission_enabled),
        runtime_release_receipt=runtime_release_receipt,
        expected_strategy_release=expected_strategy_release,
    )
