import sys
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from scripts import validate_cloud_run_startup


ROOT = Path(__file__).resolve().parents[1]


def test_production_startup_validation_is_a_ci_and_image_build_gate():
    command = "python scripts/validate_cloud_run_startup.py"
    dockerfile = (ROOT / "Dockerfile").read_text()
    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert command in dockerfile
    assert "Validate production Cloud Run startup" in ci_workflow
    assert f"uv run --no-sync {command}" in ci_workflow


def test_startup_validation_accepts_post_only_execution_routes(monkeypatch):
    app = Flask(__name__)
    for index, (path, methods) in enumerate(
        {
            "/health": ["GET"],
            "/run": ["POST"],
            "/dry-run": ["GET", "POST"],
            "/probe": ["POST"],
            "/monitor-dispatch": ["GET", "POST"],
        }.items()
    ):
        app.add_url_rule(path, f"route_{index}", lambda: "ok", methods=methods)

    monkeypatch.setattr(validate_cloud_run_startup, "_install_smoke_environment", lambda: None)
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(app=app))

    validate_cloud_run_startup.validate_startup()
