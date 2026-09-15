import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import pytest

from taisce_cuan.source import GitMirrorPublisher
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

    # A version is ingested exactly once: re-ingesting with different content must fail
    with pytest.raises(ValueError, match="refusing to overwrite an ingested version"):
        publisher.publish_source(
            source_path=source2,
            package="pkg-test",
            version="1.0.0",
            workspace_dir=workspace,
            dry_run=True,
        )


def test_each_version_seeds_a_rootless_stream_branch(tmp_path: Path):
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )
    repo_dir = workspace / "pypi.org-multi-ver"

    def parent_count(ref: str) -> int:
        out = subprocess.check_output(["git", "rev-list", "--parents", "-n", "1", ref], cwd=repo_dir, text=True)
        return len(out.strip().split()) - 1

    # Each version maps to stream/{major}.{minor} derived purely from its version,
    # committed as a rootless (0-parent) orphan regardless of ingestion order.
    for version, stream in [("1.0.0", "stream/1.0"), ("2.0.0", "stream/2.0"),
                            ("1.1.0", "stream/1.1"), ("0.9.0", "stream/0.9")]:
        source = create_sample_source(tmp_path, "multi-ver", version)
        publisher.publish_source(
            source_path=source,
            package="multi-ver",
            version=version,
            workspace_dir=workspace,
            dry_run=True,
        )
        branches = subprocess.check_output(["git", "branch", "--list"], cwd=repo_dir, text=True)
        assert stream in branches
        # The canonical tag is a rootless commit; no fabricated cross-version lineage.
        assert parent_count(f"multi-ver/{version}") == 0

    # No "main" branch is ever created; branches are one-per-minor-line.
    branches = subprocess.check_output(["git", "branch", "--list"], cwd=repo_dir, text=True)
    assert "main" not in branches


def test_reingesting_same_minor_different_patch_is_refused(tmp_path: Path):
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    source_110 = create_sample_source(tmp_path, "same-minor", "1.1.0")
    publisher.publish_source(
        source_path=source_110, package="same-minor", version="1.1.0",
        workspace_dir=workspace, dry_run=True,
    )

    # A second patch in the same minor line maps to the existing stream/1.1;
    # taisce-cuan seeds a stream once and refuses to advance it.
    source_115 = create_sample_source(tmp_path, "same-minor", "1.1.5")
    with pytest.raises(ValueError, match="Stream branch stream/1.1 already exists"):
        publisher.publish_source(
            source_path=source_115, package="same-minor", version="1.1.5",
            workspace_dir=workspace, dry_run=True,
        )


def test_unparseable_version_is_refused(tmp_path: Path):
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )

    # A version that cannot be parsed has no stream to seed; mirroring fails
    # rather than falling back to a shared branch.
    source = create_sample_source(tmp_path, "bad-ver", "not-a-version")
    with pytest.raises(ValueError, match="Cannot parse version"):
        publisher.publish_source(
            source_path=source, package="bad-ver", version="not-a-version",
            workspace_dir=workspace, dry_run=True,
        )


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

    monkeypatch.setattr("taisce_cuan.provenance.attest.shutil.which", lambda name: "/usr/local/bin/cosign")

    def fake_run(command, **_kwargs):
        captured.extend(command)
        output_file.write_text("attestation")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("taisce_cuan.provenance.attest.subprocess.run", fake_run)

    assert publisher.sign_attestation(metadata, source_file, str(key_file), output_file) == output_file
    assert "--tlog-upload=false" not in captured
    assert any(arg.startswith("--type=") for arg in captured)
    assert f"--key={key_file}" in captured
    assert any(arg.startswith("--output-file=") or arg.startswith("--bundle=") for arg in captured)


def test_baseline_tag_preserved_on_idempotent_reingest(tmp_path: Path):
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

    # Re-ingesting the same version (idempotent no-op) must preserve the advanced baseline tag
    publisher.publish_source(
        source_path=source_file,
        package="pkg-base",
        version="1.0.0",
        workspace_dir=workspace,
        dry_run=True,
    )
    current_base = subprocess.check_output(["git", "rev-parse", "baseline/1.0.0^{commit}"], cwd=repo_dir, text=True).strip()
    assert current_base == backport_commit


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
    # - refs/heads/stream/1.0 exists and points to the seeded commit
    remote_stream_100 = subprocess.check_output(
        ["git", "rev-parse", "refs/heads/stream/1.0^{commit}"], cwd=remote_bare, text=True
    ).strip()

    assert remote_canonical_100 == remote_baseline_100
    assert remote_canonical_100 == remote_stream_100

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
        ["git", "rev-parse", "refs/heads/stream/1.0^{tree}"], cwd=remote_bare, text=True
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

    # Running a second version ingestion maintains the advanced baseline tag
    source_200 = create_sample_source(tmp_path, "pkg-remote", "2.0.0")
    tag_200 = publisher.publish_source(
        source_path=source_200,
        package="pkg-remote",
        version="2.0.0",
        workspace_dir=workspace,
        dry_run=False,
    )
    assert tag_200 == "pkg-remote/2.0.0"

    # Verify baseline/1.0.0 is still the advanced backport commit in the bare remote
    remote_base_after_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    assert remote_base_after_200 == backport_commit

    # Verify 2.0.0 tags and stream/2.0 in bare remote
    remote_canonical_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/pkg-remote/2.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    remote_baseline_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/baseline/2.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    remote_stream_200 = subprocess.check_output(
        ["git", "rev-parse", "refs/heads/stream/2.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    assert remote_canonical_200 == remote_baseline_200 == remote_stream_200
    assert remote_canonical_200 != remote_canonical_100

    # 4c. Re-ingesting an existing version with different content is refused over the real remote
    source_100_modified = create_sample_source(
        tmp_path, "pkg-remote", "1.0.0", filename="pkg-remote-1.0.0-mod.tar.gz", extra_content="force-overwrite"
    )
    with pytest.raises(ValueError, match="refusing to overwrite an ingested version"):
        publisher.publish_source(
            source_path=source_100_modified,
            package="pkg-remote",
            version="1.0.0",
            workspace_dir=workspace,
            dry_run=False,
        )

    # The original tags in the bare remote remain untouched
    remote_canonical_100_after = subprocess.check_output(
        ["git", "rev-parse", "refs/tags/pkg-remote/1.0.0^{commit}"], cwd=remote_bare, text=True
    ).strip()
    assert remote_canonical_100_after == remote_canonical_100


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


def test_publish_source_signing_and_legacy_unlinking_mocked(tmp_path: Path, monkeypatch):
    source_file = create_sample_source(tmp_path, "signed-pkg", "1.0.0")
    workspace = tmp_path / "workspace"
    repo_dir = workspace / "pypi.org-signed-pkg"
    repo_dir.mkdir(parents=True, exist_ok=True)
    lightwell_dir = repo_dir / ".lightwell"
    lightwell_dir.mkdir(parents=True, exist_ok=True)
    legacy_envelope = lightwell_dir / "metadata.dsse"
    legacy_envelope.write_text('{"stale": true}\n')
    legacy_prov_envelope = lightwell_dir / "provenance.dsse"
    legacy_prov_envelope.write_text('{"stale_prov": true}\n')

    key_file = tmp_path / "signing.key"
    key_file.write_text("private key bytes")
    pub_file = tmp_path / "signing.pub"
    pub_file.write_text("public key bytes")

    monkeypatch.setattr("taisce_cuan.provenance.attest.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")

    verify_calls = []
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] != "/bin/cosign":
            return real_run(command, **kwargs)
        if command[1] == "attest-blob":
            out_arg = [arg for arg in command if arg.startswith("--output-file=") or arg.startswith("--bundle=")][0]
            out_path = Path(out_arg.split("=", 1)[1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"payloadType":"application/vnd.in-toto+json"}\n')
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1] == "verify-blob-attestation":
            verify_calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    publisher = GitMirrorPublisher(
        forge_url="https://forge.example.com",
        group="testgroup",
        committer_name="bot",
        committer_email="bot@example.com",
    )
    publisher.publish_source(
        source_path=source_file,
        package="signed-pkg",
        version="1.0.0",
        workspace_dir=workspace,
        source_registry="pypi.org",
        sign_key=str(key_file),
        dry_run=True,
    )

    # Legacy metadata.dsse and provenance.dsse must be unlinked
    assert not legacy_envelope.exists()
    assert not legacy_prov_envelope.exists()
    assert (lightwell_dir / "metadata.dsse.json").exists()
    assert (lightwell_dir / "provenance.dsse.json").exists()
    assert not (lightwell_dir / "provenance.dsse").exists()
    assert not (lightwell_dir / "metadata.dsse").exists()

    # Verification must be called with public key, never private key
    assert len(verify_calls) == 1
    assert verify_calls[0][1] == "verify-blob-attestation"
    key_idx = verify_calls[0].index("--key")
    assert verify_calls[0][key_idx + 1] == str(pub_file)


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


def test_publish_source_rhtl_verification_fail_closed_and_opaque(tmp_path: Path, monkeypatch):
    source_file = create_sample_source(tmp_path, "rhtl-gate-pkg", "2.0.0")
    carrier = source_file.parent
    origin_path = carrier / "source-origin.json"
    origin = json.loads(origin_path.read_text())
    origin["acquired"]["registry"] = "rhtl"
    origin["provenance"] = {"mode": "rhtl", "advertised": True, "sha256": "placeholder"}
    raw = carrier / "provenance.pep740.json"
    raw.write_text(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": {
        "statement": "cGF5bG9hZA==", "signature": "c2ln"}}]}]}))
    import hashlib
    origin["provenance"]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    origin_path.write_text(json.dumps(origin, sort_keys=True) + "\n")
    trans_path = carrier / "sdist-transformation.json"
    trans = json.loads(trans_path.read_text())
    trans["source_origin_sha256"] = hashlib.sha256(origin_path.read_bytes()).hexdigest()
    trans_path.write_text(json.dumps(trans, sort_keys=True) + "\n")

    pub_key_file = tmp_path / "release3.pub"
    pub_key_file.write_text("release3 public key")

    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup")

    # 1. Missing public_key must fail closed
    with pytest.raises(ValueError, match="public verification key is required"):
        publisher.publish_source(
            source_path=source_file, package="rhtl-gate-pkg", version="2.0.0",
            workspace_dir=workspace, source_registry="rhtl", public_key=None, dry_run=True,
        )

    # 2. cosign verify failure must fail closed
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    real_run = subprocess.run

    def cosign_fail(command, **kwargs):
        if command[0] == "/bin/cosign":
            return SimpleNamespace(returncode=1, stdout="", stderr="signature check failed")
        return real_run(command, **kwargs)

    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", cosign_fail)
    with pytest.raises(RuntimeError, match="cosign verify-blob-attestation failed"):
        publisher.publish_source(
            source_path=source_file, package="rhtl-gate-pkg", version="2.0.0",
            workspace_dir=workspace, source_registry="rhtl", public_key=str(pub_key_file), dry_run=True,
        )

    # 3. Successful verification: adapted DSSE published, RHTL evidence remains opaque (no provenance.dsse)
    def cosign_ok(command, **kwargs):
        if command[0] == "/bin/cosign":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", cosign_ok)
    publisher.publish_source(
        source_path=source_file, package="rhtl-gate-pkg", version="2.0.0",
        workspace_dir=workspace, source_registry="rhtl", public_key=str(pub_key_file), dry_run=True,
    )
    repo = workspace / "pypi.org-rhtl-gate-pkg"
    lightwell = repo / ".lightwell"
    assert (lightwell / "provenance.pep740.json").exists()
    assert (lightwell / "provenance.dsse.json").exists()
    assert not (lightwell / "provenance.dsse").exists()



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
    assert not (lightwell / "metadata.dsse").exists()


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
