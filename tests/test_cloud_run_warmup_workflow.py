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
