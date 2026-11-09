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

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from taisce_cuan.sdist import compute_sha256

_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
# Matches an optional scheme (captured, so it survives the strip) followed by
# a colon-separated userinfo pair ("user:token@") before the first path
# separator. Deliberately narrower than "any netloc with an '@'": a bare
# `user@host` (no ':') is left alone, since that's an SSH routing username
# (e.g. the conventional `git@github.com`), not a credential.
_CREDENTIALED_URL_PREFIX_RE = re.compile(
    r"^((?:[a-zA-Z][a-zA-Z0-9+.-]*://)?)[^/@]+:[^/@]+@"
)


def _no_prompt_env() -> dict[str, str]:
    """Environment for git subprocess calls that must never hang on a
    credential prompt against a private or nonexistent HTTPS URL."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _redact_url_credentials(url: str) -> str:
    """Strip an embedded `user:token@` credential pair from a URL before it
    is persisted to metadata, a commit message, or error/log text.

    Matched via regex rather than `urllib.parse.urlsplit`, which only
    recognizes userinfo inside a proper `scheme://netloc` -- a URL missing
    its scheme (e.g. "https://" dropped during copy-paste) still has a
    netloc-shaped credential prefix that urlsplit would silently leave
    unparsed and unredacted. A no-op for plain filesystem paths and
    scp-style SSH refs (`git@host:path`), which don't match the
    colon-separated-pair shape.
    """
    return _CREDENTIALED_URL_PREFIX_RE.sub(r"\1", url)


def _resolve_ref_to_sha(ref: str, clone_dir: Path, env: dict[str, str]) -> str:
    """Resolve `ref` to a commit SHA in a freshly-cloned repo.

    A plain `git clone` fetches every branch as a remote-tracking ref
    (`refs/remotes/origin/*`) but only checks out the default branch
    locally (`refs/heads/<default>`), so `<ref>^{commit}` alone resolves
    tags and SHAs (fetched in full either way) but not a non-default
    branch. Falls back to `origin/<ref>` before giving up, so `--ref`
    behaves for branches as advertised.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            cwd=clone_dir,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError:
        result = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"origin/{ref}^{{commit}}",
            ],
            cwd=clone_dir,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    return result.stdout.strip()


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
    upstream_url: str,
    ref: str,
    workspace_dir: Path,
    *,
    package: str,
    version: str,
) -> ClonedGitSource:
    """
    Clone a git repository and resolve a ref to its commit SHA.

    Performs a full (non-shallow) clone of the upstream repository and
    resolves the given ref to its exact commit SHA. Does not check out a
    working tree at that ref — callers that need the tree (e.g.
    archive_git_source_to_dir) operate on the resolved SHA directly via
    `git archive`.

    Args:
        upstream_url: URL of the git repository to clone.
        ref: Git ref (tag, branch, commit) to resolve.
        workspace_dir: Directory where the clone will be created.
        package: Package name (for documentation and directory naming).
        version: Package version (for documentation and directory naming).

    Returns:
        ClonedGitSource containing the resolved commit SHA and clone directory
        path.

    Raises:
        RuntimeError: If the clone or ref resolution fails, or resolves to
        something that isn't a valid commit SHA, with upstream_url and ref
        in the message.
    """
    clone_dir = workspace_dir / f"git-{package}-{version}"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    env = _no_prompt_env()
    redacted_url = _redact_url_credentials(upstream_url)

    try:
        # Full (non-shallow) clone. The `--` end-of-options marker prevents
        # upstream_url from being interpreted as an option if it happens to
        # start with a dash.
        subprocess.run(
            ["git", "clone", "--", upstream_url, str(clone_dir)],
            check=True,
            capture_output=True,
            env=env,
        )
    except subprocess.CalledProcessError:
        # `from None`, not `from e`: CalledProcessError.cmd holds the raw
        # argv passed above, including the unredacted upstream_url (needed
        # there for real auth) -- chaining it would leak the credential to
        # any caller that logs this exception with a traceback.
        raise RuntimeError(
            f"Failed to clone git repository: {redacted_url} (ref={ref})"
        ) from None

    try:
        # Resolve the ref directly to its commit SHA without checking it out
        # (archive_git_source_to_dir operates on the resolved SHA, not a
        # working tree). `--verify --end-of-options` rejects an option-like
        # ref (e.g. "--orphan=x") instead of silently treating it as a flag.
        resolved_sha = _resolve_ref_to_sha(ref, clone_dir, env)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to resolve ref '{ref}' to commit SHA from {redacted_url}"
        ) from e

    if not _SHA_RE.match(resolved_sha):
        raise RuntimeError(
            f"Resolved ref '{ref}' from {redacted_url} to an invalid commit "
            f"SHA: {resolved_sha!r}"
        )

    return ClonedGitSource(
        package=package,
        version=version,
        upstream_url=redacted_url,
        ref=ref,
        resolved_sha=resolved_sha,
        clone_dir=clone_dir,
    )


def _neutralize_export_attributes(clone_dir: Path) -> None:
    """Make `git archive` ignore the tree's own `.gitattributes`.

    Writes `$GIT_DIR/info/attributes`, which takes precedence over any
    in-tree `.gitattributes` for archive purposes. This disables both
    `export-ignore` (which would silently drop files from the archive) and
    `export-subst` (which rewrites placeholder content, e.g. setuptools-scm's
    `.git_archival.txt`, using clone-time ref state — content that would
    otherwise drift across re-clones of the identical commit and break
    idempotency). The archived tree ends up being the literal, unmodified
    tracked tree.
    """
    info_dir = clone_dir / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "attributes").write_text("* -export-subst -export-ignore\n")


def archive_git_source_to_dir(cloned: ClonedGitSource, dest_source_dir: Path) -> str:
    """Write cloned.resolved_sha's tracked tree into dest_source_dir and return
    the sha256 hex digest of the `git archive` tar bytes (a content hash,
    reused by publish_git_source's idempotency check — see Task 5).

    Neutralizes `export-ignore`/`export-subst` from any in-tree
    `.gitattributes` before archiving (see `_neutralize_export_attributes`),
    so the archived tree matches the tracked tree exactly and is stable
    across re-clones of the same commit.

    Args:
        cloned: ClonedGitSource with a resolved commit SHA and clone directory.
        dest_source_dir: Directory where the extracted source tree will be written.

    Returns:
        SHA256 hex digest (64 characters) of the tar archive bytes.

    Raises:
        RuntimeError: If git archive or extraction fails.
    """
    dest_source_dir.mkdir(parents=True, exist_ok=True)
    _neutralize_export_attributes(cloned.clone_dir)

    # Create a temporary tar file for hashing. Placed under the clone's
    # parent directory (itself under the caller-controlled workspace_dir)
    # rather than system /tmp, so it lands on provisioned storage rather
    # than a small tmpfs in a real Tekton pod.
    with tempfile.NamedTemporaryFile(
        suffix=".tar", delete=False, dir=cloned.clone_dir.parent
    ) as tmp_tar:
        tar_path = Path(tmp_tar.name)

    try:
        # Create git archive tar of the resolved commit
        try:
            with open(tar_path, "wb") as tar_out:
                subprocess.run(
                    ["git", "archive", "--format=tar", cloned.resolved_sha],
                    cwd=cloned.clone_dir,
                    stdout=tar_out,
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
