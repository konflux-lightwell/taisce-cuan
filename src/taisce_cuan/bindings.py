"""Validation of catalog authorization and artifact-boundary bindings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "lightwell.catalog-binding"
VERSION = 2
_HEX = set("0123456789abcdef")


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
    candidate = Path(path) if isinstance(path, str) else Path("/")
    if not isinstance(path, str) or not path or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"binding {name} path must be relative and confined")
    if not isinstance(digest, str) or len(digest) != 64 or set(digest.lower()) - _HEX:
        raise ValueError(f"binding {name} sha256 is invalid")
    return path, digest.lower()


def _repo_entry(repo: Path, value: Any, name: str) -> Path:
    path, digest = _entry(value, name)
    root = repo.resolve()
    candidate = (repo / path).resolve()
    if root not in candidate.parents or not candidate.is_file() or sha256(candidate) != digest:
        raise ValueError(f"catalog binding {name} does not match a repository file")
    return candidate


def validate_bindings(repo_dir: Path, archive: Path, source_origin: Path,
                      authorization: Path, boundary: Path, canonical: str, version: str) -> None:
    """Require byte-identical catalog authorization and artifact-boundary assertions."""
    try:
        authorization_bytes = authorization.read_bytes()
        boundary_bytes = boundary.read_bytes()
        if authorization_bytes != boundary_bytes:
            raise ValueError("authorization and artifact boundary must be byte-identical assertions")
        auth = json.loads(authorization_bytes)
        bound = json.loads(boundary_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("catalog authorization and boundary must be valid JSON") from exc
    if not isinstance(auth, dict) or not isinstance(bound, dict) or auth != bound:
        raise ValueError("authorization and artifact boundary must be byte-identical assertions")
    required = {"schema", "version", "source_origin", "transformation", "normalized_archive",
                "metadata", "metadata_dsse"}
    evidence = {"provenance_dsse", "upstream_provenance_response"}
    if set(auth) not in (required | {"provenance_dsse"}, required | {"upstream_provenance_response"}) or ("provenance_dsse" in auth) == ("upstream_provenance_response" in auth):
        raise ValueError("catalog binding requires exactly one provenance evidence alternative")
    if auth.get("schema") != SCHEMA or auth.get("version") != VERSION:
        raise ValueError("unsupported catalog binding schema")

    # Every sidecar and attestation reference is a repository-relative path.
    origin_file = _repo_entry(repo_dir, auth["source_origin"], "source_origin")
    transformation = _repo_entry(repo_dir, auth["transformation"], "transformation")
    _repo_entry(repo_dir, auth["metadata"], "metadata")
    _repo_entry(repo_dir, auth["metadata_dsse"], "metadata_dsse")
    if origin_file.name != "source-origin.json":
        raise ValueError("source_origin must reference source-origin.json")
    if not source_origin.is_file() or source_origin.read_bytes() != origin_file.read_bytes():
        raise ValueError("source origin sidecar differs from the bound repository artifact")
    npath, ndigest = _entry(auth["normalized_archive"], "normalized_archive")
    normalized = (repo_dir / npath).resolve()
    if repo_dir.resolve() not in normalized.parents or not normalized.is_file() or sha256(normalized) != ndigest:
        raise ValueError("normalized_archive does not match a repository file")
    if not archive.is_file() or normalized.read_bytes() != archive.read_bytes():
        raise ValueError("normalized_archive does not match the supplied archive")

    try:
        origin = json.loads(origin_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("source origin sidecar must be valid JSON") from exc
    acquired = origin.get("verified_sha256")
    if not isinstance(acquired, str) or len(acquired) != 64 or set(acquired.lower()) - _HEX:
        raise ValueError("source origin verified_sha256 is invalid")
    # Transformation is deliberately bound independently: upstream and normalized bytes may differ.
    if transformation.stat().st_size == 0:
        raise ValueError("transformation evidence must not be empty")

    if "provenance_dsse" in auth:
        evidence_file = _repo_entry(repo_dir, auth["provenance_dsse"], "provenance_dsse")
        try:
            raw = evidence_file.read_bytes()
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or "payloadType" not in parsed or "payload" not in parsed:
                raise ValueError
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("provenance_dsse must be a valid DSSE envelope") from exc
    else:
        response = auth["upstream_provenance_response"]
        if not isinstance(response, dict) or set(response) != {"path", "sha256", "url", "status"}:
            raise ValueError("upstream_provenance_response must contain path, sha256, url, status")
        raw_file = _repo_entry(repo_dir, {"path": response["path"], "sha256": response["sha256"]},
                               "upstream_provenance_response")
        if not isinstance(response["url"], str) or response["url"] != origin.get("provenance_url"):
            raise ValueError("upstream provenance URL does not match source origin")
        if response["status"] != origin.get("provenance_response_status"):
            raise ValueError("upstream provenance status does not match source origin")
        if origin.get("provenance_response_sha256") != sha256(raw_file):
            raise ValueError("upstream provenance digest does not match source origin")
        # Opaque upstream evidence is raw, not a DSSE envelope.
        try:
            parsed = json.loads(raw_file.read_bytes())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "payloadType" in parsed:
            raise ValueError("opaque upstream provenance response must not be DSSE")
