import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MIGRATION_DIR = Path(__file__).resolve().parents[1]
OPS_DIR = MIGRATION_DIR / "ops"
sys.path.insert(0, str(OPS_DIR))

import monitor_state  # noqa: E402
import release_manifest  # noqa: E402


class StatefulRunner:
    def __init__(
        self,
        *,
        fail_watchdog_start=False,
        inspection_failure=False,
        enabled_state_overrides=None,
    ):
        self.enabled = {
            monitor_state.MONITOR_UNIT: False,
            monitor_state.WATCHDOG_UNIT: False,
        }
        self.active = {
            monitor_state.MONITOR_UNIT: False,
            monitor_state.WATCHDOG_UNIT: False,
        }
        self.fail_watchdog_start = fail_watchdog_start
        self.inspection_failure = inspection_failure
        self.enabled_state_overrides = enabled_state_overrides or {}
        self.watchdog_start_failures = 0
        self.calls = []

    def run(self, arguments, *, cwd=None, env=None, check=True):
        del cwd, env
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if arguments[:2] == ("/usr/bin/systemctl", "is-enabled"):
            value = self.enabled_state_overrides.get(
                arguments[2],
                "enabled" if self.enabled[arguments[2]] else "disabled",
            )
            return monitor_state.CommandResult(0 if value == "enabled" else 1, value)
        if arguments[:2] == ("/usr/bin/systemctl", "is-active"):
            value = "active" if self.active[arguments[2]] else "inactive"
            return monitor_state.CommandResult(0 if value == "active" else 3, value)
        if arguments[:2] == ("/usr/bin/systemctl", "enable"):
            self.enabled[arguments[2]] = True
            return monitor_state.CommandResult(0, "")
        if arguments[:2] == ("/usr/bin/systemctl", "disable"):
            self.enabled[arguments[2]] = False
            return monitor_state.CommandResult(0, "")
        if arguments[:2] == ("/usr/bin/systemctl", "start"):
            if (
                arguments[2] == monitor_state.WATCHDOG_UNIT
                and self.fail_watchdog_start
                and self.watchdog_start_failures == 0
            ):
                self.watchdog_start_failures += 1
                raise monitor_state.HostOpsError("simulated start failure")
            self.active[arguments[2]] = True
            return monitor_state.CommandResult(0, "")
        if arguments[:2] == ("/usr/bin/systemctl", "stop"):
            self.active[arguments[2]] = False
            return monitor_state.CommandResult(0, "")
        if arguments[0:2] == ("/usr/bin/docker", "compose") and "ps" in arguments:
            if self.inspection_failure:
                return monitor_state.CommandResult(1, "")
            container_id = "container-id" if self.active[monitor_state.MONITOR_UNIT] else ""
            return monitor_state.CommandResult(0, container_id)
        if arguments[0:2] == ("/usr/bin/docker", "inspect"):
            return monitor_state.CommandResult(
                0,
                json.dumps({"Status": "running", "Health": {"Status": "healthy"}}),
            )
        return monitor_state.CommandResult(0, "")


class TestLifecycle(monitor_state.MonitorLifecycle):
    def preflight_enable(self):
        return None


class MonitorLifecycleTests(unittest.TestCase):
    def test_enable_and_disable_reconcile_both_units(self):
        runner = StatefulRunner()
        lifecycle = TestLifecycle(runner=runner)

        enabled = lifecycle.enable()
        self.assertEqual(enabled["desired_state"], "enabled")
        self.assertTrue(enabled["healthy"])
        self.assertTrue(runner.enabled[monitor_state.MONITOR_UNIT])
        self.assertTrue(runner.enabled[monitor_state.WATCHDOG_UNIT])

        disabled = lifecycle.disable()
        self.assertEqual(disabled["desired_state"], "disabled")
        self.assertTrue(disabled["healthy"])
        self.assertFalse(runner.active[monitor_state.MONITOR_UNIT])
        self.assertFalse(runner.active[monitor_state.WATCHDOG_UNIT])

    def test_failed_transition_restores_prior_enablement_and_activity(self):
        runner = StatefulRunner(fail_watchdog_start=True)
        lifecycle = TestLifecycle(runner=runner)

        with self.assertRaisesRegex(
            monitor_state.HostOpsError, "prior lifecycle state was restored"
        ):
            lifecycle.enable()

        self.assertFalse(runner.enabled[monitor_state.MONITOR_UNIT])
        self.assertFalse(runner.enabled[monitor_state.WATCHDOG_UNIT])
        self.assertFalse(runner.active[monitor_state.MONITOR_UNIT])
        self.assertFalse(runner.active[monitor_state.WATCHDOG_UNIT])

    def test_disabled_status_does_not_hide_docker_inspection_failure(self):
        lifecycle = TestLifecycle(runner=StatefulRunner(inspection_failure=True))
        status = lifecycle.status()
        self.assertEqual(status["desired_state"], "disabled")
        self.assertFalse(status["healthy"])
        self.assertEqual(status["container"]["status"], "inspection-failed")

    def test_status_labels_masked_or_missing_units_unsupported(self):
        runner = StatefulRunner(
            enabled_state_overrides={
                monitor_state.MONITOR_UNIT: "masked",
                monitor_state.WATCHDOG_UNIT: "not-found",
            }
        )
        status = TestLifecycle(runner=runner).status()
        self.assertEqual(status["desired_state"], "unsupported")
        self.assertFalse(status["healthy"])

    def test_transition_rejects_failed_unit_state_before_mutation(self):
        runner = StatefulRunner()
        original_run = runner.run

        def failed_monitor(arguments, **kwargs):
            if tuple(arguments) == (
                "/usr/bin/systemctl",
                "is-active",
                monitor_state.MONITOR_UNIT,
            ):
                return monitor_state.CommandResult(3, "failed")
            return original_run(arguments, **kwargs)

        runner.run = failed_monitor
        with self.assertRaisesRegex(monitor_state.HostOpsError, "activity is failed"):
            TestLifecycle(runner=runner).disable()
        self.assertFalse(any(call[1] in ("enable", "disable", "start", "stop") for call in runner.calls))


def make_manifest(artifact_name, artifact):
    digest = release_manifest._sha256(artifact)
    commit = "a" * 40
    image_digest = "sha256:" + "b" * 64
    return {
        "schema_version": 1,
        "release_id": "autoTasks@{}".format(commit),
        "source": {
            "repository": "autoTasks",
            "commit": commit,
            "tree": "c" * 40,
            "dirty_patch_sha256": None,
        },
        "build": {
            "generator": "migration/ops/release_manifest.py",
            "source_timestamp_utc": "2026-07-21T12:00:00Z",
        },
        "artifact": {
            "filename": artifact_name,
            "format": "git-archive-tar",
            "sha256": digest,
            "bytes": len(artifact),
        },
        "components": [{
            "name": "autoTasks",
            "commit": commit,
            "destination": "/opt/tellor/autoTasks",
        }],
        "images": [{
            "name": "openzeppelin-monitor",
            "reference": "openzeppelin/openzeppelin-monitor:v1.5.0@{}".format(image_digest),
            "digest": image_digest,
        }],
        "deployment": {
            "status": "not-deployed",
            "authorization_id": None,
            "account_id": None,
            "region": None,
            "instance_id": None,
            "deployed_at_utc": None,
            "rollback_release": "autoTasks@{}".format("d" * 40),
        },
    }


class ReleaseManifestTests(unittest.TestCase):
    def test_build_is_reproducible_and_refuses_later_untracked_content(self):
        image = (
            "openzeppelin/openzeppelin-monitor:v1.5.0@sha256:"
            + "b" * 64
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "release"
            (source / "migration").mkdir(parents=True)
            (source / "migration" / "docker-compose.yaml").write_text(
                "services:\n  monitor:\n    image: {}\n".format(image),
                encoding="utf-8",
            )
            (source / "README.md").write_text("release fixture\n", encoding="utf-8")
            subprocess.run(("git", "init", "-q"), cwd=source, check=True)
            subprocess.run(("git", "add", "."), cwd=source, check=True)
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Release Test",
                    "-c",
                    "user.email=release-test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ),
                cwd=source,
                check=True,
            )

            first = release_manifest.build_release(
                source, output, "autoTasks@{}".format("d" * 40)
            )
            first_manifest = first.read_bytes()
            first_artifact = (output / json.loads(first_manifest)["artifact"]["filename"]).read_bytes()
            second = release_manifest.build_release(
                source, output, "autoTasks@{}".format("d" * 40)
            )
            self.assertEqual(second.read_bytes(), first_manifest)
            self.assertEqual(
                (output / json.loads(first_manifest)["artifact"]["filename"]).read_bytes(),
                first_artifact,
            )

            (source / "untracked.txt").write_text("block release\n", encoding="utf-8")
            with self.assertRaisesRegex(release_manifest.ReleaseError, "dirty"):
                release_manifest.build_release(
                    source, root / "second-release", "autoTasks@{}".format("d" * 40)
                )

    def test_verify_and_record_deployment_are_digest_checked(self):
        artifact = b"deterministic release artifact"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_name = "autoTasks-aaaaaaaaaaaa.tar"
            (root / artifact_name).write_bytes(artifact)
            manifest_path = root / release_manifest.MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(make_manifest(artifact_name, artifact)), encoding="utf-8"
            )

            release_manifest.verify_release(manifest_path)
            release_manifest.record_deployment(
                manifest_path,
                authorization_id="change-123",
                account_id="075483720677",
                region="us-east-2",
                instance_id="i-00b865643d4023484",
                deployed_at_utc="2026-07-21T15:00:00Z",
            )
            deployed = release_manifest.verify_release(
                manifest_path, require_deployed=True
            )
            self.assertEqual(deployed["deployment"]["status"], "deployed")

            (root / artifact_name).write_bytes(b"tampered")
            with self.assertRaisesRegex(release_manifest.ReleaseError, "size"):
                release_manifest.verify_release(manifest_path)

    def test_manifest_prohibits_dirty_patch_hash(self):
        manifest = make_manifest("autoTasks-aaaaaaaaaaaa.tar", b"archive")
        manifest["source"]["dirty_patch_sha256"] = "e" * 64
        with self.assertRaisesRegex(release_manifest.ReleaseError, "prohibited"):
            release_manifest.validate_manifest(manifest)

    def test_manifest_rejects_type_confusion_and_release_mismatch_cleanly(self):
        manifest = make_manifest("autoTasks-aaaaaaaaaaaa.tar", b"archive")
        manifest["source"]["commit"] = 7
        with self.assertRaisesRegex(release_manifest.ReleaseError, "commit"):
            release_manifest.validate_manifest(manifest)

        manifest = make_manifest("autoTasks-aaaaaaaaaaaa.tar", b"archive")
        manifest["release_id"] = "autoTasks@{}".format("f" * 40)
        with self.assertRaisesRegex(release_manifest.ReleaseError, "does not match"):
            release_manifest.validate_manifest(manifest)

    def test_schema_is_json_and_declares_closed_top_level(self):
        schema = json.loads(
            (MIGRATION_DIR / "DEPLOYMENT_MANIFEST.schema.json").read_text()
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertFalse(schema["additionalProperties"])


class ArtifactSafetyTests(unittest.TestCase):
    def test_systemd_dropins_and_install_only_wrapper_encode_coupling(self):
        monitor_dropin = (
            OPS_DIR
            / "systemd"
            / "openzeppelin-monitor.service.d"
            / "10-watchdog-coupling.conf"
        ).read_text()
        watchdog_dropin = (
            OPS_DIR
            / "systemd"
            / "monitor-watchdog.timer.d"
            / "10-monitor-coupling.conf"
        ).read_text()
        installer = (OPS_DIR / "install-host-ops.sh").read_text()

        self.assertIn("Also=monitor-watchdog.timer", monitor_dropin)
        self.assertIn("Wants=monitor-watchdog.timer", monitor_dropin)
        self.assertIn("PartOf=openzeppelin-monitor.service", watchdog_dropin)
        for mutation in (
            "systemctl enable ",
            "systemctl disable ",
            "systemctl start ",
            "systemctl stop ",
        ):
            self.assertNotIn(mutation, installer)
        self.assertIn('"${SOURCE_DIR}/install-monitoring.sh"', installer)

    def test_inventory_has_only_read_only_aws_operations(self):
        inventory = (OPS_DIR / "inventory-readonly.sh").read_text()
        for prohibited in (
            "send-command",
            "start-session",
            "get-parameter",
            "put-parameter",
            "delete-parameter",
            "terminate-instances",
            "start-instances",
            "stop-instances",
        ):
            self.assertNotIn(prohibited, inventory)
        for required in (
            "get-caller-identity",
            "describe-instances",
            "describe-security-groups",
            "describe-volumes",
            "describe-instance-information",
            "describe-alarms",
            "list-recovery-points-by-resource",
            "get-instance-profile",
        ):
            self.assertIn(required, inventory)

    def test_workload_contract_keeps_unresolved_workloads_non_active(self):
        contract = (MIGRATION_DIR / "WORKLOAD_CONTRACT.md").read_text()
        self.assertIn("OpenZeppelin Monitor | `disabled`", contract)
        self.assertIn("Monitor watchdog | `derived-from: openzeppelin-monitor.service`", contract)
        self.assertIn("Reference-price prototype | `undecided-preserve-observed-state`", contract)
        self.assertIn("Big Mac CPI | `undecided-preserve-observed-state`", contract)


if __name__ == "__main__":
    unittest.main()
