"""
Copyright (C) 2026 Lightwell

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

         http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the package-git-source-image step.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Push a git source checkout as an OCI artifact — Tekton step",
    )
    parser.add_argument(
        "--image", required=True, help="Destination OCI artifact reference"
    )
    parser.add_argument(
        "--source-dir",
        default="/var/workdir/source-repo",
        help="Directory containing the fetched git source tree to push",
    )
    parser.add_argument(
        "--package", required=True, help="Canonical Python package name"
    )
    parser.add_argument("--version", required=True, help="Package version string")
    parser.add_argument(
        "--image-expires-after",
        default="",
        help="Delete image tag after specified time. Empty means keep.",
    )
    parser.add_argument(
        "--git-url",
        required=True,
        help="Immutable source git repository URL for Konflux Snapshot attribution",
    )
    parser.add_argument(
        "--revision",
        required=True,
        help="Full immutable source commit revision for Konflux Snapshot attribution",
    )
    parser.add_argument(
        "--image-url-result-path",
        help="Write the pushed image reference here (Tekton result)",
    )
    parser.add_argument(
        "--image-digest-result-path",
        help="Write the resolved digest here (Tekton result)",
    )
    parser.add_argument(
        "--image-ref-result-path", help="Write image@digest here (Tekton result)"
    )
    parser.add_argument(
        "--chains-git-url-result-path",
        help="Write --git-url through unchanged here (Tekton result)",
    )
    parser.add_argument(
        "--chains-git-commit-result-path",
        help="Write --revision through unchanged here (Tekton result)",
    )
    return parser


def select_oci_auth(image: str, workspace_dir: Path) -> Path:
    """Run select-oci-auth and write its auth.json to workspace_dir.

    select-oci-auth prints the selected registry credentials (from
    ~/.docker/config.json) to stdout as JSON; its informational messages go to
    stderr and are left to stream through uncaptured.

    Returns:
        Path to the written auth.json file.
    """
    result = subprocess.run(
        ["select-oci-auth", image], check=True, stdout=subprocess.PIPE, text=True
    )
    auth_path = workspace_dir / "auth.json"
    auth_path.write_text(result.stdout)
    return auth_path


def list_entries_to_push(source_dir: Path) -> list[str]:
    """Return the top-level entry names of source_dir, or raise if there are none.

    Checked before any auth/network work so an empty checkout fails immediately
    rather than after a pointless select-oci-auth call.

    Raises:
        RuntimeError: If source_dir has no entries to push.
    """
    entries = sorted(p.name for p in source_dir.iterdir())
    if not entries:
        raise RuntimeError(f"{source_dir} is empty, no git source files to push")
    return entries


def push_oci_artifact(
    image: str,
    source_dir: Path,
    entries: list[str],
    package: str,
    version: str,
    image_expires_after: str,
    auth_file: Path,
) -> None:
    """Push the given top-level entries of source_dir to image via oras.

    Runs with source_dir as the working directory so oras receives relative
    file/directory names, matching how a human would run this by hand. Output
    streams directly to the caller's stdout/stderr (the Tekton step log) rather
    than being captured, since oras's own progress/retry messages matter for
    debugging a failed push.

    Raises:
        subprocess.CalledProcessError: If the oras push itself fails.
    """
    args = [
        "retry",
        "oras",
        "push",
        image,
        "--registry-config",
        str(auth_file),
        "--annotation",
        f"dev.lightwell.package={package}",
        "--annotation",
        f"dev.lightwell.version={version}",
        "--annotation",
        "dev.lightwell.pipeline=python-git-ingest",
        "--artifact-type",
        "application/vnd.lightwell.git-source",
    ]
    if image_expires_after:
        args += ["--annotation", f"quay.expires-after={image_expires_after}"]
    args += list(entries)

    subprocess.run(args, check=True, cwd=source_dir)


def resolve_oci_digest(image: str, auth_file: Path) -> str:
    """Return the digest oras resolves for image, using the same auth as the push."""
    result = subprocess.run(
        ["retry", "oras", "resolve", "--registry-config", str(auth_file), image],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _write_result(path: str | None, value: str) -> None:
    if path:
        Path(path).write_text(value)


def main(argv: list[str] | None = None) -> int:
    """Push a git source checkout as an OCI artifact — Tekton step.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        0 on success, 1 on failure.
    """
    args = create_parser().parse_args(argv)
    source_dir = Path(args.source_dir)
    workspace_dir = source_dir.parent

    logger.info("Pushing git-source OCI artifact: %s", args.image)

    try:
        entries = list_entries_to_push(source_dir)
        auth_file = select_oci_auth(args.image, workspace_dir)
        push_oci_artifact(
            args.image,
            source_dir,
            entries,
            args.package,
            args.version,
            args.image_expires_after,
            auth_file,
        )
        digest = resolve_oci_digest(args.image, auth_file)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        logger.error("Failed to push git-source OCI artifact %s: %s", args.image, e)
        return 1

    image_ref = f"{args.image}@{digest}"
    logger.info("Done — pushed %s", image_ref)

    _write_result(args.image_url_result_path, args.image)
    _write_result(args.image_digest_result_path, digest)
    _write_result(args.image_ref_result_path, image_ref)
    _write_result(args.chains_git_url_result_path, args.git_url)
    _write_result(args.chains_git_commit_result_path, args.revision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
