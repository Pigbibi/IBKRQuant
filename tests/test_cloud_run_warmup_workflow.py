from pathlib import Path


def test_env_sync_creates_non_trading_health_warmup_with_retries() -> None:
    workflow = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")

    assert 'scheduler.get("probe_time")' in workflow
    assert 'warmup_job_name="${cloud_run_service%-service}-warmup-scheduler"' in workflow
    assert 'warmup_uri="${service_url}/health"' in workflow
    assert '--http-method=GET' in workflow
    assert '--max-retry-attempts=2' in workflow
    assert workflow.count('--max-retry-attempts=2') == 2
    assert '--attempt-deadline=60s' in workflow
    assert 'warmup_uri="${service_url}/healthz"' not in workflow
    assert 'warmup_uri="${service_url}/probe"' not in workflow


def test_main_scheduler_update_is_post_only_and_oidc_authenticated() -> None:
    workflow = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    for command in (
        'gcloud scheduler jobs update http "${job_name}"',
        'gcloud scheduler jobs create http "${job_name}"',
    ):
        command_block = workflow.split(command, 1)[1].split("--quiet", 1)[0]
        assert '--http-method=POST' in command_block
        assert '--oidc-service-account-email="${GCP_SCHEDULER_SERVICE_ACCOUNT}"' in command_block
        assert '--oidc-token-audience="${service_url}"' in command_block
    assert 'scheduler_uri="${service_url}/run"' in workflow
    assert 'warmup_uri="${service_url}/health"' in workflow


def test_cloud_run_deploy_is_private_internal_and_serial() -> None:
    workflow = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    deploy_block = workflow.split('gcloud run deploy "${cloud_run_service}"', 1)[1]
    deploy_block = deploy_block.split("--quiet", 1)[0]

    for option in (
        "--no-allow-unauthenticated",
        "--ingress=internal",
        "--max-instances=1",
        "--concurrency=1",
    ):
        assert option in deploy_block


def test_lifecycle_refreshes_after_successful_or_failed_deploy() -> None:
    workflow = Path(".github/workflows/runtime-target-lifecycle.yml").read_text(encoding="utf-8")
    deploy = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    deploy_name = deploy.splitlines()[0].removeprefix("name: ")

    assert f"  workflow_run:\n    workflows: [\"{deploy_name}\"]\n    types: [completed]" in workflow
    assert "github.event.workflow_run.conclusion" not in workflow
    assert "gh workflow run" not in workflow
    assert "gcloud run deploy" not in workflow
    assert "gcloud scheduler jobs resume" not in workflow
    assert "gcloud scheduler jobs pause" not in workflow


def test_lifecycle_observes_the_exact_existing_service_binding() -> None:
    workflow = Path(".github/workflows/runtime-target-lifecycle.yml").read_text(encoding="utf-8")
    publish = workflow.split("      - name: Publish lifecycle to the unified control plane", 1)[1]
    publish = publish.split("      - name:", 1)[0]

    assert 'observe-gcp: "true"' in publish
    assert "gcp-project: ${{ env.GCP_PROJECT_ID }}" in publish
    assert "cloud-run-region: ${{ env.CLOUD_RUN_REGION }}" in publish
    assert "cloud-run-service: ${{ matrix.target.service }}" in publish
    assert "scheduler-location: ${{ env.RUNTIME_HEARTBEAT_SCHEDULER_LOCATION }}" in publish


def _resolve_sync_plan(tmp_path, monkeypatch, *, selector="service-b", target="configured", services=None, mode="per_service"):
    import json
    import os
    import subprocess
    import sys
    import textwrap

    workflow = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Resolve admissible Cloud Run targets\n", 1)[1].split("      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    plan = {"mode": mode, "targets": [{"service_name": service, "env": {"RUNTIME_TARGET_ENABLED": "false"}} for service in (services or ["service-a", "service-b"])]}
    stub = tmp_path / "uv"
    stub.write_text(f"#!{sys.executable}\nimport os\nprint(os.environ['TEST_PLAN'])\n")
    stub.chmod(0o700)
    (tmp_path / "python").symlink_to(sys.executable)
    output = tmp_path / "output"
    github_env = tmp_path / "env"
    output.touch()
    github_env.touch()
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    env = {
        "PATH": os.environ["PATH"],
        "TEST_PLAN": json.dumps(plan),
        "WORKFLOW_TARGET": target,
        "INPUT_CONFIGURED_SERVICE": selector,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_ENV": str(github_env),
    }
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    return result, output.read_text(), github_env.read_text(), plan


def test_configured_service_selects_one_target_and_all_downstream_sources(tmp_path, monkeypatch) -> None:
    import json
    from scripts.reconcile_cloud_runtime import load_targets

    result, output, github_env, _ = _resolve_sync_plan(tmp_path, monkeypatch)
    assert result.returncode == 0, result.stderr
    plan = json.loads(output.split("\n", 1)[1].split("\n", 1)[0])
    assert [target["service_name"] for target in plan["targets"]] == ["service-b"]
    assert "CLOUD_RUN_SERVICES=service-b\n" in github_env
    assert "CLOUD_RUN_SERVICE=service-b\n" in github_env
    inventory = json.loads(github_env.split("CLOUD_RUN_SERVICE_TARGETS_JSON=", 1)[1].split("\n", 1)[0])
    assert inventory == {"targets": [{"service_name": "service-b"}]}
    scoped_env = dict(line.split("=", 1) for line in github_env.splitlines())
    scoped_env["SYNC_PLAN_JSON"] = json.dumps(plan)
    assert [target.service_name for target in load_targets(env=scoped_env)] == ["service-b"]
    assert plan["targets"][0]["env"] == {"RUNTIME_TARGET_ENABLED": "false"}
    assert result.stdout == ""


def test_configured_service_rejects_unmatched_duplicate_or_noninventory(tmp_path, monkeypatch) -> None:
    for index, kwargs in enumerate((
        {"selector": "missing"},
        {"services": ["service-b", "service-b"]},
        {"selector": " service-b"},
        {"selector": "service-b\nOTHER=value"},
        {"mode": "legacy"},
    )):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        result, output, github_env, _ = _resolve_sync_plan(case_path, monkeypatch, **kwargs)
        assert result.returncode != 0
        assert output == ""
        assert github_env == ""
        assert "service-b" not in result.stderr


def test_empty_selector_and_hk_verify_preserve_existing_targets(tmp_path, monkeypatch) -> None:
    import json

    for index, kwargs in enumerate(({"selector": ""}, {"target": "hk-verify"})):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        result, output, github_env, original = _resolve_sync_plan(case_path, monkeypatch, **kwargs)
        assert result.returncode == 0, result.stderr
        assert json.loads(output.split("\n", 1)[1].split("\n", 1)[0]) == original
        assert github_env == ""


def test_single_target_sync_skips_global_cleanup_before_other_service_mutation() -> None:
    import subprocess
    import textwrap

    workflow = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")
    assert "      configured_service:\n" in workflow
    assert "INPUT_CONFIGURED_SERVICE: ${{ inputs.configured_service }}" in workflow
    scope_guard = "(env.WORKFLOW_TARGET != 'configured' || env.INPUT_CONFIGURED_SERVICE == '')"
    for name in ("Prune old Cloud Run revisions", "Clean up old Cloud Run images"):
        step = workflow.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]
        assert scope_guard in step.split("        run:", 1)[0]
    cleanup = workflow.split("          reconcile_args=(", 1)[1].split('          for update in "${scheduler_updates[@]}";', 1)[0]
    assert 'if [ "${WORKFLOW_TARGET:-configured}" != "configured" ] || [ -z "${INPUT_CONFIGURED_SERVICE:-}" ]; then' in cleanup
    script = 'python3() { return 83; }\n' + textwrap.dedent("          reconcile_args=(" + cleanup)
    for target, selector, expected in (("configured", "service-b", 0), ("configured", "", 83), ("hk-verify", "service-b", 83)):
        result = subprocess.run(["/bin/bash", "-c", script], env={"WORKFLOW_TARGET": target, "INPUT_CONFIGURED_SERVICE": selector}, capture_output=True)
        assert result.returncode == expected
    assert workflow.index("Resolve admissible Cloud Run targets") < workflow.index("Verify deployed runtime target admission before traffic shift")
