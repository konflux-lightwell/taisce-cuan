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

import datetime
import subprocess
from pathlib import Path
import pytest

from taisce_cuan.provenance.attest import (
    CosignAttestationSigner,
    attest_source_mirror,
    build_ingestion_metadata,
)
from taisce_cuan.provenance.verify import (
    SourceRoute,
    VerifiedSourceArtifact,
)
from taisce_cuan.source.artifact import (
    AcquiredSourceArtifact,
    NormalizedSourceArtifact,
)


def make_dummy_verified(
    tmp_path: Path,
    pkg: str = "pkg-sample",
    ver: str = "1.0.0",
    route: SourceRoute = SourceRoute.PYPI,
) -> VerifiedSourceArtifact:
    root = tmp_path / "carrier"
    root.mkdir(parents=True, exist_ok=True)
    orig_sdist = root / "downloads" / f"{pkg}-{ver}.tar.gz"
    orig_sdist.parent.mkdir(parents=True, exist_ok=True)
    orig_sdist.write_bytes(b"dummy orig sdist content")

    norm_sdist = root / f"{pkg}-{ver}.tar.gz"
    norm_sdist.write_bytes(b"dummy norm sdist content")

    acquired = AcquiredSourceArtifact(
        root=root,
        sdist=orig_sdist,
        source_origin=root / "source-origin.json",
        rhtl_index=None,
        pep740_provenance=None,
    )
    normalized = NormalizedSourceArtifact(
        root=root,
        acquired=acquired,
        sdist=norm_sdist,
        transformation=root / "sdist-transformation.json",
    )

    return VerifiedSourceArtifact(
        package=pkg,
        version=ver,
        route=route,
        registry="pypi.org" if route == SourceRoute.PYPI else "rhtl",
        normalized_sha256="2" * 64,
        acquired_sha256="1" * 64,
        artifact=normalized,
        evidence_files=(),
    )


def test_build_ingestion_metadata_single_subject_no_git_tree(tmp_path: Path):
    verified = make_dummy_verified(tmp_path, "pkg-sample", "1.0.0")
    fixed_time = datetime.datetime(2026, 9, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)

    metadata = build_ingestion_metadata(
        verified,
        repo_name="pypi.org-pkg-sample",
        timestamp=fixed_time,
    )

    # Exactly 1 subject
    assert len(metadata.subject) == 1
    assert metadata.subject[0].name == "pkg-sample-1.0.0.tar.gz"
    assert metadata.subject[0].digest == {"sha256": "2" * 64}
    # Never emit Git tree or commit in subjects
    for subj in metadata.subject:
        assert "gitTree" not in subj.digest
        assert "gitCommit" not in subj.digest


def test_cosign_attestation_signer_fail_closed_missing_key(tmp_path: Path):
    signer = CosignAttestationSigner()
    verified = make_dummy_verified(tmp_path)
    metadata = build_ingestion_metadata(verified, "repo")

    # Missing file path fails closed
    with pytest.raises(ValueError, match="does not exist or is not a file"):
        signer.sign(metadata, verified.artifact.sdist, "/non/existent/key.pem", tmp_path / "out.dsse")


def test_cosign_attestation_signer_missing_cosign_binary(tmp_path: Path):
    # If cosign binary does not exist
    key_file = tmp_path / "key.pem"
    key_file.write_text("dummy key")
    signer = CosignAttestationSigner(cosign_bin="/usr/bin/nonexistent_cosign")
    verified = make_dummy_verified(tmp_path)
    metadata = build_ingestion_metadata(verified, "repo")

    with pytest.raises(RuntimeError, match="cosign' CLI binary is not installed"):
        signer.sign(metadata, verified.artifact.sdist, str(key_file), tmp_path / "out.dsse")


def test_cosign_signing_success_and_temp_predicate_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COSIGN_PASSWORD", "")
    # Generate test key pair
    key_prefix = tmp_path / "cosign_test"
    subprocess.run(
        ["cosign", "generate-key-pair", f"--output-key-prefix={key_prefix}"],
        check=True,
        capture_output=True,
    )
    key_path = tmp_path / "cosign_test.key"

    signer = CosignAttestationSigner()
    verified = make_dummy_verified(tmp_path)
    metadata = build_ingestion_metadata(verified, "repo")
    out_file = tmp_path / "metadata.dsse"

    result = signer.sign(metadata, verified.artifact.sdist, str(key_path), out_file)
    assert result == out_file
    assert out_file.exists()
    assert len(out_file.read_bytes()) > 0

    # Ensure no leftover predicate files in /tmp or cwd
    # The temporary predicate has suffix -predicate.json and is cleaned up in finally
    assert not list(tmp_path.glob("*-predicate.json"))


def test_attest_source_mirror_pypi_vs_rhtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COSIGN_PASSWORD", "")
    key_prefix = tmp_path / "cosign_key"
    subprocess.run(
        ["cosign", "generate-key-pair", f"--output-key-prefix={key_prefix}"],
        check=True,
        capture_output=True,
    )
    key_path = str(tmp_path / "cosign_key.key")

    # 1. PyPI signs both metadata.dsse.json and provenance.dsse.json
    pypi_verified = make_dummy_verified(tmp_path, "pkg-pypi", "1.0.0", route=SourceRoute.PYPI)
    pypi_repo = tmp_path / "repo_pypi"
    attest_source_mirror(pypi_verified, pypi_repo, "repo-pypi", sign_key=key_path)

    lightwell_pypi = pypi_repo / ".lightwell"
    assert (lightwell_pypi / "metadata.json").exists()
    assert (lightwell_pypi / "metadata.dsse.json").exists()
    assert (lightwell_pypi / "provenance.dsse.json").exists()

    # 2. RHTL signs metadata.dsse.json only; NEVER provenance.dsse.json
    rhtl_verified = make_dummy_verified(tmp_path, "pkg-rhtl", "1.0.0", route=SourceRoute.RHTL)
    rhtl_repo = tmp_path / "repo_rhtl"
    attest_source_mirror(rhtl_verified, rhtl_repo, "repo-rhtl", sign_key=key_path)

    lightwell_rhtl = rhtl_repo / ".lightwell"
    assert (lightwell_rhtl / "metadata.json").exists()
    assert (lightwell_rhtl / "metadata.dsse.json").exists()
    assert not (lightwell_rhtl / "provenance.dsse.json").exists()
