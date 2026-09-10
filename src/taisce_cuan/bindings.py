"""Validation of catalog-supplied authorization and artifact-boundary bindings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "lightwell.catalog-binding"
VERSION = 1


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(value: Any, name: str) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"binding {name} must contain exactly path and sha256")
    path, digest = value["path"], value["sha256"]
    if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValueError(f"binding {name} path must be relative and confined")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError(f"binding {name} sha256 is invalid")
    return path, digest.lower()


def validate_bindings(repo_dir: Path, archive: Path, source_origin: Path,
                      authorization: Path, boundary: Path, canonical: str, version: str) -> None:
    """Require two catalog artifacts to make the same, exact binding assertion.

    Taisce only verifies catalog output; it neither signs nor interprets DSSE payloads.
    """
    try:
        auth = json.loads(authorization.read_text())
        bound = json.loads(boundary.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("catalog authorization and boundary must be valid JSON") from exc
    expected = {"schema": SCHEMA, "version": VERSION}
    required = {"schema", "version", "source_origin", "archive", "metadata_dsse", "provenance_dsse"}
    if not isinstance(auth, dict) or not isinstance(bound, dict):
        raise ValueError("catalog bindings must be JSON objects")
    if set(auth) != required or set(bound) != required or auth != bound or any(auth.get(k) != v for k, v in expected.items()):
        raise ValueError("authorization and artifact boundary do not have the exact catalog binding schema")
    paths = {
        "source_origin": (source_origin, "source-origin.json"),
        "archive": (archive, archive.name),
    }
    for name, (actual, expected_path) in paths.items():
        path, digest = _entry(auth[name], name)
        if path != expected_path or not actual.is_file() or sha256(actual) != digest:
            raise ValueError(f"catalog binding {name} does not match the supplied artifact")
    for name in ("metadata_dsse", "provenance_dsse"):
        path, digest = _entry(auth[name], name)
        candidate = (repo_dir / path).resolve()
        if repo_dir.resolve() not in candidate.parents or not candidate.is_file() or sha256(candidate) != digest:
            raise ValueError(f"catalog binding {name} does not match a repository file")
