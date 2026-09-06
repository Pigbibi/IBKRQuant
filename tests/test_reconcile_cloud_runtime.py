from __future__ import annotations

import json
import unittest
from unittest import mock

from scripts import reconcile_cloud_runtime as rcr


class ReconcileCloudRuntimeTests(unittest.TestCase):
    def test_load_targets_reads_all_supported_env_shapes(self) -> None:
        env = {
            "SYNC_PLAN_JSON": json.dumps(
                {
                    "targets": [
                        {
                            "runtime_target_json": json.dumps(
                                {
                                    "service_name": "interactive-brokers-quant-live-u1234-service",
                                    "account_scope": "u1234",
                                }
                            ),
                            "region": "us-central1",
                        }
                    ]
                }
            ),
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        {"service_name": "interactive-brokers-quant-live-u1234-service"},
                        {"service_name": "interactive-brokers-quant-live-u5678-service", "region": "asia-east1"},
                    ]
                }
            ),
            "CLOUD_RUN_SERVICES": "interactive-brokers-quant-live-u9999-service, extra-service;interactive-brokers-quant-live-u1234-service",
        }

        targets = rcr.load_targets(env=env)

        self.assertEqual(
            [target.service_name for target in targets],
            [
                "interactive-brokers-quant-live-u1234-service",
                "interactive-brokers-quant-live-u5678-service",
                "interactive-brokers-quant-live-u9999-service",
                "extra-service",
            ],
        )
        self.assertEqual(targets[0].region, "us-central1")
        self.assertEqual(targets[0].account_scope, "u1234")
        self.assertEqual(targets[1].region, "asia-east1")

    def test_legacy_jobs_for_ibkr_service_include_only_explicit_candidates(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service")

        self.assertEqual(
            set(rcr._legacy_jobs_for_target("ibkr", target)),
            {
                "interactive-brokers-quant-live-u1234-service-probe-scheduler",
                "interactive-brokers-quant-live-u1234-service-precheck-scheduler",
                "interactive-brokers-quant-live-u1234-probe-scheduler",
                "ibkr-u1234-backup-execution",
                "ibkr-u1234-pre-market-dry-run",
                "interactive-brokers-monitor-dispatcher-scheduler",
            },
        )

    def test_ensure_latest_traffic_updates_to_commit_revision(self) -> None:
        target = rcr.RuntimeTarget(
            service_name="interactive-brokers-quant-live-u1234-service",
            region="us-central1",
        )
        rev_name = "interactive-brokers-quant-live-u1234-service-00001-abc"
        stale = "interactive-brokers-quant-live-u1234-service-00000-old"
        calls: list[list[str]] = []
        service_describe_count = 0

        def fake_run(args, *, json_output=False, dry_run=False):
            nonlocal service_describe_count
            calls.append(list(args))
            self.assertFalse(dry_run)
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                service_describe_count += 1
                traffic_rev = stale if service_describe_count == 1 else rev_name
                return {
                    "status": {
                        "latestReadyRevisionName": stale,
                        "traffic": [{"percent": 100, "revisionName": traffic_rev}],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "list"]:
                return [
                    {
                        "metadata": {
                            "name": rev_name,
                            "labels": {"commit-sha": "abc123"},
                        },
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ]
            if args[:4] == ["gcloud", "run", "services", "update-traffic"]:
                return ""
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            rcr.ensure_latest_traffic(
                project="interactivebrokersquant",
                region="us-central1",
                targets=[target],
                expected_commit="abc123",
                dry_run=False,
            )

        self.assertEqual(service_describe_count, 2)
        traffic_cmds = [cmd for cmd in calls if cmd[:4] == ["gcloud", "run", "services", "update-traffic"]]
        self.assertEqual(len(traffic_cmds), 1)
        self.assertIn(f"--to-revisions={rev_name}=100", traffic_cmds[0])
        self.assertNotIn("--to-latest", traffic_cmds[0])

    def test_ensure_latest_traffic_requires_matching_commit_revision(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service", region="us-central1")

        def fake_run(args, *, json_output=False, dry_run=False):
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                return {
                    "status": {
                        "latestCreatedRevisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                        "latestReadyRevisionName": "interactive-brokers-quant-live-u1234-service-00000-old",
                        "traffic": [],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "list"]:
                return []
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            with self.assertRaisesRegex(rcr.ReconcileError, "No Ready revision"):
                rcr.ensure_latest_traffic(
                    project="interactivebrokersquant",
                    region="us-central1",
                    targets=[target],
                    expected_commit="abc123",
                    dry_run=False,
                )

    def test_ensure_latest_traffic_routes_when_latest_ready_is_stale(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service", region="us-central1")
        stale = "interactive-brokers-quant-live-u1234-service-00001"
        fresh = "interactive-brokers-quant-live-u1234-service-00003"
        calls: list[list[str]] = []

        def fake_run(args, *, json_output=False, dry_run=False):
            calls.append(list(args))
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                traffic_rev = stale
                if any(cmd[:4] == ["gcloud", "run", "services", "update-traffic"] for cmd in calls[:-1]):
                    traffic_rev = fresh
                return {
                    "status": {
                        "latestReadyRevisionName": stale,
                        "traffic": [{"percent": 100, "revisionName": traffic_rev}],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "list"]:
                return [
                    {
                        "metadata": {"name": fresh, "labels": {"commit-sha": "abc123"}},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                    {
                        "metadata": {"name": stale, "labels": {"commit-sha": "old999"}},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                ]
            if args[:4] == ["gcloud", "run", "services", "update-traffic"]:
                return ""
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            rcr.ensure_latest_traffic(
                project="interactivebrokersquant",
                region="us-central1",
                targets=[target],
                expected_commit="abc123",
                dry_run=False,
            )

        traffic_cmds = [cmd for cmd in calls if cmd[:4] == ["gcloud", "run", "services", "update-traffic"]]
        self.assertEqual(len(traffic_cmds), 1)
        self.assertIn(f"--to-revisions={fresh}=100", traffic_cmds[0])

    def test_delete_legacy_schedulers_deletes_only_known_jobs(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service")
        expected_jobs = {
            "interactive-brokers-quant-live-u1234-service-probe-scheduler",
            "interactive-brokers-quant-live-u1234-service-precheck-scheduler",
            "interactive-brokers-quant-live-u1234-probe-scheduler",
            "ibkr-u1234-backup-execution",
            "ibkr-u1234-pre-market-dry-run",
            "interactive-brokers-monitor-dispatcher-scheduler",
        }
        describe_calls: list[list[str]] = []
        delete_calls: list[list[str]] = []

        def fake_run_optional(args, *, dry_run=False):
            self.assertFalse(dry_run)
            describe_calls.append(list(args))
            self.assertEqual(args[:4], ["gcloud", "scheduler", "jobs", "describe"])
            self.assertIn(args[4], expected_jobs)
            self.assertIn("--location=us-central1", args)
            return True

        def fake_run(args, *, json_output=False, dry_run=False):
            self.assertFalse(json_output)
            self.assertFalse(dry_run)
            delete_calls.append(list(args))
            self.assertEqual(args[:4], ["gcloud", "scheduler", "jobs", "delete"])
            self.assertIn(args[4], expected_jobs)
            self.assertIn("--location=us-central1", args)
            return ""

        with mock.patch.object(rcr, "_run_optional", side_effect=fake_run_optional), mock.patch.object(
            rcr, "_run", side_effect=fake_run
        ):
            rcr.delete_legacy_schedulers(
                platform="ibkr",
                project="interactivebrokersquant",
                region="us-central1",
                scheduler_location="us-central1",
                targets=[target],
                env={},
                dry_run=False,
            )

        self.assertEqual({call[4] for call in describe_calls}, expected_jobs)
        self.assertEqual({call[4] for call in delete_calls}, expected_jobs)

if __name__ == "__main__":
    unittest.main()
