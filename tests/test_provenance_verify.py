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

import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import pytest

from taisce_cuan.source.mirror import GitMirrorPublisher
from taisce_cuan.provenance.verify import (
    ProvenanceVerificationError,
    SourceRoute,
    adapt_rhtl_pep740,
    copy_verified_evidence,
    extract_pep740_predicate_type,
    load_json_object,
    locate_source_artifact_paths,
    normalize_source_route,
    verify_acquired_source,
    verify_blob_attestation,
    verify_lightwell_attestations,
    verify_normalized_source_artifact,
    verify_relative_artifact_path,
    verify_sha256,
    verify_transformation,
    verify_upstream_evidence,
)
from taisce_cuan.sdist import compute_sha256
from taisce_cuan.source.artifact import ArtifactError


def make_carrier(
    root: Path,
    pkg: str,
    ver: str,
    *,
    registry: str = "pypi.org",
    mode: str = "pypi",
    distinct_normalized: bool = False,
    advertised_rhtl: bool = False,
    include_rhtl_index: bool = False,
) -> tuple[Path, Path]:
    carrier = root / f"carrier_{pkg}_{ver}"
    carrier.mkdir(parents=True, exist_ok=True)
    downloads = carrier / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    orig_sdist = downloads / f"{pkg}-{ver}.tar.gz"
    # Create valid tar.gz
    src_dir = carrier / f"src_{pkg}"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "pyproject.toml").write_text(f"[project]\nname='{pkg}'\nversion='{ver}'\n")
    with tarfile.open(orig_sdist, "w:gz") as tar:
        tar.add(src_dir, arcname=f"{pkg}-{ver}")

    norm_sdist = carrier / f"{pkg}-{ver}.tar.gz"
    if distinct_normalized:
        (src_dir / "normalized.txt").write_text("Fromager normalized content")
        with tarfile.open(norm_sdist, "w:gz") as tar:
            tar.add(src_dir, arcname=f"{pkg}-{ver}")
    else:
        norm_sdist.write_bytes(orig_sdist.read_bytes())

    orig_sha = compute_sha256(orig_sdist)
    norm_sha = compute_sha256(norm_sdist)

    pep740_sha = None
    if advertised_rhtl:
        pep740_path = carrier / "provenance.pep740.json"
        pep740_bytes = b'{"predicateType":"https://slsa.dev/provenance/v1"}'
        pep740_path.write_bytes(pep740_bytes)
        pep740_sha = compute_sha256(pep740_path)

    pep691_sha = None
    if include_rhtl_index:
        pep691_path = carrier / "rhtl-index.pep691.json"
        pep691_bytes = b'{"files":[]}'
        pep691_path.write_bytes(pep691_bytes)
        pep691_sha = compute_sha256(pep691_path)

    origin_dict = {
        "schema": "https://lightwell.dev/schemas/source-origin/v1",
        "acquired": {
            "registry": registry,
            "url": f"https://registry.example.com/{pkg}/{ver}",
            "sha256": orig_sha,
            "size": len(orig_sdist.read_bytes()),
            "package": pkg,
            "version": ver,
            "path": f"downloads/{pkg}-{ver}.tar.gz",
        },
        "provenance": {
            "mode": mode,
            "advertised": advertised_rhtl,
            "status": "advertised" if advertised_rhtl else "not-advertised",
            "sha256": pep740_sha,
            "rhtl": {
                "status": "advertised" if advertised_rhtl else "not-advertised",
                "evidence": {
                    "sha256": pep691_sha,
                },
            },
        },
    }
    origin_file = carrier / "source-origin.json"
    origin_file.write_text(json.dumps(origin_dict, sort_keys=True) + "\n")
    origin_sha = compute_sha256(origin_file)

    transform_dict = {
        "schema": "https://lightwell.dev/schemas/sdist-transformation/v1",
        "input": {"sha256": orig_sha},
        "output": {"sha256": norm_sha, "path": f"{pkg}-{ver}.tar.gz"},
        "source_origin_sha256": origin_sha,
        "transformation": "normalized-sdist",
    }
    (carrier / "sdist-transformation.json").write_text(json.dumps(transform_dict, sort_keys=True) + "\n")

    return norm_sdist, carrier


def test_normalize_source_route():
    assert normalize_source_route("rhtl") == SourceRoute.RHTL
    assert normalize_source_route("packages.redhat.com") == SourceRoute.RHTL
    assert normalize_source_route("pypi") == SourceRoute.PYPI
    assert normalize_source_route("pypi.org") == SourceRoute.PYPI
    assert normalize_source_route("pypi.python.org") == SourceRoute.PYPI

    with pytest.raises(ProvenanceVerificationError, match="Unsupported source registry"):
        normalize_source_route("unsupported-forge.org")


def test_verify_relative_artifact_path(tmp_path: Path):
    root = tmp_path / "carrier"
    root.mkdir()
    downloads = root / "downloads"
    downloads.mkdir()
    (downloads / "pkg.tar.gz").write_text("dummy")

    # Safe relative path
    p = verify_relative_artifact_path(root, "downloads/pkg.tar.gz", "test")
    assert p == downloads / "pkg.tar.gz"

    # Absolute path rejected
    with pytest.raises(ArtifactError, match="must be relative"):
        verify_relative_artifact_path(root, "/etc/passwd", "test")

    # Traversal path rejected
    with pytest.raises(ArtifactError, match="Dangerous path traversal"):
        verify_relative_artifact_path(root, "../outside.txt", "test")


def test_verify_sha256(tmp_path: Path):
    f = tmp_path / "sample.txt"
    f.write_text("hello world\n")
    actual = compute_sha256(f)

    # Success matching
    assert verify_sha256(f, actual, "test") == actual

    # Mismatch fails closed
    with pytest.raises(ProvenanceVerificationError, match="digest mismatch"):
        verify_sha256(f, "0" * 64, "test")


def test_verify_normalized_source_artifact_pypi(tmp_path: Path):
    norm_sdist, carrier = make_carrier(tmp_path, "sample-pkg", "1.0.0", registry="pypi.org", mode="pypi")
    verified = verify_normalized_source_artifact(norm_sdist, package="sample-pkg", version="1.0.0")

    assert verified.package == "sample-pkg"
    assert verified.version == "1.0.0"
    assert verified.route == SourceRoute.PYPI
    assert verified.registry == "pypi.org"
    assert verified.normalized_sha256 == compute_sha256(norm_sdist)
    assert verified.acquired_sha256 == compute_sha256(carrier / "downloads" / "sample-pkg-1.0.0.tar.gz")
    assert verified.artifact.sdist == norm_sdist
    # PyPI evidence files: source-origin.json and sdist-transformation.json
    assert len(verified.evidence_files) == 2


def test_verify_normalized_source_artifact_rhtl_advertised(tmp_path: Path):
    norm_sdist, carrier = make_carrier(
        tmp_path,
        "sample-rhtl",
        "2.0.0",
        registry="rhtl",
        mode="rhtl",
        advertised_rhtl=True,
        include_rhtl_index=True,
    )
    verified = verify_normalized_source_artifact(norm_sdist, package="sample-rhtl", version="2.0.0")

    assert verified.route == SourceRoute.RHTL
    assert verified.registry == "rhtl"
    names = [f.name for f in verified.evidence_files]
    assert "source-origin.json" in names
    assert "sdist-transformation.json" in names
    assert "provenance.pep740.json" in names
    assert "rhtl-index.pep691.json" not in names


def test_verify_normalized_source_artifact_rhtl_unadvertised(tmp_path: Path):
    norm_sdist, carrier = make_carrier(
        tmp_path,
        "sample-unadv",
        "1.5.0",
        registry="packages.redhat.com",
        mode="rhtl",
        advertised_rhtl=False,
        include_rhtl_index=True,
    )
    verified = verify_normalized_source_artifact(norm_sdist, package="sample-unadv", version="1.5.0")

    assert verified.route == SourceRoute.RHTL
    names = [f.name for f in verified.evidence_files]
    assert "source-origin.json" in names
    assert "sdist-transformation.json" in names
    assert "rhtl-index.pep691.json" in names
    assert "provenance.pep740.json" not in names


def test_verify_distinct_normalized_and_original_hashes(tmp_path: Path):
    norm_sdist, carrier = make_carrier(
        tmp_path, "distinct-hashes", "1.0.0", distinct_normalized=True
    )
    verified = verify_normalized_source_artifact(norm_sdist, package="distinct-hashes", version="1.0.0")

    assert verified.normalized_sha256 != verified.acquired_sha256


def test_verify_fails_on_tampered_acquired_archive(tmp_path: Path):
    norm_sdist, carrier = make_carrier(tmp_path, "tampered-orig", "1.0.0")
    orig = carrier / "downloads" / "tampered-orig-1.0.0.tar.gz"
    orig.write_text("tampered bytes")

    with pytest.raises(ProvenanceVerificationError, match="original archive digest mismatch"):
        verify_normalized_source_artifact(norm_sdist, package="tampered-orig", version="1.0.0")


def test_verify_fails_on_transformation_input_mismatch(tmp_path: Path):
    norm_sdist, carrier = make_carrier(tmp_path, "transform-mismatch", "1.0.0")
    tf = carrier / "sdist-transformation.json"
    data = json.loads(tf.read_text())
    data["input"]["sha256"] = "1" * 64
    tf.write_text(json.dumps(data))

    with pytest.raises(ProvenanceVerificationError, match="sdist transformation input does not match"):
        verify_normalized_source_artifact(norm_sdist, package="transform-mismatch", version="1.0.0")


def test_verify_fails_on_route_mode_conflict(tmp_path: Path):
    # Registry is rhtl, but mode is set to pypi -> fail closed
    norm_sdist, carrier = make_carrier(
        tmp_path, "conflicting-route", "1.0.0", registry="rhtl", mode="pypi"
    )

    with pytest.raises(ProvenanceVerificationError, match="Conflicting provenance mode"):
        verify_normalized_source_artifact(norm_sdist, package="conflicting-route", version="1.0.0")


def test_verify_fails_on_origin_package_or_version_mismatch(tmp_path: Path):
    norm_sdist, carrier = make_carrier(tmp_path, "mismatch-meta", "1.0.0")

    # Mismatched requested package
    with pytest.raises(ProvenanceVerificationError, match="source-origin acquired package"):
        verify_normalized_source_artifact(norm_sdist, package="different-pkg", version="1.0.0")

    # Mismatched requested version
    with pytest.raises(ProvenanceVerificationError, match="source-origin acquired version"):
        verify_normalized_source_artifact(norm_sdist, package="mismatch-meta", version="2.0.0")


def test_verify_rejects_traversal_in_acquired_path(tmp_path: Path):
    # A source-origin acquired.path pointing outside the carrier must be rejected
    # before any archive is read.
    norm_sdist, carrier = make_carrier(tmp_path, "traversal", "1.0.0")
    origin_file = carrier / "source-origin.json"
    origin = json.loads(origin_file.read_text())
    origin["acquired"]["path"] = "../evil-1.0.0.tar.gz"
    origin_file.write_text(json.dumps(origin))

    with pytest.raises(ArtifactError, match="Dangerous path traversal"):
        verify_normalized_source_artifact(norm_sdist, package="traversal", version="1.0.0")


def test_verify_fails_on_missing_registry(tmp_path: Path):
    # Without an explicit acquired.registry, no route can be resolved (no implicit
    # PyPI default) -> fail closed.
    norm_sdist, carrier = make_carrier(tmp_path, "no-registry", "1.0.0")
    origin_file = carrier / "source-origin.json"
    origin = json.loads(origin_file.read_text())
    del origin["acquired"]["registry"]
    origin_file.write_text(json.dumps(origin))

    with pytest.raises(ProvenanceVerificationError, match="Unsupported source registry"):
        verify_normalized_source_artifact(norm_sdist, package="no-registry", version="1.0.0")


def test_verify_fails_on_non_dict_sections(tmp_path: Path):
    # source-origin.json must be a JSON object with dict acquired/provenance sections.
    norm_sdist, carrier = make_carrier(tmp_path, "non-dict", "1.0.0")
    origin_file = carrier / "source-origin.json"
    transform_file = carrier / "sdist-transformation.json"

    # Whole document is not an object
    origin_file.write_text("[]")
    with pytest.raises(ProvenanceVerificationError, match="must be a JSON object"):
        verify_normalized_source_artifact(norm_sdist, package="non-dict", version="1.0.0")

    # acquired section is not a dict
    _, carrier = make_carrier(tmp_path, "non-dict-acq", "1.0.0")
    norm_sdist = carrier / "non-dict-acq-1.0.0.tar.gz"
    origin_file = carrier / "source-origin.json"
    origin = json.loads(origin_file.read_text())
    origin["acquired"] = "not-a-dict"
    origin_file.write_text(json.dumps(origin))
    with pytest.raises(ProvenanceVerificationError, match="lacks acquired section"):
        verify_normalized_source_artifact(norm_sdist, package="non-dict-acq", version="1.0.0")

    # provenance section is not a dict (re-anchor the transformation digest so the
    # tampered origin passes the earlier digest gate and reaches the check).
    _, carrier = make_carrier(tmp_path, "non-dict-prov", "1.0.0")
    norm_sdist = carrier / "non-dict-prov-1.0.0.tar.gz"
    origin_file = carrier / "source-origin.json"
    transform_file = carrier / "sdist-transformation.json"
    origin = json.loads(origin_file.read_text())
    origin["provenance"] = "not-a-dict"
    origin_file.write_text(json.dumps(origin))
    tf = json.loads(transform_file.read_text())
    tf["source_origin_sha256"] = compute_sha256(origin_file)
    transform_file.write_text(json.dumps(tf))
    with pytest.raises(ProvenanceVerificationError, match="lacks provenance section"):
        verify_normalized_source_artifact(norm_sdist, package="non-dict-prov", version="1.0.0")


def test_verify_fails_on_pypi_with_extraneous_pep740(tmp_path: Path):
    norm_sdist, carrier = make_carrier(tmp_path, "extraneous-pep740", "1.0.0", registry="pypi.org", mode="pypi")
    # Add extraneous pep740 to PyPI carrier
    (carrier / "provenance.pep740.json").write_text('{"statement":"bad"}')

    with pytest.raises(ProvenanceVerificationError, match="PyPI source carrier must not contain RHTL provenance.pep740.json"):
        verify_normalized_source_artifact(norm_sdist, package="extraneous-pep740", version="1.0.0")


def test_copy_verified_evidence(tmp_path: Path):
    norm_sdist, carrier = make_carrier(tmp_path, "copy-ev", "1.0.0")
    verified = verify_normalized_source_artifact(norm_sdist, package="copy-ev", version="1.0.0")

    dest = tmp_path / "lightwell_target"
    copy_verified_evidence(verified.evidence_files, dest)

    assert (dest / "source-origin.json").exists()
    assert (dest / "sdist-transformation.json").exists()
    assert (dest / "source-origin.json").read_bytes() == (carrier / "source-origin.json").read_bytes()


def test_verify_lightwell_attestations(tmp_path: Path):
    lightwell = tmp_path / ".lightwell"
    lightwell.mkdir()

    # Unsigned: no-op
    verify_lightwell_attestations(lightwell, route=SourceRoute.PYPI, signed=False)

    # Signed PyPI missing metadata.dsse.json -> fails closed
    with pytest.raises(ProvenanceVerificationError, match="missing .lightwell/metadata.dsse.json"):
        verify_lightwell_attestations(lightwell, route=SourceRoute.PYPI, signed=True)

    (lightwell / "metadata.dsse.json").write_text("meta dsse")

    # Signed PyPI missing provenance.dsse.json -> fails closed
    with pytest.raises(ProvenanceVerificationError, match="missing .lightwell/provenance.dsse.json"):
        verify_lightwell_attestations(lightwell, route=SourceRoute.PYPI, signed=True)

    (lightwell / "provenance.dsse.json").write_text("prov dsse")
    # Now valid for PyPI
    verify_lightwell_attestations(lightwell, route=SourceRoute.PYPI, signed=True)


def test_rhtl_pep740_adaptation_preserves_base64_and_rejects_malformed(tmp_path: Path):
    raw = tmp_path / "provenance.pep740.json"
    output = tmp_path / "provenance.dsse.json"
    payload = "eyJwcmVjaXNlbHkiOiJub3QtZGVjb2RlZCJ9=="
    signature = "c2lnbmF0dXJlLXN0cmluZw=="
    raw.write_bytes(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": {
        "statement": payload, "signature": signature}}]}]}).encode())
    
    # Test direct module function
    adapt_rhtl_pep740(raw, output)
    adapted = json.loads(output.read_text())
    assert adapted == {"payloadType": "application/vnd.in-toto+json", "payload": payload,
                       "signatures": [{"sig": signature}]}
    assert json.loads(raw.read_bytes())["attestation_bundles"][0]["attestations"][0]["envelope"]["statement"] == payload

    # Test publisher facade
    output.unlink()
    GitMirrorPublisher.adapt_rhtl_pep740(raw, output)
    assert output.exists()

    raw.write_text("{malformed")
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    # Rejects multiple bundles
    raw.write_text(json.dumps({"attestation_bundles": [
        {"attestations": [{"envelope": {"statement": payload, "signature": signature}}]},
        {"attestations": [{"envelope": {"statement": payload, "signature": signature}}]},
    ]}))
    with pytest.raises(ValueError, match="bundle"):
        adapt_rhtl_pep740(raw, output)
    # Rejects multiple attestations in a bundle
    raw.write_text(json.dumps({"attestation_bundles": [
        {"attestations": [
            {"envelope": {"statement": payload, "signature": signature}},
            {"envelope": {"statement": payload, "signature": signature}},
        ]},
    ]}))
    with pytest.raises(ValueError, match="attestation"):
        adapt_rhtl_pep740(raw, output)
    # Rejects malformed nested elements (would raise AttributeError if not handled)
    raw.write_text(json.dumps({"attestation_bundles": [{"attestations": ["not-a-dict"]}]}))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    raw.write_text(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": "not-a-dict"}]}]}))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    raw.write_text(json.dumps({"attestation_bundles": ["not-a-dict"]}))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    raw.write_text(json.dumps(["not-a-dict"]))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)


def test_verify_blob_attestation_targets_acquired_and_never_resigns(monkeypatch, tmp_path: Path):
    source = tmp_path / "downloads" / "original.tar.gz"
    signature = tmp_path / "provenance.dsse.json"
    key = tmp_path / "release3.pub"
    source.parent.mkdir(); source.write_bytes(b"original"); signature.write_text("{}\n"); key.write_text("public")
    captured = []
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", lambda command, **kwargs: captured.append(command) or SimpleNamespace(returncode=0, stderr=""))
    
    # Test direct module function
    verify_blob_attestation(source, signature, str(key))
    assert captured[0][1] == "verify-blob-attestation"
    assert captured[0][-1] == str(source)
    assert "--key" in captured[0]
    assert str(key) in captured[0]
    assert "--signature" in captured[0] or any(arg.startswith("--signature=") or arg.startswith("--bundle=") for arg in captured[0])
    assert "--type" in captured[0] or any(arg.startswith("--type=") for arg in captured[0])
    assert "attest-blob" not in captured[0]

    # Test publisher facade
    captured.clear()
    GitMirrorPublisher.verify_blob_attestation(source, signature, str(key))
    assert len(captured) == 1
    assert captured[0][1] == "verify-blob-attestation"

    # Fails closed on missing or empty key
    with pytest.raises(ValueError, match="public verification key is required"):
        verify_blob_attestation(source, signature, "")
    with pytest.raises(ValueError, match="does not exist or is not a file"):
        verify_blob_attestation(source, signature, "/non/existent/key.pub")

    # Fails closed if cosign CLI binary is missing
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="Cosign CLI is required"):
        verify_blob_attestation(source, signature, str(key))

    # Fails closed if cosign returns non-zero exit code
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="signature verification failed"))
    with pytest.raises(RuntimeError, match="cosign verify-blob-attestation failed"):
        verify_blob_attestation(source, signature, str(key))


def test_verify_blob_attestation_falls_back_to_bundle_flag(monkeypatch, tmp_path: Path):
    """Newer cosign rejects --signature; verification must retry with --bundle and succeed."""
    source = tmp_path / "downloads" / "original.tar.gz"
    signature = tmp_path / "provenance.dsse.json"
    key = tmp_path / "release3.pub"
    source.parent.mkdir()
    source.write_bytes(b"original")
    signature.write_text("{}\n")
    key.write_text("public")

    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(command)
        if "--signature" in command:
            return SimpleNamespace(returncode=1, stderr="Error: unknown flag: --signature\nuse --bundle")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", fake_run)

    # Must not raise: the --bundle retry succeeds.
    verify_blob_attestation(source, signature, str(key))

    assert len(captured) == 2
    assert "--signature" in captured[0]
    assert "--bundle" in captured[1]
    assert str(signature) in captured[1]


def test_verify_blob_attestation_does_not_retry_on_genuine_failure(monkeypatch, tmp_path: Path):
    """A real signature mismatch (not a CLI-compat error) must fail closed without a --bundle retry."""
    source = tmp_path / "downloads" / "original.tar.gz"
    signature = tmp_path / "provenance.dsse.json"
    key = tmp_path / "release3.pub"
    source.parent.mkdir()
    source.write_bytes(b"original")
    signature.write_text("{}\n")
    key.write_text("public")

    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=1, stderr="signature verification failed")

    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="cosign verify-blob-attestation failed"):
        verify_blob_attestation(source, signature, str(key))

    assert len(captured) == 1
    assert all("--bundle" not in arg for arg in captured[0])


