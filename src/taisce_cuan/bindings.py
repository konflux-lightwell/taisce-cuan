"""Validation of catalog authorization and artifact-boundary bindings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "lightwell.catalog-binding"
VERSION = 3
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


def _unavailable(repo: Path, value: Any, origin: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {"status", "reason", "index_response"}:
        raise ValueError("upstream_provenance_unavailable must contain status, reason, index_response")
    if value["status"] != "unavailable" or value["reason"] != "not-advertised":
        raise ValueError("unavailable upstream provenance has an invalid status or reason")
    response = value["index_response"]
    if not isinstance(response, dict) or set(response) != {"path", "sha256", "url", "status"}:
        raise ValueError("index_response must contain path, sha256, url, status")
    if response["status"] != 200 or not isinstance(response["url"], str):
        raise ValueError("unavailable index response requires HTTP status 200 and URL")
    response_path = _repo_entry(repo, {"path": response["path"], "sha256": response["sha256"]}, "upstream_provenance_unavailable.index_response")
    if origin.get("rhtl_response_path") != response["path"] or origin.get("rhtl_response_sha256") != sha256(response_path):
        raise ValueError("RHTL response evidence does not match source origin")
    if origin.get("rhtl_response_url") != response["url"] or origin.get("rhtl_response_status") != 200:
        raise ValueError("RHTL response URL or status does not match source origin")
    try:
        index = json.loads(response_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("RHTL response evidence must be valid JSON") from exc
    if not isinstance(index, dict) or not isinstance(index.get("files"), list):
        raise ValueError("RHTL response evidence must be a PEP 691 index")
    matches = []
    for item in index["files"]:
        if not isinstance(item, dict) or item.get("url") != origin.get("artifact_url"):
            continue
        hashes = item.get("hashes")
        if isinstance(hashes, dict) and hashes.get("sha256") == origin.get("declared_sha256"):
            matches.append(item)
    if len(matches) != 1:
        raise ValueError("RHTL response does not identify the source artifact")
    if "provenance" in matches[0]:
        raise ValueError("advertised provenance cannot use unavailable evidence")


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
    alternatives = {"provenance_dsse", "upstream_provenance_response", "upstream_provenance_unavailable"}
    if auth.get("schema") != SCHEMA or auth.get("version") != VERSION or set(auth) - (required | alternatives):
        raise ValueError("unsupported catalog binding schema")
    if len(set(auth) & alternatives) != 1 or not required.issubset(auth):
        raise ValueError("catalog binding requires exactly one provenance evidence alternative")

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
        transformation_data = json.loads(transformation.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("source origin and transformation must be valid JSON") from exc
    acquired = origin.get("verified_sha256")
    expected = transformation_data.get("input", {}).get("sha256") if isinstance(transformation_data, dict) else None
    if not isinstance(acquired, str) or not isinstance(expected, str) or acquired.lower() != expected.lower():
        raise ValueError("acquired artifact digest does not match transformation.input.sha256")
    carrier = origin.get("acquired_artifact")
    if not isinstance(carrier, dict) or carrier.get("sha256", "").lower() != acquired.lower():
        raise ValueError("source origin acquired_artifact must pair with verified_sha256")
    carrier_path = carrier.get("path")
    if not isinstance(carrier_path, str) or not carrier_path or Path(carrier_path).is_absolute() or ".." in Path(carrier_path).parts:
        raise ValueError("source origin acquired_artifact path is invalid")
    acquired_file = (source_origin.parent / carrier_path).resolve()
    if source_origin.parent.resolve() not in acquired_file.parents or not acquired_file.is_file() or sha256(acquired_file) != acquired.lower():
        raise ValueError("acquired_artifact does not match verified_sha256")
    registry = origin.get("source_registry")
    if registry not in {"rhtl", "pypi.org"} or origin.get("canonical_name") != canonical or str(origin.get("version")) != str(version):
        raise ValueError("source origin identity does not match catalog binding")
    if transformation.stat().st_size == 0:
        raise ValueError("transformation evidence must not be empty")

    if "provenance_dsse" in auth:
        if registry != "pypi.org":
            raise ValueError("provenance_dsse is only valid for PyPI source origins")
        evidence_file = _repo_entry(repo_dir, auth["provenance_dsse"], "provenance_dsse")
        try:
            parsed = json.loads(evidence_file.read_bytes())
            if not isinstance(parsed, dict) or "payloadType" not in parsed or "payload" not in parsed:
                raise ValueError
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("provenance_dsse must be a valid DSSE envelope") from exc
    elif "upstream_provenance_response" in auth:
        if registry != "rhtl":
            raise ValueError("upstream provenance response requires an RHTL source origin")
        response = auth["upstream_provenance_response"]
        if not isinstance(response, dict) or set(response) != {"path", "sha256", "url", "status"}:
            raise ValueError("upstream_provenance_response must contain path, sha256, url, status")
        if response["path"] != "provenance-response.bin" or origin.get("provenance_response_path") != response["path"]:
            raise ValueError("upstream provenance response must use provenance-response.bin")
        raw_file = _repo_entry(repo_dir, {"path": response["path"], "sha256": response["sha256"]}, "upstream_provenance_response")
        if response["url"] != origin.get("provenance_url") or response["status"] != origin.get("provenance_response_status"):
            raise ValueError("upstream provenance response does not match source origin")
        try:
            parsed = json.loads(raw_file.read_bytes())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "payloadType" in parsed:
            raise ValueError("opaque upstream provenance response must not be DSSE")
    else:
        if registry != "rhtl":
            raise ValueError("unavailable upstream provenance requires an RHTL source origin")
        _unavailable(repo_dir, auth["upstream_provenance_unavailable"], origin)
