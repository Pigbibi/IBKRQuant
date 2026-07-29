from pathlib import Path


def test_pyproject_declares_runtime_and_test_dependencies() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "dependencies = [" in pyproject
    assert "quant-platform-kit @ git+https://github.com/QuantStrategyLab/" in pyproject
    assert "us-equity-strategies @ git+https://github.com/QuantStrategyLab/" in pyproject
    assert "hk-equity-strategies @ git+https://github.com/QuantStrategyLab/" in pyproject
    assert "[project.optional-dependencies]" in pyproject
    assert "test = [" in pyproject


def test_ci_docker_and_env_sync_use_uv_lock() -> None:
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    env_sync = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    lockfile = Path("uv.lock").read_text(encoding="utf-8")

    assert lockfile.startswith("version = ")
    assert "uv sync --frozen --extra test" in ci
    assert "uv run --no-sync ruff check --exclude external ." in ci
    assert "uv run --no-sync python external/QuantPlatformKit/scripts/check_qpk_pin_consistency.py" in ci
    assert "uv sync --frozen --no-dev" in env_sync
    assert "uv run --no-sync python scripts/build_cloud_run_env_sync_plan.py --json" in env_sync
    assert "COPY . ." in dockerfile
    assert dockerfile.index("COPY . .") < dockerfile.index("uv sync --frozen --no-dev")
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "python -m pip install -r requirements.txt" not in dockerfile
    assert "--no-install-project" not in ci
    assert "--no-install-project" not in env_sync
    assert "--no-install-project" not in dockerfile
