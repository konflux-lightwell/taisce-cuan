"""Exercise bounded mirror downloads against real Git repositories."""

import subprocess
from pathlib import Path

import pytest

from taisce_cuan.source import GitMirrorPublisher


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def remote_history(tmp_path, make_local_git_repo):
    remote = make_local_git_repo(tmp_path, "demo", "1.0")
    parent = git(remote, "rev-parse", "HEAD")
    (remote / "pyproject.toml").write_text("[project]\nname='demo'\nversion='2.0'\n")
    git(remote, "add", "-A")
    git(remote, "commit", "-m", "target version")
    target = git(remote, "rev-parse", "HEAD")
    git(remote, "tag", "-a", "demo/2.0", "-m", "target")
    git(remote, "tag", "baseline/2.0", parent)
    git(remote, "branch", "stream/2.0", parent)
    git(remote, "branch", "unrelated", parent)
    return remote, parent, target


def test_fetch_downloads_only_target_tag_without_parent_history(
    tmp_path, remote_history
):
    remote, parent, target = remote_history
    repo = tmp_path / "mirror"
    publisher = GitMirrorPublisher()
    repo.mkdir()
    publisher._init_git_repo(repo)

    refs = publisher._fetch_existing_remote_refs(repo, str(remote), "demo", "2.0")

    assert refs == {
        "refs/tags/demo/2.0",
        "refs/tags/baseline/2.0",
        "refs/heads/stream/2.0",
    }
    assert git(repo, "for-each-ref", "--format=%(refname)") == "refs/tags/demo/2.0"
    assert git(repo, "rev-parse", "demo/2.0^{commit}") == target
    assert git(repo, "rev-list", "demo/2.0") == target
    assert subprocess.run(["git", "cat-file", "-e", parent], cwd=repo).returncode != 0


def test_new_version_downloads_no_remote_objects(tmp_path, remote_history):
    remote, parent, target = remote_history
    repo = tmp_path / "mirror"
    repo.mkdir()
    publisher = GitMirrorPublisher()
    publisher._init_git_repo(repo)

    assert (
        publisher._fetch_existing_remote_refs(repo, str(remote), "demo", "3.0") == set()
    )

    assert git(repo, "for-each-ref") == ""
    for sha in (parent, target):
        assert subprocess.run(["git", "cat-file", "-e", sha], cwd=repo).returncode != 0


def test_remote_lookup_failure_stops_publish(tmp_path):
    repo = tmp_path / "mirror"
    repo.mkdir()
    publisher = GitMirrorPublisher()
    publisher._init_git_repo(repo)
    with pytest.raises(RuntimeError, match="Could not access remote repository"):
        publisher._fetch_existing_remote_refs(
            repo, str(tmp_path / "missing"), "demo", "2.0"
        )


def test_tag_disappearing_after_lookup_fails_closed(
    tmp_path, remote_history, monkeypatch
):
    remote, _, _ = remote_history
    repo = tmp_path / "mirror"
    repo.mkdir()
    publisher = GitMirrorPublisher()
    publisher._init_git_repo(repo)
    real_run = subprocess.run

    def remove_tag_after_lookup(command, **kwargs):
        result = real_run(command, **kwargs)
        if command[:2] == ["git", "ls-remote"]:
            git(remote, "tag", "-d", "demo/2.0")
        return result

    monkeypatch.setattr(subprocess, "run", remove_tag_after_lookup)
    with pytest.raises(RuntimeError, match="Could not fetch existing version tag"):
        publisher._fetch_existing_remote_refs(repo, str(remote), "demo", "2.0")


@pytest.mark.parametrize("source_type", ["sdist", "git"])
def test_existing_stream_without_version_tag_is_rejected(
    tmp_path, make_local_git_repo, source_type
):
    from test_git_mirror import create_sample_source

    from taisce_cuan.provenance.attest import PublishGitSourceRequest

    remote = make_local_git_repo(tmp_path, "remote", "1.0")
    git(remote, "branch", "stream/2.0")
    publisher = GitMirrorPublisher(remote_url=str(remote))
    workspace = tmp_path / "workspace"
    with pytest.raises(ValueError, match="Stream branch stream/2.0 already exists"):
        if source_type == "sdist":
            publisher.publish_source(
                source_path=create_sample_source(tmp_path, "demo", "2.0"),
                package="demo",
                version="2.0",
                workspace_dir=workspace,
            )
        else:
            publisher.publish_git_source(
                PublishGitSourceRequest(
                    upstream_url=str(remote),
                    ref="1.0",
                    package="demo",
                    version="2.0",
                    workspace_dir=workspace,
                )
            )


@pytest.mark.parametrize("source_type", ["sdist", "git"])
def test_existing_baseline_without_version_tag_is_preserved(
    tmp_path, make_local_git_repo, source_type
):
    from test_git_mirror import create_sample_source

    from taisce_cuan.provenance.attest import PublishGitSourceRequest

    remote = make_local_git_repo(tmp_path, "remote", "1.0")
    original = git(remote, "rev-parse", "HEAD")
    git(remote, "tag", "baseline/2.0")
    publisher = GitMirrorPublisher(remote_url=str(remote))
    workspace = tmp_path / "workspace"
    if source_type == "sdist":
        publisher.publish_source(
            source_path=create_sample_source(tmp_path, "demo", "2.0"),
            package="demo",
            version="2.0",
            workspace_dir=workspace,
        )
    else:
        publisher.publish_git_source(
            PublishGitSourceRequest(
                upstream_url=str(remote),
                ref="1.0",
                package="demo",
                version="2.0",
                workspace_dir=workspace,
            )
        )
    assert git(remote, "rev-parse", "baseline/2.0") == original
    assert git(remote, "rev-parse", "demo/2.0") != original
