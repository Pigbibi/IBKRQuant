from pathlib import Path


def test_pyproject_declares_runtime_and_test_dependencies() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "dependencies = [" in pyproject
    assert "quant-platform-kit @ git+https://github.com/QuantStrategyLab/" in pyproject
    assert "us-equity-strategies @ git+https://github.com/QuantStrategyLab/" in pyproject
    assert "hk-equity-strategies @ git+https://github.com/QuantStrategyLab/" in pyproject
    assert "[project.optional-dependencies]" in pyproject
    assert "test = [" in pyproject


def test_ci_docker_and_runtime_monitoring_use_uv_lock() -> None:
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    env_sync = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    runtime_guard = Path(".github/workflows/runtime-guard.yml").read_text(encoding="utf-8")
    runtime_target_lifecycle = Path(".github/workflows/runtime-target-lifecycle.yml").read_text(
        encoding="utf-8"
    )
    execution_report_heartbeat = Path(".github/workflows/execution-report-heartbeat.yml").read_text(
        encoding="utf-8"
    )
    lockfile = Path("uv.lock").read_text(encoding="utf-8")

    assert lockfile.startswith("version = ")
    assert "uv sync --frozen --extra test" in ci
    assert "uv run --no-sync ruff check --exclude external ." in ci
    assert "uv run --no-sync python external/QuantPlatformKit/scripts/check_qpk_pin_consistency.py" in ci
    assert "uv sync --frozen --no-dev" in env_sync
    assert "uv run --no-sync python scripts/build_cloud_run_env_sync_plan.py --json" in env_sync
    setup_uv = "uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78"
    for workflow in (runtime_guard, runtime_target_lifecycle, execution_report_heartbeat):
        assert setup_uv in workflow
        assert workflow.count(setup_uv) == 1
        assert workflow.index(setup_uv) < workflow.index("google-github-actions/auth@v3")
        assert "actions/setup-python" not in workflow
        assert "python -m pip install" not in workflow
        assert workflow.count("uv sync --frozen --no-dev") == 1
    assert "uv run --no-sync python scripts/cloud_run_runtime_guard.py" in runtime_guard
    assert "uv run --no-sync python scripts/cloud_run_runtime_guard.py" in runtime_target_lifecycle
    assert "uv run --no-sync python scripts/execution_report_heartbeat.py" in runtime_target_lifecycle
    assert "uv run --no-sync python scripts/execution_report_heartbeat.py" in execution_report_heartbeat
    assert "run: python scripts/cloud_run_runtime_guard.py" not in runtime_guard
    assert "          python scripts/cloud_run_runtime_guard.py" not in runtime_target_lifecycle
    assert 'name = "pandas-market-calendars"' in lockfile
    assert 'pandas-market-calendars==5.4.0' not in runtime_target_lifecycle
    assert 'pandas-market-calendars==5.4.0' not in execution_report_heartbeat
    assert "Traceback|ImportError|ModuleNotFoundError" in runtime_target_lifecycle
    assert "IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_URI" in env_sync
    assert "Fetch opt-in immutable recovery state ledger" in env_sync
    assert "gcloud storage cp --quiet" in env_sync
    assert "COPY . ." in dockerfile
    assert dockerfile.index("COPY . .") < dockerfile.index("uv sync --frozen --no-dev")
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "python -m pip install -r requirements.txt" not in dockerfile
    assert "--no-install-project" not in ci
    assert "--no-install-project" not in env_sync
    assert "--no-install-project" not in dockerfile
