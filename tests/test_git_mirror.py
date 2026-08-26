import json
import subprocess
import tarfile
from pathlib import Path
import pytest

from taisce_cuan.git_mirror import GitMirrorPublisher


def create_sample_source(path: Path, pkg_name: str, version: str, filename: str = "", extra_content: str = "") -> Path:
    fn = filename or f"{pkg_name}-{version}.tar.gz"
    source_file = path / fn
    pkg_dir = path / f"src_{fn}"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "pyproject.toml").write_text(f"[project]\nname='{pkg_name}'\nversion='{version}'\n# {extra_content}")
    with tarfile.open(source_file, "w:gz") as tar:
        tar.add(pkg_dir, arcname=f"{pkg_name}-{version}")
    return source_file


def test_git_mirror_publisher_dry_run(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "sample", "0.1.0")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="custom bot",
        committer_email="custom@example.com",
    )

    tag = publisher.publish_source(
        source_path=source_file,
        package="sample",
        version="0.1.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    assert tag == "sample/0.1.0"
    repo_dir = workspace / "pypi.org-sample"
    assert (repo_dir / "source" / "pyproject.toml").exists()
    assert (repo_dir / ".lightwell" / "metadata.json").exists()

    meta = json.loads((repo_dir / ".lightwell" / "metadata.json").read_text())
    assert meta["predicate"]["buildDefinition"]["buildType"] == "https://lightwell.dev/buildTypes/python-source-ingest/v1"
    assert meta["predicate"]["buildDefinition"]["externalParameters"]["canonical_name"] == "sample"
    assert meta["subject"][1]["digest"]["gitTree"] != "pending"

    # Verify custom git committer config
    user_name = subprocess.check_output(["git", "config", "user.name"], cwd=repo_dir, text=True).strip()
    user_email = subprocess.check_output(["git", "config", "user.email"], cwd=repo_dir, text=True).strip()
    assert user_name == "custom bot"
    assert user_email == "custom@example.com"


def test_idempotent_push_same_content(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "pkg-test", "1.0.0")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    tag1 = publisher.publish_source(
        source_path=source_file,
        package="pkg-test",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    assert tag1 == "pkg-test/1.0.0"

    # Second push with identical content -> returns tag cleanly without error
    tag2 = publisher.publish_source(
        source_path=source_file,
        package="pkg-test",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    assert tag2 == "pkg-test/1.0.0"


def test_overwrite_protection_different_content(tmp_path: Path):
    source1 = create_sample_source(tmp_path, "pkg-test", "1.0.0", filename="pkg-test-1.0.0-v1.tar.gz", extra_content="v1")
    source2 = create_sample_source(tmp_path, "pkg-test", "1.0.0", filename="pkg-test-1.0.0-v2.tar.gz", extra_content="v2_modified")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    publisher.publish_source(
        source_path=source1,
        package="pkg-test",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    # Re-pushing with different content and allow_overwrite=False must fail
    with pytest.raises(ValueError, match="allow_overwrite is False"):
        publisher.publish_source(
            source_path=source2,
            package="pkg-test",
            version="1.0.0",
            workspace_dir=workspace,
            allow_overwrite=False,
            dry_run=True,
        )

    # Re-pushing with allow_overwrite=True succeeds
    tag = publisher.publish_source(
        source_path=source2,
        package="pkg-test",
        version="1.0.0",
        workspace_dir=workspace,
        allow_overwrite=True,
        dry_run=True,
    )
    assert tag == "pkg-test/1.0.0"


def test_semver_branch_topology_backfill(tmp_path: Path):
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    # 1. Ingest 1.0.0
    source_100 = create_sample_source(tmp_path, "multi-ver", "1.0.0")
    publisher.publish_source(
        source_path=source_100,
        package="multi-ver",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    # 2. Ingest 2.0.0 (newer -> advances main)
    source_200 = create_sample_source(tmp_path, "multi-ver", "2.0.0")
    publisher.publish_source(
        source_path=source_200,
        package="multi-ver",
        version="2.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    repo_dir = workspace / "pypi.org-multi-ver"

    # Verify main is at 2.0.0 commit
    head_show = subprocess.check_output(["git", "show", "HEAD:source/pyproject.toml"], cwd=repo_dir, text=True)
    assert "version='2.0.0'" in head_show

    # 3. Backfill 1.1.0 (between 1.0.0 and 2.0.0) -> branches stream/1.1 from 1.0.0
    source_110 = create_sample_source(tmp_path, "multi-ver", "1.1.0")
    publisher.publish_source(
        source_path=source_110,
        package="multi-ver",
        version="1.1.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    # Check that stream/1.1 exists
    branches = subprocess.check_output(["git", "branch", "--list"], cwd=repo_dir, text=True)
    assert "stream/1.1" in branches

    # Check that 1.1.0 parent commit is 1.0.0 commit, not 2.0.0
    tag_110_parents = subprocess.check_output(["git", "rev-parse", "multi-ver/1.1.0^"], cwd=repo_dir, text=True).strip()
    tag_100_commit = subprocess.check_output(["git", "rev-parse", "multi-ver/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    assert tag_110_parents == tag_100_commit

    # 4. Backfill 0.9.0 (older than all existing tags) -> creates orphan branch stream/0.9
    source_090 = create_sample_source(tmp_path, "multi-ver", "0.9.0")
    publisher.publish_source(
        source_path=source_090,
        package="multi-ver",
        version="0.9.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    # 0.9.0 should have 0 parent commits (orphan root commit)
    parent_count = len(subprocess.check_output(["git", "rev-list", "--parents", "-n", "1", "multi-ver/0.9.0"], cwd=repo_dir, text=True).strip().split()) - 1
    assert parent_count == 0


def test_sign_attestation_protection_when_key_empty_or_missing(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "sign-test", "1.0.0")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    repo_dir = workspace / "pypi.org-sign-test"
    repo_dir.mkdir(parents=True, exist_ok=True)
    meta_file = repo_dir / "meta.json"
    meta_file.write_text("{}")
    out_prov = repo_dir / "prov.json"

    # When sign_key is None or empty -> returns None safely
    assert publisher.sign_attestation(meta_file, source_file, None, out_prov) is None
    assert publisher.sign_attestation(meta_file, source_file, "", out_prov) is None

    # When sign_key is a non-existent file path -> returns None safely
    assert publisher.sign_attestation(meta_file, source_file, "/non/existent/key.pem", out_prov) is None
