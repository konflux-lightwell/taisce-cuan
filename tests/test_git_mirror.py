import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

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
    """Create a normalized sdist with its required provenance carrier evidence."""
    fn = filename or f"{pkg_name}-{version}.tar.gz"
    carrier_root = path / f"carrier_{pkg_name}_{version}_{extra_content or 'default'}"
    carrier_root.mkdir(parents=True, exist_ok=True)
    source_file = carrier_root / fn
    pkg_dir = carrier_root / f"src_{fn}"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "pyproject.toml").write_text(f"[project]\nname='{pkg_name}'\nversion='{version}'\n# {extra_content}")
    with tarfile.open(source_file, "w:gz") as tar:
        tar.add(pkg_dir, arcname=f"{pkg_name}-{version}")

    import hashlib
    source_sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()
    downloads = carrier_root / "downloads"
    downloads.mkdir()
    original = downloads / fn
    original.write_bytes(source_file.read_bytes())
    original_sha256 = hashlib.sha256(original.read_bytes()).hexdigest()
    (carrier_root / "source-origin.json").write_text(json.dumps({
        "schema": "https://lightwell.dev/schemas/source-origin/v1",
        "acquired": {"registry": "pypi.org", "sha256": original_sha256, "path": f"downloads/{fn}",
                     "package": pkg_name, "version": version},
        "provenance": {"mode": "pypi", "rhtl": {"status": "not-advertised"}},
    }, sort_keys=True) + "\n")
    (carrier_root / "sdist-transformation.json").write_text(json.dumps({
        "schema": "https://lightwell.dev/schemas/sdist-transformation/v1",
        "input": {"sha256": original_sha256},
        "output": {"sha256": source_sha256, "path": fn},
        "source_origin_sha256": hashlib.sha256((carrier_root / "source-origin.json").read_bytes()).hexdigest(),
        "transformation": "normalized-sdist",
    }, sort_keys=True) + "\n")
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
    assert len(meta["subject"]) == 1
    assert "gitTree" not in meta["subject"][0]["digest"]

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


def test_rhtl_pep740_adaptation_preserves_base64_and_rejects_malformed(tmp_path: Path):
    raw = tmp_path / "provenance.pep740.json"
    output = tmp_path / "provenance.dsse.json"
    payload = "eyJwcmVjaXNlbHkiOiJub3QtZGVjb2RlZCJ9=="
    signature = "c2lnbmF0dXJlLXN0cmluZw=="
    raw.write_bytes(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": {
        "statement": payload, "signature": signature}}]}]}).encode())
    GitMirrorPublisher.adapt_rhtl_pep740(raw, output)
    adapted = json.loads(output.read_text())
    assert adapted == {"payloadType": "application/vnd.in-toto+json", "payload": payload,
                       "signatures": [{"sig": signature}]}
    assert json.loads(raw.read_bytes())["attestation_bundles"][0]["attestations"][0]["envelope"]["statement"] == payload
    raw.write_text("{malformed")
    with pytest.raises(ValueError, match="malformed"):
        GitMirrorPublisher.adapt_rhtl_pep740(raw, output)


def test_verify_blob_attestation_targets_acquired_and_never_resigns(monkeypatch, tmp_path: Path):
    source = tmp_path / "downloads" / "original.tar.gz"
    signature = tmp_path / "provenance.dsse.json"
    key = tmp_path / "release3.pub"
    source.parent.mkdir(); source.write_bytes(b"original"); signature.write_text("{}\n"); key.write_text("public")
    captured = []
    monkeypatch.setattr("taisce_cuan.git_mirror.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.git_mirror.subprocess.run", lambda command, **kwargs: captured.append(command) or SimpleNamespace(returncode=0, stderr=""))
    GitMirrorPublisher.verify_blob_attestation(source, signature, str(key))
    assert captured[0][1] == "verify-blob-attestation"
    assert captured[0][-1] == str(source)
    assert "attest-blob" not in captured[0]


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


def test_sign_attestation_uses_configured_cosign_policy(monkeypatch, tmp_path: Path):
    source_file = create_sample_source(tmp_path, "cosign-policy", "1.0.0")
    output_file = tmp_path / "metadata.dsse.json"
    key_file = tmp_path / "cosign.key"
    key_file.write_text("test key")
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="test",
        committer_email="test@example.com",
    )
    metadata = IngestionMetadata(
        subject=[Subject(name="cosign-policy-1.0.0.tar.gz", digest={"sha256": "0" * 64})],
        predicate=Predicate(
            buildDefinition=BuildDefinition(
                externalParameters=ExternalParameters(
                    package="cosign-policy",
                    canonical_name="cosign-policy",
                    version="1.0.0",
                ),
            ),
            runDetails=RunDetails(
                builder=Builder(),
                metadata=RunDetailsMetadata(
                    startedOn="2026-09-11T00:00:00Z",
                    finishedOn="2026-09-11T00:00:00Z",
                ),
            ),
        ),
    )
    captured: list[str] = []

    monkeypatch.setattr("taisce_cuan.git_mirror.shutil.which", lambda name: "/usr/local/bin/cosign")

    def fake_run(command, **_kwargs):
        captured.extend(command)
        output_file.write_text("attestation")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("taisce_cuan.git_mirror.subprocess.run", fake_run)

    assert publisher.sign_attestation(metadata, source_file, str(key_file), output_file) == output_file
    assert "--tlog-upload=false" not in captured
    assert "--type=https://slsa.dev/provenance/v1" in captured
    assert f"--key={key_file}" in captured
    assert f"--output-file={output_file}" in captured


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


def test_provenance_carrier_is_published_with_normalized_sdist(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "carrier-test", "1.0.0")
    carrier_root = source_file.parent
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    tag = publisher.publish_source(
        source_path=source_file,
        package="carrier-test",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    assert tag == "carrier-test/1.0.0"

    repo_dir = workspace / "pypi.org-carrier-test"
    for evidence_name in ("source-origin.json", "sdist-transformation.json"):
        published = repo_dir / ".lightwell" / evidence_name
        assert published.exists()
        assert published.read_text() == (carrier_root / evidence_name).read_text()

    origin = json.loads((repo_dir / ".lightwell" / "source-origin.json").read_text())
    transformation = json.loads((repo_dir / ".lightwell" / "sdist-transformation.json").read_text())
    assert origin["acquired"]["registry"] == "pypi.org"
    assert transformation["input"]["sha256"] == origin["acquired"]["sha256"]
    assert transformation["output"]["sha256"] == origin["acquired"]["sha256"]
    assert not (repo_dir / ".lightwell" / "provenance.json").exists()


def test_rhtl_metadata_binds_validated_closure_and_registry(tmp_path: Path, monkeypatch):
    source_file = create_sample_source(tmp_path, "rhtl-pkg", "1.0.0")
    carrier = source_file.parent
    origin_path = carrier / "source-origin.json"
    origin = json.loads(origin_path.read_text())
    origin["acquired"]["registry"] = "rhtl"
    origin["provenance"] = {"mode": "rhtl", "advertised": True,
                             "sha256": "placeholder", "rhtl": {"status": "advertised"}}
    raw = carrier / "provenance.pep740.json"
    raw.write_text(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": {
        "statement": "cGF5bG9hZA==", "signature": "c2ln"}}]}]}))
    import hashlib
    origin["provenance"]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    origin_path.write_text(json.dumps(origin, sort_keys=True) + "\n")
    transformation_path = carrier / "sdist-transformation.json"
    transformation = json.loads(transformation_path.read_text())
    transformation["source_origin_sha256"] = hashlib.sha256(origin_path.read_bytes()).hexdigest()
    transformation_path.write_text(json.dumps(transformation, sort_keys=True) + "\n")
    monkeypatch.setattr(GitMirrorPublisher, "verify_blob_attestation", staticmethod(lambda *args: None))

    workspace = tmp_path / "workspace"
    GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup").publish_source(
        source_path=source_file, package="rhtl-pkg", version="1.0.0",
        workspace_dir=workspace, source_registry="pypi.org", dry_run=True)
    # Repository naming remains compatible with the existing mirror layout.
    repo = workspace / "pypi.org-rhtl-pkg"
    meta = json.loads((repo / ".lightwell" / "metadata.json").read_text())
    build = meta["predicate"]["buildDefinition"]
    assert build["externalParameters"]["upstream_registry"] == "rhtl"
    deps = {item["annotations"]["role"]: item for item in build["resolvedDependencies"]}
    assert {"lightwell-source-origin", "lightwell-sdist-transformation",
            "upstream-acquired-sdist", "lightwell-normalized-sdist",
            "upstream-rhtl-pep740", "adapted-rhtl-dsse"} <= deps.keys()
    assert all(item["digest"].get("sha256") for item in deps.values())
    assert not any("pypi" in item["name"].lower() for item in build["resolvedDependencies"])
    assert meta["predicate"]["runDetails"]["metadata"]["attestation_level"] == "unsigned-inventory"


def test_legacy_provenance_is_not_converted_or_published(tmp_path: Path):
    source_file = create_sample_source(tmp_path, "legacy-provenance", "1.0.0")
    legacy = source_file.parent / "sdist-provenance.json"
    legacy.write_text('{"statement":"legacy attestation"}\n')
    workspace = tmp_path / "workspace"
    GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup").publish_source(
        source_path=source_file,
        package="legacy-provenance",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    lightwell = workspace / "pypi.org-legacy-provenance" / ".lightwell"
    assert not (lightwell / "provenance.json").exists()
    assert not (lightwell / "provenance.dsse").exists()
    assert not (lightwell / "metadata.dsse.json").exists()


def test_cli_push_auto_discover_metadata(tmp_path: Path):
    from taisce_cuan.cli import main
    source_file = create_sample_source(tmp_path, "auto-disc-pkg", "3.2.1")
    workspace = tmp_path / "cli_workspace"

    exit_code = main([
        "push",
        f"--source={source_file}",
        f"--workspace-dir={workspace}",
        "--forge-url=https://forge.example.com",
        "--group=testgroup",
        "--committer-name=bot",
        "--committer-email=bot@example.com",
        "--dry-run",
    ])
    assert exit_code == 0
    repo_dir = workspace / "pypi.org-auto-disc-pkg"
    assert repo_dir.exists()
    metadata_file = repo_dir / ".lightwell" / "metadata.json"
    assert metadata_file.exists()
    assert '"package": "auto-disc-pkg"' in metadata_file.read_text()
    assert '"version": "3.2.1"' in metadata_file.read_text()




