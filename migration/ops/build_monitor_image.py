#!/usr/bin/env python3
"""Build and inspect the private Monitor OCI image from a clean source commit."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKER_CONTEXT = REPOSITORY_ROOT / "migration" / "oci"
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
NUMERIC_USER = "65532:65532"


class ImageBuildError(RuntimeError):
    pass


def _run(arguments: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            tuple(arguments),
            cwd=str(cwd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ImageBuildError("required build command could not run") from error


def clean_source_sha(
    root: Path,
    runner: Callable[..., subprocess.CompletedProcess] = _run,
) -> str:
    status = runner(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=root
    )
    if status.returncode != 0:
        raise ImageBuildError("Git status failed")
    if status.stdout.strip():
        raise ImageBuildError("source worktree is dirty")
    head = runner(("git", "rev-parse", "HEAD"), cwd=root)
    source_sha = head.stdout.strip()
    if head.returncode != 0 or not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ImageBuildError("source Git SHA is invalid")
    return source_sha


def build_command(
    source_sha: str,
    image_reference: str,
    context: Path = DOCKER_CONTEXT,
) -> Sequence[str]:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ImageBuildError("source Git SHA is invalid")
    if not isinstance(image_reference, str) or not image_reference.strip() or any(
        character.isspace() for character in image_reference
    ):
        raise ImageBuildError("local image reference is invalid")
    return (
        "docker",
        "build",
        "--pull=false",
        "--network=none",
        "--platform",
        "linux/amd64",
        "--build-arg",
        "SOURCE_GIT_SHA={}".format(source_sha),
        "--tag",
        image_reference,
        "--file",
        str(context / "Dockerfile"),
        str(context),
    )


def materialize_committed_context(
    root: Path,
    source_sha: str,
    destination: Path,
    runner: Callable[..., subprocess.CompletedProcess] = _run,
) -> None:
    """Materialize the fixed OCI context from Git, never from mutable files."""
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ImageBuildError("source Git SHA is invalid")
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("Dockerfile", ".dockerignore"):
        blob = runner(
            ("git", "show", "{}:migration/oci/{}".format(source_sha, name)),
            cwd=root,
        )
        if blob.returncode != 0:
            raise ImageBuildError("committed OCI build context is incomplete")
        try:
            (destination / name).write_text(blob.stdout, encoding="utf-8")
        except OSError as error:
            raise ImageBuildError(
                "committed OCI build context could not be written"
            ) from error


def validate_inspection(value: Any, source_sha: str) -> Dict[str, Any]:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ImageBuildError("source Git SHA is invalid")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ImageBuildError("Docker image inspection returned an invalid shape")
    image = value[0]
    config = image.get("Config")
    if not isinstance(config, dict):
        raise ImageBuildError("Docker image inspection has no config")
    labels = config.get("Labels")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise ImageBuildError("Monitor image must be linux/amd64")
    if config.get("User") != NUMERIC_USER:
        raise ImageBuildError("Monitor image must declare numeric user {}".format(NUMERIC_USER))
    if not isinstance(labels, dict) or labels.get(
        "org.opencontainers.image.revision"
    ) != source_sha:
        raise ImageBuildError("Monitor image revision label does not match source HEAD")
    return image


def build_image(
    root: Path,
    image_reference: Optional[str] = None,
    runner: Callable[..., subprocess.CompletedProcess] = _run,
) -> str:
    root = root.resolve()
    source_sha = clean_source_sha(root, runner=runner)
    reference = image_reference or "tellor-ops/monitor:source-{}".format(source_sha)
    with tempfile.TemporaryDirectory(prefix="tellor-monitor-oci-") as temporary:
        context = Path(temporary) / "context"
        materialize_committed_context(root, source_sha, context, runner=runner)
        built = runner(build_command(source_sha, reference, context), cwd=root)
        if built.returncode != 0:
            raise ImageBuildError("Docker build failed")
    inspected = runner(("docker", "image", "inspect", reference), cwd=root)
    if inspected.returncode != 0:
        raise ImageBuildError("Docker image inspection failed")
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise ImageBuildError("Docker image inspection was not JSON") from error
    validate_inspection(payload, source_sha)
    return reference


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-reference")
    args = parser.parse_args(argv)
    try:
        print(build_image(REPOSITORY_ROOT, args.image_reference))
        return 0
    except ImageBuildError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
