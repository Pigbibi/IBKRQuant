"""HTTP route implementation handlers extracted from ``main`` (pure move).

These are the request-handling bodies for the read-only health probe,
broker-reconciliation, and monitor-dispatch endpoints. ``main.py`` keeps
owning the Flask ``app`` instance and every ``@app.route(...)`` decorator;
it imports the functions below and calls them from its existing thin
route wrappers (``handle_probe``, ``handle_reconciliation``,
``handle_monitor_dispatch``, ``health``).

``main.py`` still owns almost all of the mutable module-level state these
handlers read (config constants resolved once per process, plus functions
the test suite monkeypatches directly on ``main``, e.g.
``monkeypatch.setattr(strategy_module, "connect_ib", ...)``). Every such
name is therefore accessed as ``main.<name>`` (an attribute lookup on the
module object at call time) instead of a bound import, so this stays a
pure move with zero behavior change: unpatched, ``main.<name>`` resolves
to the exact same object a bare name would have; patched, it observes the
patch exactly like the original in-module code did.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback

from flask import request


class _MainModuleProxy:
    """Resolve ``main.<name>`` against ``sys.modules["main"]`` on every access.

    The test suite reloads ``main`` per test via
    ``sys.modules.pop("main", None); importlib.import_module("main")``,
    which creates a brand-new module object each time. A plain
    ``import main`` here would bind once to whichever ``main`` module
    object existed when ``application.http_routes`` first loaded (it is
    cached in ``sys.modules`` and not reloaded alongside ``main``), so it
    would go stale after the first test. Looking up ``sys.modules["main"]``
    fresh on every attribute access keeps this in sync with whichever
    ``main`` module is currently active, which is what makes every
    ``monkeypatch.setattr(strategy_module, "...", ...)`` in the test suite
    observable here -- exactly like the original in-module code.
    """

    def __getattr__(self, name):
        return getattr(sys.modules["main"], name)


main = _MainModuleProxy()


_SCHEDULER_JOB_NAME_PATTERN = re.compile(
    r"projects/[A-Za-z0-9-]+/locations/[A-Za-z0-9-]+/jobs/[A-Za-z0-9_-]+"
)


def _build_health_probe_connection_error_message(exc: Exception) -> str:
    return f"{main.t('health_probe_title')}\n{main.t('ibkr_connection_error_prefix')}{str(exc)}"


def _handle_probe(*, response_body: str = "Probe OK"):
    ib = None
    log_context = None
    report = None
    try:
        log_context = main.build_request_log_context()
        report = main.build_execution_report(log_context, dry_run_only_override=True)
        main.log_runtime_event(
            log_context,
            "health_probe_received",
            message="Received health probe request",
            http_method=request.method,
            execution_window="probe",
        )
        ib = main.connect_ib(
            read_only=True,
            validate_trading_permissions=False,
        )
        snapshot = main.build_portfolio_snapshot(ib)
        positions = tuple(getattr(snapshot, "positions", ()) or ())
        buying_power = float(getattr(snapshot, "buying_power", 0.0) or 0.0)
        total_equity = float(getattr(snapshot, "total_equity", 0.0) or 0.0)
        main.finalize_runtime_report(
            report,
            status="ok",
            summary={
                "buying_power": buying_power,
                "total_equity": total_equity,
                "positions_count": len(positions),
            },
        )
        main.log_runtime_event(
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
            main.append_runtime_report_error(
                report,
                stage="health_probe",
                message=str(exc),
                error_type=type(exc).__name__,
                failure_category="ibkr_connection",
            )
            main.finalize_runtime_report(
                report,
                status="error",
                diagnostics={"probe_failure_category": "ibkr_connection"},
            )
        if log_context is not None:
            main.log_runtime_event(
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
        main._publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 500
    except Exception as exc:
        if report is not None:
            main.append_runtime_report_error(
                report,
                stage="health_probe",
                message=str(exc),
                error_type=type(exc).__name__,
            )
            main.finalize_runtime_report(report, status="error")
        if log_context is not None:
            main.log_runtime_event(
                log_context,
                "health_probe_failed",
                message="Health probe failed",
                severity="ERROR",
                execution_window="probe",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        error_msg = f"{main.t('health_probe_title')}\n{main.t('health_probe_error_prefix')}{traceback.format_exc()}"
        main._publish_runtime_failure_notification(
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
                report_path = main.persist_execution_report(report, dry_run_only_override=True)
                print(f"execution_report {report_path}", flush=True)
        except Exception as persist_exc:
            print(f"failed to persist execution report: {persist_exc}", flush=True)


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
        reason = (
            "missing_scheduler_identity"
            if request.headers.get("X-CloudScheduler-JobName") is None
            else "invalid_scheduler_identity"
        )
        print(json.dumps({"event": "broker_reconciliation_rejected", "reason": reason}), flush=True)
        return "Error", 400
    reconciliation_request_id = main.normalize_reconciliation_request_id(
        request.headers.get("X-QSL-Reconciliation-Request-Id")
    )
    try:
        log_context = main.build_request_log_context()
        report = main.build_execution_report(log_context, dry_run_only_override=True)
        report.setdefault("diagnostics", {})["reconciliation_scheduler_job_sha256"] = scheduler_job_sha256
        if reconciliation_request_id is not None:
            report.setdefault("diagnostics", {})["reconciliation_request_id"] = reconciliation_request_id
        runtime_target = main.RUNTIME_SETTINGS.runtime_target
        if runtime_target is None:
            raise main.IBKRReconciliationReadError(
                "IBKR reconciliation requires an explicit runtime target."
            )
        main.log_runtime_event(
            log_context,
            "broker_reconciliation_received",
            message="Received broker reconciliation request",
            execution_window="reconciliation",
        )
        ib = main.connect_ib(
            read_only=True,
            validate_trading_permissions=False,
        )
        observations = main.collect_read_only_reconciliation_observations(
            ib,
            account_ids=main.ACCOUNT_IDS,
            fetch_portfolio_snapshot=main.fetch_portfolio_snapshot,
            market_currency=main.MARKET_CURRENCY,
            cash_only_execution=main.CASH_ONLY_EXECUTION,
        )
        candidate = main.build_reconciliation_candidate(
            observations=observations,
            runtime_target=runtime_target,
            platform_id=runtime_target.platform_id,
            strategy_profile=main.STRATEGY_PROFILE,
            account_group=main.ACCOUNT_GROUP,
            project_id=main.PROJECT_ID,
        )
        payload = candidate.to_safe_dict()
        main.finalize_runtime_report(
            report,
            status="ok",
            summary={
                "broker_reconciliation_permits_active_lkg": candidate.permits_active_lkg,
                "broker_reconciliation_blockers_count": len(candidate.recovery_blockers),
                "broker_reconciliation_ledger_records_count": candidate.execution_ledger_records_count,
            },
            diagnostics={"broker_reconciliation": payload},
        )
        main.log_runtime_event(
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
    except (main.IBKRReconciliationReadError, ConnectionError, TimeoutError) as exc:
        if report is not None:
            main.append_runtime_report_error(
                report,
                stage="broker_reconciliation",
                message="Broker reconciliation failed.",
                error_type=type(exc).__name__,
                failure_category="broker_reconciliation",
            )
            main.finalize_runtime_report(
                report,
                status="error",
                diagnostics={"broker_reconciliation_failure": type(exc).__name__},
            )
        if log_context is not None:
            main.log_runtime_event(
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
            main.append_runtime_report_error(
                report,
                stage="broker_reconciliation",
                message="Broker reconciliation failed.",
                error_type=type(exc).__name__,
            )
            main.finalize_runtime_report(report, status="error")
        if log_context is not None:
            main.log_runtime_event(
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
                report_path = main.persist_reconciliation_report(report)
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

    log_context = main.build_request_log_context()
    targets = main.load_monitor_targets()
    result = main.dispatch_due_monitor_targets(
        targets,
        lookback_minutes=main.lookback_minutes_from_env(),
        timeout_seconds=main.timeout_seconds_from_env(),
        max_workers=main.max_workers_from_env(),
        local_service_name=main.SERVICE_NAME or os.getenv("K_SERVICE"),
        local_dispatch_fn=_dispatch_local_monitor,
    )
    main.log_runtime_event(
        log_context,
        "monitor_dispatch_completed",
        message="Monitor dispatch completed",
        monitor_targets_count=len(targets),
        dispatches_due=result.get("dispatches_due"),
        dispatches_sent=result.get("dispatches_sent"),
        dispatch_results=result.get("results") or [],
    )
    if any(bool(item.get("worker_recycle_required")) for item in (result.get("results") or [])):
        main.recycle_current_process_after_response()
    return result, 200 if result.get("ok") else 502


def _dispatch_local_monitor(dispatch):
    window = str(dispatch.get("window") or "").strip()
    if window == "probe":
        _body, status_code = _handle_probe()
        recycle_required = False
    elif window == "precheck":
        timeout_state: dict[str, bool] = {}
        _body, status_code = main._handle_dry_run_with_deadline(
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


def health_impl():
    critical_errors: list[str] = []
    for module_path in (
        "application.runtime_composer",
        "application.runtime_reporting_adapters",
        "application.runtime_strategy_adapters",
        "runtime_execution_policy",
    ):
        try:
            main.importlib.import_module(module_path)
        except Exception as exc:
            critical_errors.append(f"{module_path}: {type(exc).__name__}: {exc}")
    if critical_errors:
        return json.dumps({"status": "unhealthy", "errors": critical_errors}, ensure_ascii=False), 500
    return "OK", 200
