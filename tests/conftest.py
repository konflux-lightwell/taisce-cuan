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

import subprocess
from pathlib import Path

import pytest


def _make_local_git_repo(
    base_dir: Path, package: str, version: str, tag: str | None = None
) -> Path:
    """Create a local git repo with one commit and an annotated tag; return its
    path."""
    repo_dir = base_dir / f"upstream-{package}-{version}"
    repo_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", "--quiet"], cwd=repo_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True
    )
    (repo_dir / "pyproject.toml").write_text(
        f"[project]\nname='{package}'\nversion='{version}'\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial", "--quiet"], cwd=repo_dir, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", tag or version, "-m", "release"], cwd=repo_dir, check=True
    )
    return repo_dir


@pytest.fixture
def make_local_git_repo():
    """Fixture that provides the make_local_git_repo function."""
    return _make_local_git_repo
