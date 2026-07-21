#!/usr/bin/env python3
"""Build and verify a secret-free, reproducible autoTasks release artifact."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


SCHEMA_VERSION = 1
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "DEPLOYMENT_MANIFEST.json"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})\Z")
INSTANCE_RE = re.compile(r"i-[0-9a-f]{8,17}\Z")
REGION_RE = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d\Z")
ACCOUNT_RE = re.compile(r"\d{12}\Z")
RELEASE_ID_RE = re.compile(r"autoTasks@([0-9a-f]{40})\Z")


class ReleaseError(RuntimeError):
    pass


def _git_text(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git",) + arguments,
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError("Git command could not run") from error
    if completed.returncode != 0:
        raise ReleaseError("Git command failed")
    return completed.stdout.strip()


def _git_archive(root: Path, commit: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "archive", "--format=tar", "--prefix=autoTasks/", commit),
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError("Git archive could not run") from error
    if completed.returncode != 0:
        raise ReleaseError("Git archive failed")
    return completed.stdout


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    descriptor = None
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.tmp.".format(path.name), dir=str(path.parent)
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _write_once_or_match(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ReleaseError("release output target must not be a symlink")
    if path.exists():
        if not path.is_file():
            raise ReleaseError("release output target is not a regular file")
        if path.read_bytes() != content:
            raise ReleaseError("release output already exists with different content")
        return
    _atomic_write(path, content)


def _tracked_secret_paths(root: Path) -> Sequence[str]:
    paths = _git_text(root, "ls-files", "-z").split("\0")
    unsafe = []
    for path in paths:
        if not path:
            continue
        name = Path(path).name
        if (
            (name == ".env" or (name.startswith(".env.") and name != ".env.example"))
            or "/secrets/" in "/{}".format(path)
            or name.startswith("discord_webhooks.json")
        ):
            unsafe.append(path)
    return tuple(sorted(unsafe))


def _monitor_image(root: Path) -> str:
    compose = (root / "migration" / "docker-compose.yaml").read_text(encoding="utf-8")
    references = []
    for line in compose.splitlines():
        stripped = line.strip()
        if stripped.startswith("image:") and "openzeppelin-monitor" in stripped:
            references.append(stripped.split(":", 1)[1].strip())
    if len(references) != 1:
        raise ReleaseError("expected exactly one OpenZeppelin Monitor image reference")
    reference = references[0]
    if "@" not in reference or not DIGEST_RE.fullmatch(reference.rsplit("@", 1)[1]):
        raise ReleaseError("OpenZeppelin Monitor image must be content-pinned")
    return reference


def _canonical_utc(value: str) -> str:
    if not isinstance(value, str):
        raise ReleaseError("timestamp must be a string")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ReleaseError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ReleaseError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_manifest(value: Any, require_deployed: bool = False) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseError("manifest must be a JSON object")
    required = {
        "schema_version",
        "release_id",
        "source",
        "build",
        "artifact",
        "components",
        "images",
        "deployment",
    }
    if set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("manifest fields or schema version are invalid")
    release_id = value.get("release_id")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError("manifest release ID is invalid")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {
        "repository", "commit", "tree", "dirty_patch_sha256"
    }:
        raise ReleaseError("manifest source is invalid")
    if (
        source["repository"] != "autoTasks"
        or not isinstance(source["commit"], str)
        or not COMMIT_RE.fullmatch(source["commit"])
    ):
        raise ReleaseError("manifest source commit is invalid")
    if not isinstance(source["tree"], str) or not COMMIT_RE.fullmatch(source["tree"]):
        raise ReleaseError("manifest source tree is invalid")
    if source["dirty_patch_sha256"] is not None:
        raise ReleaseError("dirty source patches are prohibited")
    if release_id != "autoTasks@{}".format(source["commit"]):
        raise ReleaseError("manifest release ID does not match source commit")

    build = value.get("build")
    if not isinstance(build, dict) or set(build) != {
        "generator", "source_timestamp_utc"
    }:
        raise ReleaseError("manifest build metadata is invalid")
    if build["generator"] != "migration/ops/release_manifest.py":
        raise ReleaseError("manifest generator is invalid")
    _canonical_utc(build["source_timestamp_utc"])

    artifact = value.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "filename", "format", "sha256", "bytes"
    }:
        raise ReleaseError("manifest artifact is invalid")
    if not isinstance(artifact["filename"], str):
        raise ReleaseError("artifact filename is invalid")
    if Path(artifact["filename"]).name != artifact["filename"]:
        raise ReleaseError("artifact filename must not contain a path")
    if artifact["filename"] != "autoTasks-{}.tar".format(source["commit"][:12]):
        raise ReleaseError("artifact filename does not match source commit")
    if artifact["format"] != "git-archive-tar":
        raise ReleaseError("artifact format is invalid")
    if not isinstance(artifact["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", artifact["sha256"]
    ):
        raise ReleaseError("artifact digest is invalid")
    if isinstance(artifact["bytes"], bool) or not isinstance(artifact["bytes"], int) or artifact["bytes"] <= 0:
        raise ReleaseError("artifact size is invalid")

    components = value.get("components")
    if components != [{
        "name": "autoTasks",
        "commit": source["commit"],
        "destination": "/opt/tellor/autoTasks",
    }]:
        raise ReleaseError("manifest component mapping is invalid")
    images = value.get("images")
    if not isinstance(images, list) or len(images) != 1:
        raise ReleaseError("manifest image inventory is invalid")
    image = images[0]
    if not isinstance(image, dict) or set(image) != {"name", "reference", "digest"}:
        raise ReleaseError("manifest image record is invalid")
    if (
        image["name"] != "openzeppelin-monitor"
        or not isinstance(image["reference"], str)
        or not image["reference"].startswith(
            "openzeppelin/openzeppelin-monitor:"
        )
        or "@" not in image["reference"]
        or not isinstance(image["digest"], str)
    ):
        raise ReleaseError("manifest Monitor image is invalid")
    if image["reference"].rsplit("@", 1)[1] != image["digest"] or not DIGEST_RE.fullmatch(image["digest"]):
        raise ReleaseError("manifest Monitor digest is invalid")

    deployment = value.get("deployment")
    deployment_keys = {
        "status",
        "authorization_id",
        "account_id",
        "region",
        "instance_id",
        "deployed_at_utc",
        "rollback_release",
    }
    if not isinstance(deployment, dict) or set(deployment) != deployment_keys:
        raise ReleaseError("manifest deployment record is invalid")
    if (
        not isinstance(deployment["rollback_release"], str)
        or not RELEASE_ID_RE.fullmatch(deployment["rollback_release"])
    ):
        raise ReleaseError("rollback release is required")
    if deployment["status"] == "not-deployed":
        if any(deployment[key] is not None for key in (
            "authorization_id", "account_id", "region", "instance_id", "deployed_at_utc"
        )):
            raise ReleaseError("undeployed manifest contains deployment claims")
        if require_deployed:
            raise ReleaseError("manifest has no deployment record")
    elif deployment["status"] == "deployed":
        if not all(isinstance(deployment[key], str) and deployment[key].strip() for key in (
            "authorization_id", "account_id", "region", "instance_id", "deployed_at_utc"
        )):
            raise ReleaseError("deployed manifest is incomplete")
        if not ACCOUNT_RE.fullmatch(deployment["account_id"]):
            raise ReleaseError("deployment account is invalid")
        if not REGION_RE.fullmatch(deployment["region"]):
            raise ReleaseError("deployment region is invalid")
        if not INSTANCE_RE.fullmatch(deployment["instance_id"]):
            raise ReleaseError("deployment instance is invalid")
        _canonical_utc(deployment["deployed_at_utc"])
    else:
        raise ReleaseError("deployment status is invalid")
    return value


def _load_manifest(path: Path) -> Dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ReleaseError("manifest must be a regular non-symlink file")
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("manifest could not be read") from error


def verify_release(manifest_path: Path, require_deployed: bool = False) -> Dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    validate_manifest(manifest, require_deployed=require_deployed)
    artifact_path = manifest_path.parent / manifest["artifact"]["filename"]
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise ReleaseError("release artifact is missing or unsafe")
    content = artifact_path.read_bytes()
    if len(content) != manifest["artifact"]["bytes"]:
        raise ReleaseError("release artifact size does not match manifest")
    if _sha256(content) != manifest["artifact"]["sha256"]:
        raise ReleaseError("release artifact digest does not match manifest")
    return manifest


def build_release(root: Path, output_dir: Path, rollback_release: str) -> Path:
    root = root.resolve()
    output_dir = output_dir.resolve()
    if _is_within(output_dir, root):
        raise ReleaseError("release output directory must be outside the source repository")
    if not RELEASE_ID_RE.fullmatch(rollback_release):
        raise ReleaseError("rollback release must be autoTasks@ plus a full commit")
    if _git_text(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseError("source worktree is dirty; commit or remove every change first")
    unsafe = _tracked_secret_paths(root)
    if unsafe:
        raise ReleaseError("tracked secret-like paths block release creation")

    commit = _git_text(root, "rev-parse", "HEAD")
    tree = _git_text(root, "rev-parse", "HEAD^{tree}")
    if not COMMIT_RE.fullmatch(commit) or not COMMIT_RE.fullmatch(tree):
        raise ReleaseError("source commit or tree is invalid")
    source_timestamp = _canonical_utc(
        _git_text(root, "show", "-s", "--format=%cI", commit)
    )
    archive = _git_archive(root, commit)
    artifact_name = "autoTasks-{}.tar".format(commit[:12])
    image_reference = _monitor_image(root)
    image_digest = image_reference.rsplit("@", 1)[1]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_id": "autoTasks@{}".format(commit),
        "source": {
            "repository": "autoTasks",
            "commit": commit,
            "tree": tree,
            "dirty_patch_sha256": None,
        },
        "build": {
            "generator": "migration/ops/release_manifest.py",
            "source_timestamp_utc": source_timestamp,
        },
        "artifact": {
            "filename": artifact_name,
            "format": "git-archive-tar",
            "sha256": _sha256(archive),
            "bytes": len(archive),
        },
        "components": [{
            "name": "autoTasks",
            "commit": commit,
            "destination": "/opt/tellor/autoTasks",
        }],
        "images": [{
            "name": "openzeppelin-monitor",
            "reference": image_reference,
            "digest": image_digest,
        }],
        "deployment": {
            "status": "not-deployed",
            "authorization_id": None,
            "account_id": None,
            "region": None,
            "instance_id": None,
            "deployed_at_utc": None,
            "rollback_release": rollback_release,
        },
    }
    validate_manifest(manifest)
    output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write_once_or_match(output_dir / artifact_name, archive)
    manifest_content = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    manifest_path = output_dir / MANIFEST_NAME
    _write_once_or_match(manifest_path, manifest_content)
    verify_release(manifest_path)
    return manifest_path


def record_deployment(
    manifest_path: Path,
    *,
    authorization_id: str,
    account_id: str,
    region: str,
    instance_id: str,
    deployed_at_utc: str,
) -> None:
    manifest = verify_release(manifest_path)
    if manifest["deployment"]["status"] != "not-deployed":
        raise ReleaseError("deployment is already recorded")
    manifest["deployment"].update({
        "status": "deployed",
        "authorization_id": authorization_id,
        "account_id": account_id,
        "region": region,
        "instance_id": instance_id,
        "deployed_at_utc": _canonical_utc(deployed_at_utc),
    })
    validate_manifest(manifest, require_deployed=True)
    content = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_write(manifest_path, content)
    verify_release(manifest_path, require_deployed=True)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--rollback-release", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--require-deployed", action="store_true")

    record = subparsers.add_parser("record-deployment")
    record.add_argument("--manifest", type=Path, required=True)
    record.add_argument("--authorization-id", required=True)
    record.add_argument("--account-id", required=True)
    record.add_argument("--region", required=True)
    record.add_argument("--instance-id", required=True)
    record.add_argument("--deployed-at-utc", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "build":
            path = build_release(REPOSITORY_ROOT, args.output_dir, args.rollback_release)
            print(path)
        elif args.command == "verify":
            manifest = verify_release(args.manifest, args.require_deployed)
            print("verified {}".format(manifest["release_id"]))
        else:
            record_deployment(
                args.manifest,
                authorization_id=args.authorization_id,
                account_id=args.account_id,
                region=args.region,
                instance_id=args.instance_id,
                deployed_at_utc=args.deployed_at_utc,
            )
            print("deployment record verified")
        return 0
    except ReleaseError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
