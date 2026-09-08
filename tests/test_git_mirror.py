import json
import subprocess
import tarfile
from pathlib import Path
import pytest

from taisce_cuan.git_mirror import GitMirrorPublisher
from taisce_cuan.models import (
    BuildDefinition,
    Builder,
    ExternalParameters,
    IngestionMetadata,
    Predicate,
    RunDetails,
    RunDetailsMetadata,
    Subject,
)


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

    # Verify both canonical tag and baseline tag were created pointing to same commit
    canonical_commit = subprocess.check_output(["git", "rev-parse", "sample/0.1.0^{commit}"], cwd=repo_dir, text=True).strip()
    baseline_commit = subprocess.check_output(["git", "rev-parse", "baseline/0.1.0^{commit}"], cwd=repo_dir, text=True).strip()
    assert canonical_commit == baseline_commit

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


def test_sign_attestation_fail_closed(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "sign-test", "1.0.0")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    metadata = IngestionMetadata(
        subject=[Subject(name="sign-test-1.0.0.tar.gz", digest={"sha256": "abcdef"})],
        predicate=Predicate(
            buildDefinition=BuildDefinition(
                externalParameters=ExternalParameters(
                    package="sign-test",
                    canonical_name="sign-test",
                    version="1.0.0",
                ),
            ),
            runDetails=RunDetails(
                builder=Builder(),
                metadata=RunDetailsMetadata(startedOn="2026-09-02T00:00:00Z", finishedOn="2026-09-02T00:00:00Z"),
            ),
        ),
    )
    repo_dir = workspace / "pypi.org-sign-test"
    repo_dir.mkdir(parents=True, exist_ok=True)
    out_prov = repo_dir / "prov.json"

    # When sign_key is None or empty -> returns None safely
    assert publisher.sign_attestation(metadata, source_file, None, out_prov) is None
    assert publisher.sign_attestation(metadata, source_file, "", out_prov) is None

    # When sign_key is a non-existent file path -> fails closed with ValueError
    with pytest.raises(ValueError, match="does not exist"):
        publisher.sign_attestation(metadata, source_file, "/non/existent/key.pem", out_prov)


def test_baseline_tag_preservation_and_overwrite(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "pkg-base", "1.0.0")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    # Initial ingestion
    publisher.publish_source(
        source_path=source_file,
        package="pkg-base",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    repo_dir = workspace / "pypi.org-pkg-base"
    init_commit = subprocess.check_output(["git", "rev-parse", "pkg-base/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    init_base = subprocess.check_output(["git", "rev-parse", "baseline/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    assert init_commit == init_base

    # Simulate a backport advancing baseline/1.0.0 to a new commit
    (repo_dir / "source" / "patch.txt").write_text("backport patch")
    subprocess.run(["git", "add", "source/patch.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "backport commit"], cwd=repo_dir, check=True)
    backport_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    subprocess.run(["git", "tag", "-f", "baseline/1.0.0", backport_commit], cwd=repo_dir, check=True)

    # Re-running ingestion without allow_overwrite must preserve the advanced baseline tag
    publisher.publish_source(
        source_path=source_file,
        package="pkg-base",
        version="1.0.0",
        workspace_dir=workspace,
        allow_overwrite=False,
        dry_run=True,
    )
    current_base = subprocess.check_output(["git", "rev-parse", "baseline/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    assert current_base == backport_commit

    # Re-running ingestion with allow_overwrite=True resets baseline tag to canonical commit
    publisher.publish_source(
        source_path=source_file,
        package="pkg-base",
        version="1.0.0",
        workspace_dir=workspace,
        allow_overwrite=True,
        dry_run=True,
    )
    reset_base = subprocess.check_output(["git", "rev-parse", "baseline/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    new_canonical = subprocess.check_output(["git", "rev-parse", "pkg-base/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    assert reset_base == new_canonical
    assert reset_base != backport_commit


def test_git_mirror_publisher_real_bare_remote(tmp_path: Path):
    # 1. Create a local bare git repository as a simulated remote
    remote_bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_bare)], check=True)

    remote_url = str(remote_bare.resolve())
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="remote-bot",
        committer_email="remote-bot@example.com",
        remote_url=remote_url,
    )

    # 2. Test GitMirrorPublisher.publish_source with dry_run=False pointing to bare remote
    source_100 = create_sample_source(tmp_path, "pkg-remote", "1.0.0")
    tag_100 = publisher.publish_source(
        source_path=source_100,
        package="pkg-remote",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=False,
    )
    assert tag_100 == "pkg-remote/1.0.0"

    # 3. Verify atomic push in the bare remote
    # - refs/tags/<canonical>/<version> exists in the bare remote and points to right commit
    remote_canonical_100 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/pkg-remote/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    # - refs/tags/baseline/<version> exists in the bare remote and points to initial commit
    remote_baseline_100 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    # - refs/heads/main exists and is updated
    remote_main_100 = subprocess.check_output(
        ["git", "rev-parse", "refs/heads/main^{commit}"], cwd=remote_bare, text=True
    ).strip()

    assert remote_canonical_100 == remote_baseline_100
    assert remote_canonical_100 == remote_main_100

    # 4. Verify remote tag synchronization and overwrite protection over real git remote:
    # 4a. Re-running with same content is a no-op / succeeds
    tag_noop = publisher.publish_source(
        source_path=source_100,
        package="pkg-remote",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=False,
    )
    assert tag_noop == "pkg-remote/1.0.0"

    # Idempotent check from a fresh workspace where tags are fetched from remote
    workspace_fresh = tmp_path / "workspace_fresh"
    tag_noop_fresh = publisher.publish_source(
        source_path=source_100,
        package="pkg-remote",
        version="1.0.0",
        workspace_dir=workspace_fresh,
        dry_run=False,
    )
    assert tag_noop_fresh == "pkg-remote/1.0.0"

    # 4b. Advancing remote baseline/<version> (simulating a backport CT)
    # Create a backport commit in bare remote and update baseline/1.0.0 tag to it
    tree_id = subprocess.check_output(
        ["git", "rev-parse", "refs/heads/main^{tree}"], cwd=remote_bare, text=True
    ).strip()
    backport_commit = subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=test-bot",
            "-c",
            "user.email=test-bot@example.com",
            "commit-tree",
            tree_id,
            "-p",
            remote_canonical_100,
            "-m",
            "backport CT commit",
        ],
        cwd=remote_bare,
        text=True,
    ).strip()
    subprocess.run(["git", "update-ref", "refs/tags/baseline/1.0.0", backport_commit], cwd=remote_bare, check=True)

    verified_advanced_base = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    assert verified_advanced_base == backport_commit

    # Running a second version ingestion maintains the advanced baseline tag when allow_overwrite=False
    source_200 = create_sample_source(tmp_path, "pkg-remote", "2.0.0")
    tag_200 = publisher.publish_source(
        source_path=source_200,
        package="pkg-remote",
        version="2.0.0",
        workspace_dir=workspace,
        allow_overwrite=False,
        dry_run=False,
    )
    assert tag_200 == "pkg-remote/2.0.0"

    # Verify baseline/1.0.0 is still the advanced backport commit in the bare remote
    remote_base_after_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    assert remote_base_after_200 == backport_commit

    # Verify 2.0.0 tags and main in bare remote
    remote_canonical_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/pkg-remote/2.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    remote_baseline_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/2.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    remote_main_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/heads/main^{commit}"], cwd=remote_bare, text=True
    ).strip()
    assert remote_canonical_200 == remote_baseline_200 == remote_main_200
    assert remote_canonical_200 != remote_canonical_100

    # 4c. Running with allow_overwrite=True force-updates both tags in the bare remote
    source_100_modified = create_sample_source(
        tmp_path, "pkg-remote", "1.0.0", filename="pkg-remote-1.0.0-mod.tar.gz", extra_content="force-overwrite"
    )
    # First verify allow_overwrite=False raises ValueError when content differs
    with pytest.raises(ValueError, match="allow_overwrite is False"):
        publisher.publish_source(
            source_path=source_100_modified,
            package="pkg-remote",
            version="1.0.0",
            workspace_dir=workspace,
            allow_overwrite=False,
            dry_run=False,
        )

    # Now run with allow_overwrite=True
    tag_100_overwritten = publisher.publish_source(
        source_path=source_100_modified,
        package="pkg-remote",
        version="1.0.0",
        workspace_dir=workspace,
        allow_overwrite=True,
        dry_run=False,
    )
    assert tag_100_overwritten == "pkg-remote/1.0.0"

    # Verify both tags in the bare remote are updated to the newly generated commit
    remote_canonical_100_after = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/pkg-remote/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    remote_baseline_100_after = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()

    assert remote_canonical_100_after == remote_baseline_100_after
    assert remote_canonical_100_after != remote_canonical_100
    assert remote_baseline_100_after != backport_commit


def test_two_tier_provenance_resolution_tier1_embedded(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "tier1-test", "1.0.0")
    prov_file = tmp_path / "sdist-provenance.json"
    prov_file.write_text('{"statement": "tier1 embedded sdist provenance"}')

    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    tag = publisher.publish_source(
        source_path=source_file,
        package="tier1-test",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    assert tag == "tier1-test/1.0.0"

    repo_dir = workspace / "pypi.org-tier1-test"
    saved_prov = repo_dir / ".lightwell" / "provenance.json"
    assert saved_prov.exists()
    assert "tier1 embedded sdist provenance" in saved_prov.read_text()


def test_two_tier_provenance_resolution_tier2_chains(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "tier2-test", "1.0.0")
    chains_dir = tmp_path / "chains-provenance"
    chains_dir.mkdir(parents=True, exist_ok=True)
    chains_file = chains_dir / "sha256-abc123.json"
    chains_file.write_text('{"statement": "tier2 chains pipelinerun provenance"}')

    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    tag = publisher.publish_source(
        source_path=source_file,
        package="tier2-test",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    assert tag == "tier2-test/1.0.0"

    repo_dir = workspace / "pypi.org-tier2-test"
    saved_prov = repo_dir / ".lightwell" / "provenance.json"
    assert saved_prov.exists()
    assert "tier2 chains pipelinerun provenance" in saved_prov.read_text()



