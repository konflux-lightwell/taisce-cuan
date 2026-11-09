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

from taisce_cuan.source.git_source import (
    archive_git_source_to_dir,
    clone_and_resolve_git_source,
)


def test_resolves_annotated_tag_to_commit_sha(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "1.5.1^{commit}"], cwd=upstream, text=True
    ).strip()

    cloned = clone_and_resolve_git_source(
        str(upstream),
        "1.5.1",
        tmp_path / "work",
        package="griffe",
        version="1.5.1",
    )

    assert cloned.resolved_sha == expected_sha
    assert (cloned.clone_dir / "pyproject.toml").exists()


def test_raises_on_nonexistent_ref(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    try:
        clone_and_resolve_git_source(
            str(upstream),
            "does-not-exist",
            tmp_path / "work",
            package="griffe",
            version="1.5.1",
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "does-not-exist" in str(exc)
        assert str(upstream) in str(exc)


def test_archive_git_source_to_dir_extracts_tree_and_returns_hash(
    tmp_path, make_local_git_repo
):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    cloned = clone_and_resolve_git_source(
        str(upstream),
        "1.5.1",
        tmp_path / "work",
        package="griffe",
        version="1.5.1",
    )
    dest = tmp_path / "source"

    digest = archive_git_source_to_dir(cloned, dest)

    assert (dest / "pyproject.toml").exists()
    assert not (dest / ".git").exists()
    assert len(digest) == 64  # hex sha256


def test_archive_git_source_to_dir_is_deterministic_for_same_commit(
    tmp_path, make_local_git_repo
):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    cloned = clone_and_resolve_git_source(
        str(upstream),
        "1.5.1",
        tmp_path / "work",
        package="griffe",
        version="1.5.1",
    )
    digest_a = archive_git_source_to_dir(cloned, tmp_path / "source-a")
    digest_b = archive_git_source_to_dir(cloned, tmp_path / "source-b")
    assert digest_a == digest_b


def test_option_like_ref_is_rejected_not_misinterpreted_as_a_flag(
    tmp_path, make_local_git_repo
):
    """A ref value that looks like a git option (e.g. '--orphan=x') must not
    be silently accepted as one by `git checkout`/`git rev-parse` — it must
    be treated as a (nonexistent) ref name and rejected. LWLP-3402
    final-review finding 4."""
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    try:
        clone_and_resolve_git_source(
            str(upstream),
            "--orphan=x",
            tmp_path / "work",
            package="griffe",
            version="1.5.1",
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "--orphan=x" in str(exc)


def test_credentials_stripped_from_cloned_upstream_url(tmp_path, make_local_git_repo):
    """upstream_url with embedded userinfo (e.g. a leaked PAT) must not
    propagate into ClonedGitSource.upstream_url -- downstream code writes
    that value verbatim into .lightwell/metadata.json and the mirror commit
    message. LWLP-4828 review item 1."""
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    credentialed_url = f"file://user:tok3n@{upstream.resolve()}"

    cloned = clone_and_resolve_git_source(
        credentialed_url,
        "1.5.1",
        tmp_path / "work",
        package="griffe",
        version="1.5.1",
    )

    assert "tok3n" not in cloned.upstream_url
    assert "user:tok3n@" not in cloned.upstream_url
    assert str(upstream.resolve()) in cloned.upstream_url


def test_credentials_stripped_from_error_text_on_failed_resolution(tmp_path):
    """Credential redaction must also apply on the failure path -- a failed
    clone/resolve must not leak a token into the exception text (which can
    end up in logs). LWLP-4828 review item 1."""
    try:
        clone_and_resolve_git_source(
            "https://user:supersecrettoken@127.0.0.1:1/nonexistent.git",
            "main",
            tmp_path / "work",
            package="pkg",
            version="1.0",
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "supersecrettoken" not in str(exc)
        assert "127.0.0.1" in str(exc)


def test_resolves_non_default_branch_via_origin_fallback(tmp_path, make_local_git_repo):
    """A plain `git clone` only creates a local ref for the default branch --
    other branches exist only as `origin/<branch>`. clone_and_resolve_git_source
    must still resolve a non-default branch ref, matching the CLI's documented
    tag/branch/SHA support for --ref. LWLP-4828 review item 2."""
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    subprocess.run(
        ["git", "checkout", "-b", "release-1.x"],
        cwd=upstream,
        check=True,
        capture_output=True,
    )
    (upstream / "pyproject.toml").write_text(
        "[project]\nname='griffe'\nversion='1.5.1'\n# on branch\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "branch commit"], cwd=upstream, check=True
    )
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "release-1.x^{commit}"], cwd=upstream, text=True
    ).strip()
    subprocess.run(
        ["git", "checkout", "main"], cwd=upstream, check=True, capture_output=True
    )

    cloned = clone_and_resolve_git_source(
        str(upstream),
        "release-1.x",
        tmp_path / "work",
        package="griffe",
        version="1.5.1",
    )

    assert cloned.resolved_sha == expected_sha
