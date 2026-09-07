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


def test_lifecycle_observes_production_drift_without_optimization() -> None:
    workflow = Path(".github/workflows/runtime-target-lifecycle.yml").read_text(encoding="utf-8")

    assert "scripts/production_drift_health_observe.py" in workflow
    assert "id: production_drift" in workflow
    assert "LIFECYCLE_PERFORMANCE_BUCKET" in workflow
    assert "| Production drift |" in workflow
    assert "run_research_promotion_cycle" not in workflow
