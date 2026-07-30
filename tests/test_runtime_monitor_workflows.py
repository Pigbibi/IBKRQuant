from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_execution_report_heartbeat_has_market_neutral_daily_schedule() -> None:
    workflow = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()

    assert 'cron: "20 22 * * *"' in workflow
    assert 'cron: "20 22 * * 1-5"' not in workflow
    assert "RUNTIME_HEARTBEAT_MARKET_AWARE:" in workflow
    assert "pandas-market-calendars==5.4.0" in workflow


def test_runtime_monitor_workflows_retry_gcp_authentication() -> None:
    for name in ("execution-report-heartbeat.yml", "runtime-guard.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()

        assert workflow.count("google-github-actions/auth@v3") == 2
        assert "id: gcp_auth_primary" in workflow
        assert "continue-on-error: true" in workflow
        assert "steps.gcp_auth_primary.outcome == 'failure'" in workflow
