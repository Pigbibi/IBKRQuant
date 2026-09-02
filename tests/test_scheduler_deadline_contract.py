from __future__ import annotations

import re
from pathlib import Path

from scripts import build_cloud_run_env_sync_plan as sync_plan


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-cloud-run-env.yml"
MAIN = ROOT / "main.py"


def test_live_gateway_run_deadline_exceeds_cloud_run_timeout() -> None:
    assert sync_plan.RUN_SCHEDULER_ATTEMPT_DEADLINE == "330s"

    assert sync_plan._requires_extended_run_deadline(
        {"execution_mode": "live", "dry_run_only": False},
        {"IBKR_EXECUTION_BACKEND": "gateway"},
    )
    assert not sync_plan._requires_extended_run_deadline(
        {"execution_mode": "paper", "dry_run_only": True},
        {"IBKR_EXECUTION_BACKEND": "gateway"},
    )
    assert not sync_plan._requires_extended_run_deadline(
        {"execution_mode": "live", "dry_run_only": False},
        {"IBKR_EXECUTION_BACKEND": "quantconnect"},
    )

    workflow = WORKFLOW.read_text(encoding="utf-8")
    cloud_run_timeout = re.search(r"--timeout=(\d+)s", workflow)
    assert cloud_run_timeout is not None
    assert int(sync_plan.RUN_SCHEDULER_ATTEMPT_DEADLINE.removesuffix("s")) > int(
        cloud_run_timeout.group(1)
    )


def test_run_deadline_is_plan_scoped_and_warmup_remains_60_seconds() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'str(scheduler.get("attempt_deadline") or "")' in workflow
    assert "main_attempt_deadline_args=()" in workflow
    assert 'main_attempt_deadline_args+=(--attempt-deadline="${main_attempt_deadline}")' in workflow
    assert workflow.count('"${main_attempt_deadline_args[@]}"') == 2
    assert workflow.count("--attempt-deadline=60s") == 2


def test_precheck_uses_per_service_scheduler_with_bounded_deadline() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    main = MAIN.read_text(encoding="utf-8")

    assert 'precheck_job_name="${cloud_run_service%-service}-precheck-scheduler"' in workflow
    assert 'precheck_uri="${service_url}/dry-run"' in workflow
    assert workflow.count("--attempt-deadline=120s") == 2
    assert workflow.count("--max-retry-attempts=0") == 2
    assert workflow.count("--max-retry-duration=0s") == 2
    assert 'managed_scheduler_jobs=("${job_name}" "${warmup_job_name}" "${precheck_job_name}")' in workflow
    assert 'monitor_job_name="interactive-brokers-monitor-dispatcher-scheduler"' not in workflow
    assert 'shared_env_pairs+=("IBKR_MONITOR_DISPATCH_TARGETS_JSON=' not in workflow

    strategy_deadline = re.search(r'IBKR_DRY_RUN_DEADLINE_SECONDS = get_positive_int_env\([^,]+, (\d+)\)', main)
    report_grace = re.search(r'IBKR_DEADLINE_REPORT_GRACE_SECONDS = get_positive_int_env\([^,]+, (\d+)\)', main)
    assert strategy_deadline is not None
    assert report_grace is not None
    assert 120 > int(strategy_deadline.group(1)) + int(report_grace.group(1))


def test_disabled_target_gets_a_paused_canonical_precheck_before_legacy_cleanup() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    ensure_precheck = workflow.index('if [ -n "${precheck_state}" ]; then')
    enabled_state = workflow.index('case "${standard_execution_enabled}" in')
    pause_precheck = workflow.index('gcloud scheduler jobs pause "${managed_job_name}"')
    retire_legacy = workflow.index('python3 scripts/reconcile_cloud_runtime.py "${reconcile_args[@]}"')

    assert ensure_precheck < enabled_state < pause_precheck < retire_legacy


def test_runtime_target_state_controls_all_per_service_schedulers() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'managed_scheduler_jobs=("${job_name}" "${warmup_job_name}" "${precheck_job_name}")' in workflow
    assert 'for managed_job_name in "${managed_scheduler_jobs[@]}"; do' in workflow
    assert 'gcloud scheduler jobs resume "${managed_job_name}"' in workflow
    assert 'gcloud scheduler jobs pause "${managed_job_name}"' in workflow
    assert 'gcloud scheduler jobs pause "${precheck_job_name}"' not in workflow


def test_legacy_dispatcher_is_retired_after_replacement_jobs_are_ready() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    env_sync_start = workflow.index("shared_remove_env_vars=(")
    env_sync_end = workflow.index("shared_remove_secret_vars=(")
    hk_migration_guard = workflow.index("HK verify scheduler migration requires the configured migration first.")
    replacement_jobs = workflow.index('precheck_job_name="${cloud_run_service%-service}-precheck-scheduler"')
    retire_legacy = workflow.index('python3 scripts/reconcile_cloud_runtime.py "${reconcile_args[@]}"')
    remove_targets = workflow.index("--remove-env-vars=IBKR_MONITOR_DISPATCH_TARGETS_JSON")

    assert "IBKR_MONITOR_DISPATCH_TARGETS_JSON" not in workflow[env_sync_start:env_sync_end]
    assert hk_migration_guard < replacement_jobs < retire_legacy < remove_targets
    assert 'Skipping precheck scheduler migration for the isolated HK verify target.' not in workflow
    assert 'reconcile_args+=(--preserve-shared-monitor-dispatcher)' not in workflow
