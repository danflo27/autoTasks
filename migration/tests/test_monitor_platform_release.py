import copy
import gzip
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path


MIGRATION_DIR = Path(__file__).resolve().parents[1]
OPS_DIR = MIGRATION_DIR / "ops"
sys.path.insert(0, str(OPS_DIR))

import build_monitor_image  # noqa: E402
import monitor_platform_release as platform_release  # noqa: E402


IMAGE_DIGEST = "sha256:" + "b" * 64


def git(root, *arguments):
    return subprocess.run(
        ("git",) + arguments,
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def make_source(root):
    oci = root / "migration" / "oci"
    oci.mkdir(parents=True)
    (oci / "Dockerfile").write_text(
        "FROM scratch\nUSER 65532:65532\n", encoding="utf-8"
    )
    (oci / ".dockerignore").write_text("**\n", encoding="utf-8")
    scripts = root / "migration" / "config" / "triggers" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "handler.py").write_text("print('fixture')\n", encoding="utf-8")
    monitors = root / "migration" / "config" / "monitors"
    monitors.mkdir(parents=True)
    (monitors / "fixture.json").write_text('{"paused":true}\n', encoding="utf-8")
    (root / "README.md").write_text("must not enter Monitor release\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Monitor Release Test",
        "-c",
        "user.email=monitor-release@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return git(root, "rev-parse", "HEAD")


class MonitorImageInterfaceTests(unittest.TestCase):
    def test_dockerfile_is_thin_pinned_and_ends_with_numeric_non_root_user(self):
        dockerfile = (MIGRATION_DIR / "oci" / "Dockerfile").read_text(encoding="utf-8")
        instructions = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            instructions[0],
            "FROM openzeppelin/openzeppelin-monitor:v1.5.0@sha256:"
            "8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635",
        )
        self.assertIn("ARG SOURCE_GIT_SHA", instructions)
        self.assertIn(
            'LABEL org.opencontainers.image.revision="${SOURCE_GIT_SHA}"',
            instructions,
        )
        self.assertIn("ENV TELLOR_DELIVERY_MODE=live", instructions)
        self.assertEqual(instructions[-1], "USER 65532:65532")
        self.assertFalse(
            any(line.startswith(("ADD ", "COPY ", "RUN ")) for line in instructions)
        )
        self.assertEqual(
            (MIGRATION_DIR / "oci" / ".dockerignore").read_text(encoding="utf-8"),
            "**\n",
        )

    def test_build_command_is_offline_narrow_and_linux_amd64(self):
        source_sha = "a" * 40
        command = build_monitor_image.build_command(
            source_sha, "tellor-ops/monitor:source-{}".format(source_sha)
        )
        self.assertEqual(command[:2], ("docker", "build"))
        self.assertIn("--pull=false", command)
        self.assertIn("--network=none", command)
        self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
        self.assertEqual(
            command[command.index("--build-arg") + 1],
            "SOURCE_GIT_SHA={}".format(source_sha),
        )
        self.assertEqual(Path(command[-1]), MIGRATION_DIR / "oci")

    def test_image_inspection_requires_platform_user_revision_and_architecture(self):
        source_sha = "a" * 40
        valid = [{
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {
                "User": "65532:65532",
                "Labels": {"org.opencontainers.image.revision": source_sha},
            },
        }]
        self.assertEqual(
            build_monitor_image.validate_inspection(valid, source_sha)["Architecture"],
            "amd64",
        )
        cases = []
        wrong_user = copy.deepcopy(valid)
        wrong_user[0]["Config"]["User"] = "root"
        cases.append(wrong_user)
        wrong_revision = copy.deepcopy(valid)
        wrong_revision[0]["Config"]["Labels"][
            "org.opencontainers.image.revision"
        ] = "c" * 40
        cases.append(wrong_revision)
        wrong_architecture = copy.deepcopy(valid)
        wrong_architecture[0]["Architecture"] = "arm64"
        cases.append(wrong_architecture)
        missing_config = copy.deepcopy(valid)
        missing_config[0]["Config"] = None
        cases.append(missing_config)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(
                build_monitor_image.ImageBuildError
            ):
                build_monitor_image.validate_inspection(value, source_sha)

    def test_build_context_comes_from_commit_not_mutable_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            source_sha = make_source(root)
            dockerfile = root / "migration" / "oci" / "Dockerfile"
            committed = dockerfile.read_text(encoding="utf-8")
            dockerfile.write_text("FROM mutable-race\nUSER root\n", encoding="utf-8")
            context = Path(directory) / "committed-context"
            build_monitor_image.materialize_committed_context(
                root, source_sha, context
            )
            self.assertEqual(
                (context / "Dockerfile").read_text(encoding="utf-8"), committed
            )
            self.assertEqual(
                (context / ".dockerignore").read_text(encoding="utf-8"), "**\n"
            )


class MonitorPlatformReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source_sha = make_source(self.source)

    def build(self, name="release"):
        return platform_release.build_release(
            self.source, self.root / name, IMAGE_DIGEST
        )

    def test_release_is_deterministic_platform_exact_and_config_only(self):
        first_path = self.build("first")
        time.sleep(1.1)
        second_path = self.build("second")
        first = json.loads(first_path.read_text(encoding="utf-8"))
        second = json.loads(second_path.read_text(encoding="utf-8"))
        self.assertEqual(set(first), platform_release.PLATFORM_MANIFEST_FIELDS)
        self.assertEqual(first, second)
        self.assertEqual(first["schemaVersion"], 1)
        self.assertEqual(first["releaseId"], "monitor-{}".format(self.source_sha))
        self.assertEqual(first["imageSourceGitSha"], self.source_sha)
        self.assertEqual(first["imageDigest"], IMAGE_DIGEST)
        self.assertEqual(
            first["artifactKey"],
            "monitor/releases/monitor-{}.tar.gz".format(self.source_sha),
        )
        first_artifact = first_path.parent / "{}.tar.gz".format(first["releaseId"])
        second_artifact = second_path.parent / "{}.tar.gz".format(second["releaseId"])
        self.assertEqual(first_artifact.read_bytes(), second_artifact.read_bytes())
        with tarfile.open(first_artifact, mode="r:gz") as archive:
            members = archive.getmembers()
        source_timestamp = int(git(self.source, "show", "-s", "--format=%ct", "HEAD"))
        self.assertTrue(members)
        self.assertTrue(
            all(
                member.name == "config" or member.name.startswith("config/")
                for member in members
            )
        )
        self.assertTrue(all(member.isdir() or member.isreg() for member in members))
        self.assertTrue(all(member.mtime == source_timestamp for member in members))
        self.assertNotIn("README.md", {member.name for member in members})
        self.assertEqual(platform_release.verify_release(first_path), first)

    def test_dirty_source_and_invalid_digest_fail_before_output(self):
        (self.source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(platform_release.PlatformReleaseError, "dirty"):
            self.build("dirty")
        with self.assertRaisesRegex(platform_release.PlatformReleaseError, "digest"):
            platform_release.build_release(
                self.source, self.root / "invalid", "sha256:short"
            )

    def test_link_and_secret_like_config_paths_are_rejected(self):
        link = self.source / "migration" / "config" / "linked.json"
        link.symlink_to("monitors/fixture.json")
        git(self.source, "add", ".")
        git(
            self.source,
            "-c",
            "user.name=Monitor Release Test",
            "-c",
            "user.email=monitor-release@example.invalid",
            "commit",
            "-q",
            "-m",
            "link",
        )
        with self.assertRaisesRegex(platform_release.PlatformReleaseError, "link"):
            self.build("link")

        link.unlink()
        secret = self.source / "migration" / "config" / "secrets" / "route"
        secret.parent.mkdir()
        secret.write_text("not-a-real-secret\n", encoding="utf-8")
        git(self.source, "add", "-A")
        git(
            self.source,
            "-c",
            "user.name=Monitor Release Test",
            "-c",
            "user.email=monitor-release@example.invalid",
            "commit",
            "-q",
            "-m",
            "secret path",
        )
        with self.assertRaisesRegex(platform_release.PlatformReleaseError, "secret-like"):
            self.build("secret")

    def test_manifest_and_artifact_tampering_are_rejected(self):
        manifest_path = self.build()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        invalid = dict(manifest)
        invalid["imageSourceGitSha"] = "c" * 40
        with self.assertRaisesRegex(platform_release.PlatformReleaseError, "does not match"):
            platform_release.validate_manifest(invalid)
        artifact = manifest_path.parent / "{}.tar.gz".format(manifest["releaseId"])
        artifact.write_bytes(artifact.read_bytes() + b"tampered")
        with self.assertRaisesRegex(platform_release.PlatformReleaseError, "digest"):
            platform_release.verify_release(manifest_path)

    def test_archive_validator_rejects_escape_and_link_members(self):
        for member in (
            tarfile.TarInfo("../escape"),
            tarfile.TarInfo("config/link"),
        ):
            if member.name == "config/link":
                member.type = tarfile.SYMTYPE
                member.linkname = "target"
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as archive:
                archive.addfile(member)
            compressed = gzip.compress(raw.getvalue(), mtime=0)
            with self.subTest(member=member.name), self.assertRaises(
                platform_release.PlatformReleaseError
            ):
                platform_release._validate_archive(compressed)

    def test_outputs_are_write_once_or_byte_identical(self):
        manifest_path = self.build()
        platform_release.build_release(self.source, manifest_path.parent, IMAGE_DIGEST)
        manifest_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            platform_release.PlatformReleaseError, "different content"
        ):
            self.build()


if __name__ == "__main__":
    unittest.main()
