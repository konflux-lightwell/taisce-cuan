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

from taisce_cuan.source.git_source import clone_and_resolve_git_source


def test_resolves_annotated_tag_to_commit_sha(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "1.5.1^{commit}"], cwd=upstream, text=True
    ).strip()

    cloned = clone_and_resolve_git_source(
        str(upstream), "1.5.1", tmp_path / "work", package="griffe", version="1.5.1",
    )

    assert cloned.resolved_sha == expected_sha
    assert (cloned.clone_dir / "pyproject.toml").exists()


def test_raises_on_nonexistent_ref(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    try:
        clone_and_resolve_git_source(
            str(upstream), "does-not-exist", tmp_path / "work",
            package="griffe", version="1.5.1",
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "does-not-exist" in str(exc)
        assert str(upstream) in str(exc)
