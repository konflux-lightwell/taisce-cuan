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
from dataclasses import dataclass
from pathlib import Path


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
