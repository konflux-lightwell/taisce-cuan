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
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from taisce_cuan.models import (
    BuildDefinition,
    Builder,
    ExternalParameters,
    FromagerInfo,
    IngestionMetadata,
    LightwellBuildsInfo,
    Predicate,
    ResolvedDependency,
    RunDetails,
    RunDetailsMetadata,
    Subject,
)
from taisce_cuan.provenance.verify import (
    ProvenanceVerificationError,
    SourceRoute,
    VerifiedSourceArtifact,
    verify_bundle_attestation,
)
from taisce_cuan.sdist import canonicalize_name, compute_sha256

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishSourceRequest:
    """Typed input for source mirror publication."""

    source_path: Path
    package: str
    version: str
    workspace_dir: Path
    source_registry: str = "pypi.org"
    sign_key: str | None = None
    provenance_path: Path | None = None
    public_key: str | None = None
    rhtl_predicate_type: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class PublishSourceResult:
    """Typed result of source mirror publication."""

    tag_name: str
    repo_name: str
    target_branch: str
    normalized_sha256: str
    route: SourceRoute
    disposition: Literal["published", "already-present"]


def build_ingestion_metadata(
    verified: VerifiedSourceArtifact,
    repo_name: str,
    *,
    repo_dir: Path | None = None,
    sign_key: str | None = None,
    dry_run: bool = False,
    timestamp: datetime.datetime | None = None,
) -> IngestionMetadata:
    """
    Build IngestionMetadata from a verified source artifact.
    Requires exactly one normalized-sdist SHA-256 subject.
    Never emits a Git tree/commit/tag subject.
    Binds the complete local mirror closure members in resolvedDependencies.
    """
    canonical = canonicalize_name(verified.package)
    now = timestamp or datetime.datetime.now(datetime.UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Sole published subject is the normalized sdist archive
    subject_filename = f"{canonical}-{verified.version}.tar.gz"
    subjects = [
        Subject(
            name=subject_filename,
            digest={"sha256": verified.normalized_sha256},
        )
    ]

    active_repo_dir = repo_dir or (Path("/tmp") / repo_name)
    lightwell_dir = active_repo_dir / ".lightwell"
    final_downloads = lightwell_dir / "downloads"
    original_archive = final_downloads / verified.artifact.acquired.sdist.name
    normalized_archive = lightwell_dir / verified.artifact.sdist.name

    resolved_deps: list[ResolvedDependency] = []

    def bind(
        path: Path, role: str, name: str | None = None, **annotations: Any
    ) -> None:
        if not path.exists():
            return
        relative = (
            path.relative_to(repo_dir).as_posix()
            if path.is_relative_to(repo_dir)
            else path.name
        )
        resolved_deps.append(
            ResolvedDependency(
                name=name or relative,
                uri=f"./{relative}",
                digest={"sha256": compute_sha256(path)},
                annotations={"role": role, **annotations},
            )
        )

    bind(lightwell_dir / "source-origin.json", "lightwell-source-origin")
    bind(lightwell_dir / "sdist-transformation.json", "lightwell-sdist-transformation")
    bind(original_archive, "upstream-acquired-sdist", registry=verified.registry)
    bind(normalized_archive, "lightwell-normalized-sdist")

    if verified.route == SourceRoute.RHTL:
        raw_pep740 = lightwell_dir / "provenance.pep740.json"
        adapted_dsse = lightwell_dir / "provenance.dsse.json"
        index_file = lightwell_dir / "rhtl-index.pep691.json"

        if raw_pep740.is_file():
            bind(raw_pep740, "upstream-rhtl-pep740")
            bind(adapted_dsse, "adapted-rhtl-dsse")
        elif index_file.is_file():
            bind(index_file, "upstream-rhtl-pep691")

    attestation_level = "signed" if sign_key else "unsigned-inventory"
    if sign_key:
        note = "Signed Lightwell metadata attestation."
    elif dry_run:
        note = (
            "Unsigned SLSA Build Provenance inventory (dry-run; no release fallback)."
        )
    else:
        note = "Unsigned SLSA Build Provenance inventory."

    return IngestionMetadata(
        subject=subjects,
        predicate=Predicate(
            buildDefinition=BuildDefinition(
                externalParameters=ExternalParameters(
                    package=verified.package,
                    canonical_name=canonical,
                    version=verified.version,
                    upstream_registry=verified.registry,
                ),
                resolvedDependencies=resolved_deps,
            ),
            runDetails=RunDetails(
                builder=Builder(),
                metadata=RunDetailsMetadata(
                    startedOn=now_str,
                    finishedOn=now_str,
                    attestation_level=attestation_level,
                    note=note,
                    lightwell_builds=LightwellBuildsInfo(
                        repo=repo_name,
                        source_registry_used=verified.registry,
                    ),
                    fromager=FromagerInfo(),
                ),
            ),
        ),
    )


class CosignAttestationSigner:
    """Concrete Cosign attestation signer.

    Validates key form, runs attest-blob, fails closed.
    """

    def __init__(self, cosign_bin: str | None = None):
        self.cosign_bin = cosign_bin or shutil.which("cosign")

    def _validate_key(self, sign_key: str) -> None:
        key_str = sign_key.strip()
        if not key_str:
            raise ProvenanceVerificationError("sign_key is empty")

        is_kms = any(
            key_str.startswith(prefix)
            for prefix in [
                "awskms://",
                "k8s://",
                "gcpkms://",
                "azurekms://",
                "vault://",
            ]
        )
        if not is_kms:
            key_path = Path(key_str)
            if not key_path.exists() or not key_path.is_file():
                raise ValueError(
                    f"Signing key file '{key_str}' does not exist or is not a file."
                )

    def sign(
        self,
        metadata: IngestionMetadata,
        source_file: Path,
        sign_key: str | None,
        output_provenance_file: Path,
    ) -> Path | None:
        key_str = (sign_key or "").strip()
        if not key_str:
            logger.debug("No sign_key provided to CosignAttestationSigner; skipping")
            return None

        self._validate_key(key_str)

        if not self.cosign_bin or not shutil.which(self.cosign_bin):
            raise RuntimeError(
                "Signing key provided but 'cosign' CLI binary is not installed in PATH."
            )

        output_provenance_file.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w", suffix="-predicate.json", delete=False
        ) as pred_tmp:
            pred_tmp.write(
                metadata.predicate.model_dump_json(by_alias=True, exclude_none=True)
            )
            pred_tmp_path = pred_tmp.name

        try:
            cmd = [
                self.cosign_bin,
                "attest-blob",
                str(source_file),
                f"--predicate={pred_tmp_path}",
                "--type=https://slsa.dev/provenance/v1",
                f"--key={key_str}",
                "--yes",
                f"--bundle={output_provenance_file}",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)

            if res.returncode != 0 or not output_provenance_file.exists():
                raise RuntimeError(
                    f"cosign attest-blob failed (exit {res.returncode}): {res.stderr}"
                )

            logger.info(
                f"Successfully created signed attestation ({output_provenance_file})"
            )
            return output_provenance_file
        finally:
            if os.path.exists(pred_tmp_path):
                os.unlink(pred_tmp_path)


def attest_source_mirror(
    verified: VerifiedSourceArtifact,
    repo_dir: Path,
    repo_name: str,
    *,
    sign_key: str | None = None,
    public_key: str | None = None,
    signer: CosignAttestationSigner | None = None,
    dry_run: bool = False,
    timestamp: datetime.datetime | None = None,
) -> IngestionMetadata:
    """
    Build metadata, write .lightwell/metadata.json, and sign required outputs
    according to route.
    PyPI: signs metadata.dsse.json and provenance.dsse.json
    RHTL: signs metadata.dsse.json only (provenance.dsse.json is adapted from raw
    PEP 740)
    All signed envelopes use the .dsse.json extension.
    Legacy .dsse files are unlinked.
    """
    lightwell_dir = (
        repo_dir if repo_dir.name == ".lightwell" else (repo_dir / ".lightwell")
    )
    lightwell_dir.mkdir(parents=True, exist_ok=True)

    metadata = build_ingestion_metadata(
        verified,
        repo_name=repo_name,
        repo_dir=repo_dir,
        sign_key=sign_key,
        dry_run=dry_run,
        timestamp=timestamp,
    )

    metadata_file = lightwell_dir / "metadata.json"
    with open(metadata_file, "w") as f:
        f.write(metadata.model_dump_json(by_alias=True, exclude_none=True, indent=2))

    # Unlink legacy unextended files
    (lightwell_dir / "metadata.dsse").unlink(missing_ok=True)
    (lightwell_dir / "provenance.dsse").unlink(missing_ok=True)

    if sign_key:
        active_signer = signer or CosignAttestationSigner()
        metadata_attestation = active_signer.sign(
            metadata=metadata,
            source_file=metadata_file,
            sign_key=sign_key,
            output_provenance_file=lightwell_dir / "metadata.dsse.json",
        )
        if metadata_attestation is None:
            raise RuntimeError(
                "metadata signing is required when a signing key is configured"
            )

        # Self-verify the newly signed metadata attestation if a public
        # verification key is available
        verification_key = None
        if public_key:
            verification_key = public_key
        elif sign_key.startswith(
            ("awskms://", "k8s://", "gcpkms://", "azurekms://", "vault://")
        ):
            verification_key = sign_key
        else:
            candidate_pub = Path(sign_key).with_suffix(".pub")
            if candidate_pub.is_file():
                verification_key = str(candidate_pub)
            elif Path("/etc/signing-secret/cosign.pub").is_file():
                verification_key = "/etc/signing-secret/cosign.pub"
            elif Path("/etc/signing-secret/public.pem").is_file():
                verification_key = "/etc/signing-secret/public.pem"

        if verification_key:
            verify_bundle_attestation(
                metadata_file,
                metadata_attestation,
                verification_key,
                "https://slsa.dev/provenance/v1",
            )

        if verified.route == SourceRoute.PYPI:
            active_signer.sign(
                metadata=metadata,
                source_file=verified.artifact.sdist,
                sign_key=sign_key,
                output_provenance_file=lightwell_dir / "provenance.dsse.json",
            )
    else:
        (lightwell_dir / "metadata.dsse.json").unlink(missing_ok=True)
        if verified.route == SourceRoute.PYPI:
            (lightwell_dir / "provenance.dsse.json").unlink(missing_ok=True)

    return metadata
