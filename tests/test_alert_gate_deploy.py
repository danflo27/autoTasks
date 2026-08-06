"""Shell-level regression tests for the deploy-production.sh safety gates.

These tests exercise the *real* deploy-production.sh script (via `sh`) rather
than re-implementing its logic in Python, because the bug being guarded
against is specifically in the shell gate parsing. They stub out `docker` so
no container is ever built or started -- the script's own compose calls
happen unconditionally before the action-specific gate logic runs, so a fake
`docker` on PATH that always exits 0 lets the script reach (and only reach)
the validation path we care about. If a run manages to reach the real
`compose up -d --remove-orphans` step, the fake docker logs it, and the test
asserts that call never happened -- i.e. that a rejected environment never
got anywhere near "starting services".

These cover the shell deploy gate rather than the tellor_alert_gate package,
but the filename stays inside the `test_alert_gate_*.py` discovery pattern so
the single documented check command runs them too.
"""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy-production.sh"

FAKE_DOCKER = """#!/bin/sh
# Records every invocation and always succeeds, so deploy-production.sh's
# unconditional `compose config`/`compose build`/`compose run ... --check`
# calls (which happen before the action-specific gate we're testing) never
# touch a real docker daemon.
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
exit 0
"""


def _write(path, content, mode=None):
    path.write_text(content)
    if mode is not None:
        path.chmod(mode)


class DeployGateCrlfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-gate-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.script_dir = Path(self.tmp)

        # Fake docker on PATH so no real container is built or started.
        bin_dir = self.script_dir / "bin"
        bin_dir.mkdir()
        docker_stub = bin_dir / "docker"
        _write(docker_stub, FAKE_DOCKER, 0o700)
        self.docker_call_log = self.script_dir / "docker-calls.log"
        self.docker_call_log.write_text("")

        # Copy the real script into the temp script_dir so its
        # self-relative paths (.env.production, secrets/, runtime/) resolve
        # inside the sandbox instead of the real repository.
        self.script_path = self.script_dir / "deploy-production.sh"
        shutil.copyfile(DEPLOY_SCRIPT, self.script_path)
        self.script_path.chmod(0o700)
        # compose_file just needs to exist; the fake docker never reads it.
        (self.script_dir / "docker-compose.production.yaml").write_text("")

        # Pre-create secrets required regardless of the CRLF bug so the
        # script reaches the TELLOR_ALERT_DELIVERY_MODE gate under test,
        # rather than exiting earlier for an unrelated missing-file reason.
        secrets_dir = self.script_dir / "secrets"
        secrets_dir.mkdir()
        _write(secrets_dir / "bridge_ledger_seed.json", "{}", 0o600)
        _write(secrets_dir / "layer_minter_seed.json", "{}", 0o600)
        _write(secrets_dir / "rpc_ethereum_mainnet.txt", "https://primary.invalid\n", 0o600)
        _write(
            secrets_dir / "rpc_ethereum_mainnet_secondary.txt",
            "https://secondary.invalid\n",
            0o600,
        )

    def _run(self, action):
        env_file = self.script_dir / ".env.production"
        env = dict(os.environ)
        env["PATH"] = str(self.script_dir / "bin") + os.pathsep + env["PATH"]
        env["DOCKER_CALL_LOG"] = str(self.docker_call_log)
        env.pop("CDPATH", None)
        return subprocess.run(
            ["sh", str(self.script_path), action],
            cwd=str(self.script_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _write_env_file(self, lines, crlf_lines=()):
        # `lines` get plain LF endings (this is how TELLOR_RUNTIME_UID/GID
        # are written below, deliberately, so the pre-existing decimal-ID
        # check -- which is unrelated to this bug and already fails closed
        # on a stray \r -- doesn't mask the thing under test). `crlf_lines`
        # get a trailing \r before the \n, simulating the realistic
        # real-world trigger from the bug report: a value edited on
        # Windows, or carrying a trailing space/CR from a copy-paste, while
        # the rest of the file is untouched. That is exactly the condition
        # config.py's `.strip()` silently tolerates but the old anchored
        # `grep '...=live$'` gate did not.
        env_file = self.script_dir / ".env.production"
        content = "".join(line + "\n" for line in lines)
        content += "".join(line + "\r\n" for line in crlf_lines)
        env_file.write_bytes(content.encode("utf-8"))
        env_file.chmod(0o600)

    def test_up_log_only_refuses_crlf_live_environment(self):
        """--up-log-only must refuse an environment whose delivery mode is
        `live`, even when the value is followed by a stray \\r from CRLF
        line endings (config.py's Settings.from_env reads the same value
        with `.strip()`, so `live\\r` is live delivery as far as the
        service is concerned)."""
        uid = os.getuid()
        gid = os.getgid()
        self._write_env_file(
            [
                "TELLOR_RUNTIME_UID={}".format(uid),
                "TELLOR_RUNTIME_GID={}".format(gid),
                "LAYER_REPLAY_START_HEIGHT=1",
            ],
            crlf_lines=["TELLOR_ALERT_DELIVERY_MODE=live"],
        )

        result = self._run("--up-log-only")

        self.assertEqual(
            result.returncode,
            1,
            msg="expected --up-log-only to refuse a CRLF live environment; "
            "stdout={!r} stderr={!r}".format(result.stdout, result.stderr),
        )
        self.assertIn(
            "--up-log-only refuses an environment configured for live delivery",
            result.stderr,
        )
        # The refusal must happen before anything that looks like starting
        # services -- the fake docker must never have seen "up".
        calls = self.docker_call_log.read_text()
        self.assertNotIn("up -d", calls)

    def test_up_log_only_allows_crlf_log_only_environment(self):
        """Sanity check: a genuinely log-only CRLF environment is still
        allowed through to `compose up`, so the fix isn't just refusing
        everything."""
        uid = os.getuid()
        gid = os.getgid()
        self._write_env_file(
            [
                "TELLOR_RUNTIME_UID={}".format(uid),
                "TELLOR_RUNTIME_GID={}".format(gid),
                "LAYER_REPLAY_START_HEIGHT=1",
            ],
            crlf_lines=["TELLOR_ALERT_DELIVERY_MODE=log-only"],
        )

        result = self._run("--up-log-only")

        self.assertEqual(
            result.returncode,
            0,
            msg="expected a genuine log-only environment to proceed; "
            "stdout={!r} stderr={!r}".format(result.stdout, result.stderr),
        )
        calls = self.docker_call_log.read_text()
        self.assertIn("up -d --remove-orphans", calls)

    def test_duplicate_delivery_mode_entries_are_rejected(self):
        """TELLOR_ALERT_DELIVERY_MODE now gets the same duplicate-key
        detection TELLOR_RUNTIME_UID/GID already had."""
        uid = os.getuid()
        gid = os.getgid()
        self._write_env_file(
            [
                "TELLOR_RUNTIME_UID={}".format(uid),
                "TELLOR_RUNTIME_GID={}".format(gid),
                "LAYER_REPLAY_START_HEIGHT=1",
                "TELLOR_ALERT_DELIVERY_MODE=log-only",
                "TELLOR_ALERT_DELIVERY_MODE=live",
            ],
        )

        result = self._run("--up-log-only")

        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate TELLOR_ALERT_DELIVERY_MODE entries", result.stderr)


if __name__ == "__main__":
    unittest.main()
