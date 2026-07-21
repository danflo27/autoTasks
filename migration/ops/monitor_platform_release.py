#!/usr/bin/env python3
"""Build and verify the immutable Monitor release consumed by Tellor Ops."""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "MONITOR_RELEASE_MANIFEST.json"
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
RELEASE_ID_RE = re.compile(r"monitor-[0-9a-f]{40}\Z")
PLATFORM_MANIFEST_FIELDS = {
    "schemaVersion",
    "releaseId",
    "artifactKey",
    "artifactSha256",
    "imageDigest",
    "imageSourceGitSha",
}
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_FILE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024


class PlatformReleaseError(RuntimeError):
    pass


def _git(root: Path, *arguments: str, binary: bool = False):
    try:
        completed = subprocess.run(
            ("git",) + arguments,
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PlatformReleaseError("Git command could not run") from error
    if completed.returncode != 0:
        raise PlatformReleaseError("Git command failed")
    return completed.stdout


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _write_once_or_match(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise PlatformReleaseError("release output target must not be a symlink")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise PlatformReleaseError(
                "release output already exists with different content"
            )
        return
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".{}.tmp.".format(path.name), dir=str(path.parent)
        )
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _clean_source_sha(root: Path) -> str:
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise PlatformReleaseError("source worktree is dirty")
    source_sha = _git(root, "rev-parse", "HEAD").strip()
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise PlatformReleaseError("source Git SHA is invalid")
    return source_sha


def _reject_unsafe_config_tree(root: Path, source_sha: str) -> None:
    listing = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_sha,
        "migration/config",
    )
    if not listing:
        raise PlatformReleaseError("committed Monitor config tree is empty")
    for record in listing.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, object_type, _object_id = metadata.split(" ", 2)
        if object_type != "blob" or mode not in ("100644", "100755"):
            raise PlatformReleaseError("Monitor config contains a link or special file")
        relative = PurePosixPath(path).relative_to("migration/config")
        name = relative.name
        if (
            name == ".env"
            or (name.startswith(".env.") and name != ".env.example")
            or "secrets" in relative.parts
            or (name.startswith("discord_webhooks.json"))
        ):
            raise PlatformReleaseError("Monitor config contains a secret-like path")


def _config_archive(root: Path, source_sha: str) -> bytes:
    _reject_unsafe_config_tree(root, source_sha)
    source_timestamp = _git(root, "show", "-s", "--format=%ct", source_sha).strip()
    if not source_timestamp.isdigit():
        raise PlatformReleaseError("source commit timestamp is invalid")
    archive = _git(
        root,
        "archive",
        "--format=tar",
        "--prefix=config/",
        "--mtime=@{}".format(source_timestamp),
        "{}:migration/config".format(source_sha),
        binary=True,
    )
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=compressed, compresslevel=9, mtime=0
    ) as stream:
        stream.write(archive)
    content = compressed.getvalue()
    _validate_archive(content)
    return content


def _validate_archive(content: bytes) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError) as error:
        raise PlatformReleaseError("Monitor release is not a valid gzip tar") from error
    if not members:
        raise PlatformReleaseError("Monitor release archive is empty")
    seen = set()
    total_bytes = 0
    if len(members) > MAX_ARCHIVE_ENTRIES:
        raise PlatformReleaseError("Monitor release has too many entries")
    for member in members:
        if (
            not member.name
            or "\\" in member.name
            or any(ord(character) < 32 for character in member.name)
        ):
            raise PlatformReleaseError("Monitor release contains an unsafe path")
        path = PurePosixPath(member.name)
        parts = path.parts
        if (
            path.is_absolute()
            or not parts
            or parts[0] != "config"
            or any(part in ("", ".", "..") for part in parts)
            or parts in seen
        ):
            raise PlatformReleaseError("Monitor release contains an unsafe path")
        seen.add(parts)
        if not member.isdir() and not member.isreg():
            raise PlatformReleaseError("Monitor release contains a link or special file")
        if member.isreg():
            if member.size < 0 or member.size > MAX_ARCHIVE_FILE_BYTES:
                raise PlatformReleaseError("Monitor release file exceeds the size limit")
            total_bytes += member.size
            if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                raise PlatformReleaseError("Monitor release exceeds the total size limit")


def validate_manifest(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PLATFORM_MANIFEST_FIELDS:
        raise PlatformReleaseError("Monitor release manifest fields are invalid")
    if value.get("schemaVersion") != 1:
        raise PlatformReleaseError("Monitor release manifest schema is invalid")
    release_id = value.get("releaseId")
    source_sha = value.get("imageSourceGitSha")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise PlatformReleaseError("Monitor release ID is invalid")
    if not isinstance(source_sha, str) or not SOURCE_SHA_RE.fullmatch(source_sha):
        raise PlatformReleaseError("Monitor image source Git SHA is invalid")
    if release_id != "monitor-{}".format(source_sha):
        raise PlatformReleaseError("Monitor release ID does not match source Git SHA")
    if value.get("artifactKey") != "monitor/releases/{}.tar.gz".format(release_id):
        raise PlatformReleaseError("Monitor artifact key does not match release ID")
    if not isinstance(value.get("artifactSha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["artifactSha256"]
    ):
        raise PlatformReleaseError("Monitor artifact SHA-256 is invalid")
    if not isinstance(value.get("imageDigest"), str) or not IMAGE_DIGEST_RE.fullmatch(
        value["imageDigest"]
    ):
        raise PlatformReleaseError("Monitor image digest is invalid")
    return value


def verify_release(manifest_path: Path) -> Dict[str, Any]:
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise PlatformReleaseError("Monitor release manifest is missing or unsafe")
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlatformReleaseError("Monitor release manifest could not be read") from error
    artifact = manifest_path.parent / "{}.tar.gz".format(manifest["releaseId"])
    if artifact.is_symlink() or not artifact.is_file():
        raise PlatformReleaseError("Monitor release artifact is missing or unsafe")
    content = artifact.read_bytes()
    if _sha256(content) != manifest["artifactSha256"]:
        raise PlatformReleaseError("Monitor release artifact digest does not match manifest")
    _validate_archive(content)
    return manifest


def build_release(root: Path, output_dir: Path, image_digest: str) -> Path:
    root = root.resolve()
    output_dir = output_dir.resolve()
    if _within(output_dir, root):
        raise PlatformReleaseError("release output must be outside the source repository")
    if not IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise PlatformReleaseError("Monitor image digest is invalid")
    source_sha = _clean_source_sha(root)
    release_id = "monitor-{}".format(source_sha)
    artifact_name = "{}.tar.gz".format(release_id)
    artifact = _config_archive(root, source_sha)
    manifest = {
        "schemaVersion": 1,
        "releaseId": release_id,
        "artifactKey": "monitor/releases/{}".format(artifact_name),
        "artifactSha256": _sha256(artifact),
        "imageDigest": image_digest,
        "imageSourceGitSha": source_sha,
    }
    validate_manifest(manifest)
    output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write_once_or_match(output_dir / artifact_name, artifact)
    manifest_path = output_dir / MANIFEST_NAME
    _write_once_or_match(
        manifest_path,
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )
    verify_release(manifest_path)
    return manifest_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--image-digest", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(build_release(REPOSITORY_ROOT, args.output_dir, args.image_digest))
        else:
            manifest = verify_release(args.manifest)
            print("verified {}".format(manifest["releaseId"]))
        return 0
    except PlatformReleaseError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
