import hashlib
import json
from pathlib import Path

import pytest

from taisce_cuan.bindings import SCHEMA, VERSION, validate_bindings


def test_catalog_binding_requires_exact_digests(tmp_path: Path):
    archive = tmp_path / "demo-1.tar.gz"
    origin = tmp_path / "source-origin.json"
    archive.write_bytes(b"archive")
    origin.write_bytes(b"origin")
    repo = tmp_path / "repo"
    (repo / ".lightwell").mkdir(parents=True)
    metadata = repo / ".lightwell" / "metadata.dsse"
    provenance = repo / ".lightwell" / "provenance.dsse"
    metadata.write_bytes(b"metadata")
    provenance.write_bytes(b"provenance")

    def item(path):
        return {"path": path, "sha256": hashlib.sha256((repo / path).read_bytes()).hexdigest()}

    def external(path):
        return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    binding = {"schema": SCHEMA, "version": VERSION, "source_origin": external(origin),
               "archive": external(archive), "metadata_dsse": item(".lightwell/metadata.dsse"),
               "provenance_dsse": item(".lightwell/provenance.dsse")}
    auth, boundary = tmp_path / "auth.json", tmp_path / "boundary.json"
    auth.write_text(json.dumps(binding))
    boundary.write_text(json.dumps(binding))
    validate_bindings(repo, archive, origin, auth, boundary, "demo", "1")

    binding["archive"]["sha256"] = "0" * 64
    boundary.write_text(json.dumps(binding))
    with pytest.raises(ValueError):
        validate_bindings(repo, archive, origin, auth, boundary, "demo", "1")
