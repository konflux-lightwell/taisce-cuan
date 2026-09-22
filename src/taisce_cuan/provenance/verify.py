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

import base64
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from taisce_cuan.sdist import canonicalize_name, compute_sha256
from taisce_cuan.source.artifact import (
    AcquiredSourceArtifact,
    ArtifactError,
    NormalizedSourceArtifact,
)

logger = logging.getLogger(__name__)


class ProvenanceVerificationError(ValueError):
    """Raised when source-artifact or attestation verification fails (fail closed)."""


class SourceRoute(StrEnum):
    PYPI = "pypi"
    RHTL = "rhtl"


@dataclass(frozen=True)
class VerifiedSourceArtifact:
    """Immutable verified source artifact with validated paths, digests, and route."""

    package: str
    version: str
    route: SourceRoute
    registry: str
    normalized_sha256: str
    acquired_sha256: str
    artifact: NormalizedSourceArtifact
    evidence_files: tuple[Path, ...]


def locate_source_artifact_paths(
    source_path: Path, package: str, version: str
) -> tuple[Path, Path, Path, Path]:
    """
    Locate the root and required paths of the normalized source carrier.
    Returns (carrier_root, source_path, origin_file, transformation_file).
    """
    if not source_path.is_file():
        raise ArtifactError(f"Normalized source archive does not exist: {source_path}")

    carrier_root = (
        source_path.parent.parent
        if source_path.parent.name in {"downloads", "output-root"}
        else source_path.parent
    )
    origin_file = carrier_root / "source-origin.json"
    transformation_file = carrier_root / "sdist-transformation.json"

    if not origin_file.is_file():
        raise ArtifactError(
            "source-origin.json is required beside the normalized sdist"
        )
    if not transformation_file.is_file():
        raise ArtifactError(
            "sdist-transformation.json is required beside the normalized sdist"
        )

    return carrier_root, source_path, origin_file, transformation_file


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    """Load one metadata record as a JSON object with a path-specific error."""
    try:
        content = path.read_text(encoding="utf-8")
        return json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceVerificationError(
            f"source provenance carrier contains invalid JSON in {label}: {exc}"
        ) from exc


def verify_relative_artifact_path(
    root: Path, relative_path_str: str, label: str
) -> Path:
    """Reject absolute, traversal, and out-of-root paths before any archive is read."""
    if not relative_path_str or not isinstance(relative_path_str, str):
        raise ArtifactError(f"{label} path is missing or empty")

    p = Path(relative_path_str)
    if p.is_absolute():
        raise ArtifactError(f"{label} path must be relative: {relative_path_str}")

    resolved = (root / p).resolve()
    resolved_root = root.resolve()
    if not (resolved == resolved_root or resolved.is_relative_to(resolved_root)):
        raise ArtifactError(
            f"Dangerous path traversal detected in {label}: {relative_path_str}"
        )

    return root / p


def verify_sha256(path: Path, expected: str, label: str) -> str:
    """Hash one regular file and compare it to expected digest."""
    if not path.is_file():
        raise ProvenanceVerificationError(f"File for {label} does not exist: {path}")
    if not expected or not isinstance(expected, str):
        raise ProvenanceVerificationError(f"Expected SHA-256 for {label} is missing")

    actual = compute_sha256(path)
    if actual.lower() != expected.lower():
        if "pep691" in path.name or "pep740" in path.name:
            raise ProvenanceVerificationError(
                f"{path.name} does not match source-origin provenance digest"
            )
        raise ProvenanceVerificationError(
            f"{label} digest mismatch: expected {expected.lower()}, "
            f"got {actual.lower()}"
        )
    return actual.lower()


def normalize_source_route(registry: str) -> SourceRoute:
    """
    Normalize registry string into SourceRoute enum.
    rhtl and packages.redhat.com normalize to SourceRoute.RHTL.
    pypi.org normalizes to SourceRoute.PYPI.
    """
    reg_clean = (registry or "").strip().lower()
    if reg_clean in {"rhtl", "packages.redhat.com"}:
        return SourceRoute.RHTL
    if reg_clean == "pypi.org":
        return SourceRoute.PYPI
    raise ProvenanceVerificationError(f"Unsupported source registry '{registry}'")


def adapt_rhtl_pep740(raw_path: Path, output_path: Path) -> Path:
    """Adapt an RHTL PEP 740 envelope without decoding its signed values."""
    try:
        document = json.loads(raw_path.read_bytes())
        if not isinstance(document, dict):
            raise ValueError("RHTL PEP 740 evidence is malformed")
        bundles = document.get("attestation_bundles")
        if not isinstance(bundles, list) or len(bundles) != 1:
            raise ValueError(
                "RHTL PEP 740 evidence must contain exactly one attestation bundle"
            )
        if not isinstance(bundles[0], dict):
            raise ValueError("RHTL PEP 740 evidence is malformed")
        attestations = bundles[0].get("attestations")
        if not isinstance(attestations, list) or len(attestations) != 1:
            raise ValueError(
                "RHTL PEP 740 evidence must contain exactly one attestation"
            )
        if not isinstance(attestations[0], dict):
            raise ValueError("RHTL PEP 740 evidence is malformed")
        envelope = attestations[0].get("envelope")
        if not isinstance(envelope, dict):
            raise ValueError("RHTL PEP 740 evidence is malformed")
        payload = envelope.get("statement")
        signature = envelope.get("signature")
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ) as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise ValueError("RHTL PEP 740 evidence is malformed") from exc
        if isinstance(exc, ValueError):
            raise
        raise ValueError("RHTL PEP 740 evidence is malformed") from exc
    if (
        not isinstance(payload, str)
        or not isinstance(signature, str)
        or not payload
        or not signature
    ):
        raise ValueError("RHTL PEP 740 envelope has invalid payload or signature")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "payloadType": "application/vnd.in-toto+json",
                "payload": payload,
                "signatures": [{"sig": signature}],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return output_path


def extract_pep740_predicate_type(raw_path: Path) -> str | None:
    """Extract the in-toto predicateType from an RHTL PEP 740 attestation statement."""
    try:
        document = json.loads(raw_path.read_bytes())
        bundles = document.get("attestation_bundles", [])
        attestations = bundles[0].get("attestations", [])
        envelope = attestations[0].get("envelope", {})
        payload = envelope.get("statement")
        if not isinstance(payload, str):
            return None
        stmt = json.loads(base64.b64decode(payload))
        if isinstance(stmt, dict) and isinstance(stmt.get("predicateType"), str):
            return stmt["predicateType"]
    except Exception:
        return None
    return None


def _run_cosign_verify(
    source_file: Path,
    attestation_file: Path,
    public_key: str,
    predicate_type: str,
    sig_flag: str,
) -> None:
    """Run cosign verify-blob-attestation, failing closed before any publication.

    ``sig_flag`` already encodes the attestation path as ``--signature=<path>``
    (bare DSSE envelope) or ``--bundle=<path>`` (sigstore bundle), so it is the
    only reference to ``attestation_file`` passed to cosign.
    """
    key = (public_key or "").strip()
    if not key:
        raise ValueError(
            "public verification key is required for attestation verification"
        )
    if (
        not key.startswith(
            ("awskms://", "k8s://", "gcpkms://", "azurekms://", "vault://")
        )
        and not Path(key).is_file()
    ):
        raise ValueError(f"Verification key '{key}' does not exist or is not a file")
    cosign_bin = shutil.which("cosign")
    if not cosign_bin:
        raise RuntimeError("Cosign CLI is required for attestation verification")
    result = subprocess.run(
        [
            cosign_bin,
            "verify-blob-attestation",
            "--insecure-ignore-tlog",
            "--type",
            predicate_type,
            "--key",
            key,
            sig_flag,
            str(source_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"cosign verify-blob-attestation failed (exit {result.returncode}): "
            f"{result.stderr}"
            f"\n--- stdout ---\n{result.stdout}"
        )


def verify_blob_attestation(
    source_file: Path,
    signature_file: Path,
    public_key: str,
    predicate_type: str = "https://slsa.dev/provenance/v1",
) -> None:
    """Verify a bare DSSE envelope produced by adapt_rhtl_pep740 (RHTL path)."""
    _run_cosign_verify(
        source_file,
        signature_file,
        public_key,
        predicate_type,
        f"--signature={signature_file}",
    )


def verify_bundle_attestation(
    source_file: Path,
    bundle_file: Path,
    public_key: str,
    predicate_type: str = "https://slsa.dev/provenance/v1",
) -> None:
    """Verify a sigstore bundle produced by cosign attest-blob --bundle
    (self-verify)."""
    _run_cosign_verify(
        source_file,
        bundle_file,
        public_key,
        predicate_type,
        f"--bundle={bundle_file}",
    )


def verify_acquired_source(
    origin: dict[str, Any],
    carrier_root: Path,
    expected_package: str,
    expected_version: str,
) -> tuple[AcquiredSourceArtifact, str, str]:
    """
    Validate origin-record fields, safe acquired path, and acquired archive digest.
    Returns (AcquiredSourceArtifact, registry, acquired_sha256).
    """
    canonical = canonicalize_name(expected_package)
    acquired = origin.get("acquired", {})

    acquired_digest = acquired.get("sha256")
    acquired_path_str = acquired.get("path") or acquired.get("filename")
    registry = acquired.get("registry", "pypi.org")

    if not isinstance(acquired_digest, str) or not acquired_digest:
        raise ProvenanceVerificationError("source-origin.json lacks acquired digest")

    default_archive_name = f"{canonical}-{expected_version}.tar.gz"
    archive_path = carrier_root / (
        acquired_path_str or f"downloads/{default_archive_name}"
    )

    if archive_path.parent != carrier_root / "downloads" or not archive_path.is_file():
        raise ProvenanceVerificationError(
            "source provenance carrier is missing downloads/original sdist"
        )

    if acquired_path_str and Path(acquired_path_str).name != archive_path.name:
        raise ProvenanceVerificationError(
            "source-origin acquired path does not match carried original archive"
        )

    verify_sha256(archive_path, acquired_digest, "original archive")

    rhtl_index_file = carrier_root / "rhtl-index.pep691.json"
    pep740_file = carrier_root / "provenance.pep740.json"

    acquired_artifact = AcquiredSourceArtifact(
        root=carrier_root,
        sdist=archive_path,
        source_origin=carrier_root / "source-origin.json",
        rhtl_index=rhtl_index_file if rhtl_index_file.is_file() else None,
        pep740_provenance=pep740_file if pep740_file.is_file() else None,
    )
    return acquired_artifact, registry, acquired_digest.lower()


def verify_transformation(
    transformation: dict[str, Any],
    acquired: AcquiredSourceArtifact,
    source_path: Path,
    carrier_root: Path,
    origin_file: Path,
) -> tuple[NormalizedSourceArtifact, str]:
    """
    Validate transformation input/output/source-origin digests and
    normalized-output basename.
    Returns (NormalizedSourceArtifact, normalized_sha256).
    """
    output = transformation.get("output") or {}
    transform_input = transformation.get("input") or {}
    output_digest = output.get("sha256") or transformation.get("output_sha256")
    transform_input_digest = transform_input.get("sha256") or transformation.get(
        "input_sha256"
    )
    origin_digest = transformation.get("source_origin_sha256") or (
        transformation.get("source_origin", {}).get("sha256")
        if isinstance(transformation.get("source_origin"), dict)
        else None
    )
    output_path_str = output.get("path") or transformation.get("output_path")

    if not all(
        isinstance(value, str) and value
        for value in (output_digest, transform_input_digest, origin_digest)
    ):
        raise ProvenanceVerificationError(
            "sdist-transformation.json must contain input, output, and "
            "source-origin digests"
        )

    acquired_digest = compute_sha256(acquired.sdist)
    if transform_input_digest.lower() != acquired_digest.lower():
        raise ProvenanceVerificationError(
            "sdist transformation input does not match acquired digest"
        )

    source_sha256 = verify_sha256(source_path, output_digest, "normalized sdist")

    if output_path_str and Path(output_path_str).name != source_path.name:
        raise ProvenanceVerificationError(
            "sdist transformation output path does not match normalized sdist"
        )

    verify_sha256(origin_file, origin_digest, "source-origin.json")

    normalized_artifact = NormalizedSourceArtifact(
        root=carrier_root,
        acquired=acquired,
        sdist=source_path,
        transformation=carrier_root / "sdist-transformation.json",
    )
    return normalized_artifact, source_sha256


def verify_upstream_evidence(
    origin: dict[str, Any],
    carrier_root: Path,
    route: SourceRoute,
) -> tuple[SourceRoute, tuple[Path, ...]]:
    """
    Enforce PyPI/RHTL evidence-state matrix, hash only applicable opaque evidence
    file(s).
    Returns (route, tuple_of_evidence_files_to_publish).
    Never parses PEP 740 contents.
    """
    provenance = origin.get("provenance", {})

    provenance_mode = provenance.get("mode")
    if provenance_mode not in {"pypi", "rhtl", "pypi-slsa-v1", "rhtl-pep740"}:
        raise ProvenanceVerificationError(
            "source-origin provenance mode is missing or unsupported"
        )

    evidence_files: list[Path] = [
        carrier_root / "source-origin.json",
        carrier_root / "sdist-transformation.json",
    ]

    if route == SourceRoute.RHTL:
        status = provenance.get("status")
        is_advertised = provenance.get("advertised")
        pep740_digest = provenance.get("sha256")
        rhtl_evidence = provenance.get("rhtl", {}).get("evidence", {})
        pep691_digest = rhtl_evidence.get("sha256")

        if is_advertised or status == "advertised":
            pep740_file = carrier_root / "provenance.pep740.json"
            if not pep740_file.is_file():
                raise ProvenanceVerificationError(
                    "Advertised RHTL provenance file is missing: provenance.pep740.json"
                )
            if pep740_digest:
                verify_sha256(pep740_file, pep740_digest, "provenance.pep740.json")
            evidence_files.append(pep740_file)

        pep691_file = carrier_root / "rhtl-index.pep691.json"
        if pep691_file.is_file():
            if pep691_digest:
                verify_sha256(pep691_file, pep691_digest, "rhtl-index.pep691.json")
            if not is_advertised and status != "advertised":
                evidence_files.append(pep691_file)
        elif not is_advertised and status != "advertised":
            raise ProvenanceVerificationError(
                "Unadvertised RHTL route requires rhtl-index.pep691.json evidence"
            )

    return route, tuple(evidence_files)


def copy_verified_evidence(
    evidence_files: tuple[Path, ...], destination_dir: Path
) -> None:
    """Copy only the returned, already verified files; does not rediscover
    optional files."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    for src in evidence_files:
        if not src.is_file():
            raise ProvenanceVerificationError(
                f"Verified evidence file does not exist to copy: {src}"
            )
        dest = destination_dir / src.name
        shutil.copyfile(src, dest)


def verify_lightwell_attestations(
    lightwell_dir: Path,
    *,
    route: SourceRoute,
    signed: bool,
) -> None:
    """
    Separately verify only Lightwell-generated Cosign DSSE outputs after signing.
    Ensures .lightwell/ contains expected DSSE files and no extraneous files.
    All DSSE files use .dsse.json extension.
    """
    if not signed:
        return

    meta_dsse = lightwell_dir / "metadata.dsse.json"
    if not meta_dsse.is_file():
        raise ProvenanceVerificationError(
            "Signed publication missing .lightwell/metadata.dsse.json"
        )

    prov_dsse = lightwell_dir / "provenance.dsse.json"
    if route == SourceRoute.PYPI:
        if not prov_dsse.is_file():
            raise ProvenanceVerificationError(
                "Signed PyPI publication missing .lightwell/provenance.dsse.json"
            )


def verify_normalized_source_artifact(
    source_path: Path,
    *,
    package: str,
    version: str,
) -> VerifiedSourceArtifact:
    """
    Thin composition of single-purpose verification steps.
    Validates normalized source carrier against origin record and transformation record.
    """
    carrier_root, sdist_path, origin_file, transformation_file = (
        locate_source_artifact_paths(source_path, package, version)
    )
    origin = load_json_object(origin_file, "source origin")
    transformation = load_json_object(transformation_file, "transformation")

    acquired, raw_registry, acquired_sha256 = verify_acquired_source(
        origin, carrier_root, package, version
    )
    route = normalize_source_route(raw_registry)

    normalized, normalized_sha256 = verify_transformation(
        transformation, acquired, sdist_path, carrier_root, origin_file
    )

    _, evidence_files = verify_upstream_evidence(origin, carrier_root, route)

    return VerifiedSourceArtifact(
        package=package,
        version=version,
        route=route,
        registry=raw_registry,
        normalized_sha256=normalized_sha256,
        acquired_sha256=acquired_sha256,
        artifact=normalized,
        evidence_files=evidence_files,
    )
