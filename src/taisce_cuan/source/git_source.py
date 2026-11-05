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

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from taisce_cuan.sdist import compute_sha256


@dataclass(frozen=True)
class ClonedGitSource:
    """Result of cloning and resolving a git repository ref."""

    package: str
    version: str
    upstream_url: str
    ref: str
    resolved_sha: str
    clone_dir: Path


def clone_and_resolve_git_source(
    upstream_url: str, ref: str, workspace_dir: Path, *, package: str, version: str,
) -> ClonedGitSource:
    """
    Clone a git repository and resolve a ref to its commit SHA.

    Performs a full (non-shallow) clone of the upstream repository, checks out
    the specified ref, and resolves it to the exact commit SHA.

    Args:
        upstream_url: URL of the git repository to clone.
        ref: Git ref (tag, branch, commit) to checkout.
        workspace_dir: Directory where the clone will be created.
        package: Package name (for documentation and directory naming).
        version: Package version (for documentation and directory naming).

    Returns:
        ClonedGitSource containing the resolved commit SHA and clone directory
        path.

    Raises:
        RuntimeError: If the clone or ref checkout fails, with upstream_url
        and ref in the message.
    """
    clone_dir = workspace_dir / f"git-{package}-{version}"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Full (non-shallow) clone
        subprocess.run(
            ["git", "clone", upstream_url, str(clone_dir)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to clone git repository: {upstream_url} (ref={ref})"
        ) from e

    try:
        # Checkout the specified ref
        subprocess.run(
            ["git", "checkout", ref],
            cwd=clone_dir,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to checkout ref '{ref}' from {upstream_url}"
        ) from e

    try:
        # Resolve the ref to its commit SHA
        result = subprocess.run(
            ["git", "rev-parse", f"{ref}^{{commit}}"],
            cwd=clone_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        resolved_sha = result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to resolve ref '{ref}' to commit SHA from {upstream_url}"
        ) from e

    return ClonedGitSource(
        package=package,
        version=version,
        upstream_url=upstream_url,
        ref=ref,
        resolved_sha=resolved_sha,
        clone_dir=clone_dir,
    )


def archive_git_source_to_dir(cloned: ClonedGitSource, dest_source_dir: Path) -> str:
    """Write cloned.resolved_sha's tracked tree into dest_source_dir and return
    the sha256 hex digest of the `git archive` tar bytes (a content hash,
    reused by publish_git_source's idempotency check — see Task 5).

    Args:
        cloned: ClonedGitSource with a resolved commit SHA and clone directory.
        dest_source_dir: Directory where the extracted source tree will be written.

    Returns:
        SHA256 hex digest (64 characters) of the tar archive bytes.

    Raises:
        RuntimeError: If git archive or extraction fails.
    """
    dest_source_dir.mkdir(parents=True, exist_ok=True)

    # Create a temporary tar file for hashing
    with tempfile.NamedTemporaryFile(suffix='.tar', delete=False) as tmp_tar:
        tar_path = Path(tmp_tar.name)

    try:
        # Create git archive tar of the resolved commit
        try:
            subprocess.run(
                ["git", "archive", "--format=tar", cloned.resolved_sha],
                cwd=cloned.clone_dir,
                stdout=open(tar_path, 'wb'),
                check=True,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to create git archive for {cloned.resolved_sha} "
                f"from {cloned.clone_dir}: {e.stderr.decode()}"
            ) from e

        # Extract the tar into dest_source_dir
        try:
            subprocess.run(
                ["tar", "-xf", str(tar_path), "-C", str(dest_source_dir)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to extract archive to {dest_source_dir}: {e.stderr.decode()}"
            ) from e

        # Compute SHA256 of the tar file for idempotency check
        digest = compute_sha256(tar_path)

        return digest
    finally:
        # Clean up the temporary tar file
        if tar_path.exists():
            tar_path.unlink()
