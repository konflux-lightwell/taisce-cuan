import hashlib
import json
from pathlib import Path

import pytest

from taisce_cuan.bindings import SCHEMA, VERSION, validate_bindings


def test_catalog_binding_requires_exact_digests(tmp_path: Path):
    archive = tmp_path / "demo-1.tar.gz"
    origin = tmp_path / "source-origin.json"
    archive.write_bytes(b"normalized archive")
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    origin_data = {"source_registry": "pypi.org", "canonical_name": "demo", "version": "1",
                   "artifact_url": "https://example.test/demo-1.tar.gz", "declared_sha256": archive_digest,
                   "verified_sha256": archive_digest,
                   "acquired_artifact": {"path": archive.name, "sha256": archive_digest}}
    origin.write_text(json.dumps(origin_data))
    repo = tmp_path / "repo"
    (repo / ".lightwell").mkdir(parents=True)
    (repo / "source-origin.json").write_bytes(origin.read_bytes())
    (repo / "transform.txt").write_text(json.dumps({"input": {"sha256": archive_digest}}))
    (repo / "demo-1.tar.gz").write_bytes(archive.read_bytes())
    (repo / ".lightwell/metadata.json").write_bytes(b"metadata")
    (repo / ".lightwell/metadata.dsse").write_bytes(b'{"payloadType":"x","payload":"eA=="}')
    provenance = repo / ".lightwell/provenance.dsse"
    provenance.write_bytes(b'{"payloadType":"x","payload":"eA=="}')

    def item(path):
        return {"path": path, "sha256": hashlib.sha256((repo / path).read_bytes()).hexdigest()}

    binding = {"schema": SCHEMA, "version": VERSION, "source_origin": item("source-origin.json"),
               "transformation": item("transform.txt"), "normalized_archive": item("demo-1.tar.gz"),
               "metadata": item(".lightwell/metadata.json"), "metadata_dsse": item(".lightwell/metadata.dsse"),
               "provenance_dsse": item(".lightwell/provenance.dsse")}
    auth, boundary = tmp_path / "auth.json", tmp_path / "boundary.json"
    raw = json.dumps(binding, separators=(",", ":")).encode()
    auth.write_bytes(raw)
    boundary.write_bytes(raw)
    validate_bindings(repo, archive, origin, auth, boundary, "demo", "1")

    binding["normalized_archive"]["sha256"] = "0" * 64
    boundary.write_text(json.dumps(binding))
    with pytest.raises(ValueError):
        validate_bindings(repo, archive, origin, auth, boundary, "demo", "1")


def test_unavailable_alternative_uses_v3_index_response_and_validates_safely(tmp_path: Path):
    from taisce_cuan.bindings import _unavailable, sha256

    response = tmp_path / "index.json"
    response.write_text(json.dumps({"files": [{"url": "https://example.test/demo.tar.gz",
                                                 "hashes": {"sha256": "a" * 64}}]}))
    origin = {"artifact_url": "https://example.test/demo.tar.gz",
              "declared_sha256": "a" * 64,
              "rhtl_response_path": response.name,
              "rhtl_response_sha256": sha256(response),
              "rhtl_response_url": "https://example.test/simple/demo/",
              "rhtl_response_status": 200}
    value = {"status": "unavailable", "reason": "not-advertised",
             "index_response": {"path": response.name, "sha256": sha256(response),
                                 "url": origin["rhtl_response_url"], "status": 200}}
    _unavailable(tmp_path, value, origin)

    response.write_text(json.dumps({"files": [{"url": origin["artifact_url"], "hashes": "invalid"}]}))
    with pytest.raises(ValueError):
        _unavailable(tmp_path, value, {**origin, "rhtl_response_sha256": sha256(response)})
