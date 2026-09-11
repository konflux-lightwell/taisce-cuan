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

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from taisce_cuan.sdist import canonicalize_name, compute_sha256
from taisce_cuan.source.artifact import AcquiredSourceArtifact

logger = logging.getLogger(__name__)


class ProvenanceOriginError(ValueError):
    """Raised when source origin evidence cannot be validated or recorded."""


def capture_source_origin(
    root: Path,
    *,
    package: str,
    version: str,
    sdist_path: Path,
    registry: str,
    download_url: str,
    sha256: str,
    size: int,
    provenance_url: Optional[str] = None,
    raw_rhtl_index: Optional[bytes] = None,
    raw_advertised_provenance: Optional[bytes] = None,
    last_rhtl_url: Optional[str] = None,
    last_rhtl_status: Optional[int] = None,
    last_rhtl_reason: Optional[str] = None,
    last_advertised_provenance_status: Optional[int] = None,
    last_advertised_provenance_remote_url: Optional[str] = None,
) -> AcquiredSourceArtifact:
    """Validate evidence state, preserve raw evidence files, and write source-origin.json."""
    root.mkdir(parents=True, exist_ok=True)
    canonical = canonicalize_name(package)

    rhtl_index_path: Optional[Path] = None
    if raw_rhtl_index is not None:
        rhtl_index_path = root / "rhtl-index.pep691.json"
        rhtl_index_path.write_bytes(raw_rhtl_index)

    pep740_path: Optional[Path] = None
    prov_sha256: Optional[str] = None

    is_rhtl = registry in {"rhtl", "packages.redhat.com"}
    mode = "rhtl" if is_rhtl else "pypi"

    if is_rhtl:
        if provenance_url is not None:
            if raw_advertised_provenance is None:
                raise ProvenanceOriginError("Advertised RHTL provenance URL was provided but raw content is missing")
            pep740_path = root / "provenance.pep740.json"
            pep740_path.write_bytes(raw_advertised_provenance)
            prov_sha256 = hashlib.sha256(raw_advertised_provenance).hexdigest()
        else:
            (root / "provenance.pep740.json").unlink(missing_ok=True)

    rel_sdist_path = (
        sdist_path.relative_to(root).as_posix()
        if sdist_path.is_relative_to(root)
        else f"downloads/{canonical}-{version}.tar.gz"
    )

    origin: Dict[str, Any] = {
        "schema": "https://lightwell.dev/schemas/source-origin/v1",
        "acquired": {
            "registry": registry,
            "url": download_url,
            "sha256": sha256,
            "size": size,
            "package": package,
            "version": version,
            "path": rel_sdist_path,
        },
        "provenance": {
            "mode": mode,
        },
    }

    evidence_present = rhtl_index_path is not None and rhtl_index_path.is_file()
    evidence = {
        "path": "rhtl-index.pep691.json" if evidence_present else None,
        "reason": last_rhtl_reason,
        "sha256": compute_sha256(rhtl_index_path) if evidence_present else None,
        "status": last_rhtl_status,
        "url": last_rhtl_url,
    }

    if is_rhtl:
        if provenance_url is not None:
            origin["provenance"].update({
                "advertised": True,
                "http_status": last_advertised_provenance_status,
                "path": "provenance.pep740.json",
                "reference": "provenance.pep740.json",
                "remote_url": last_advertised_provenance_remote_url or provenance_url,
                "sha256": prov_sha256,
                "status": "advertised",
                "url": provenance_url,
            })
            origin["provenance"]["rhtl"] = {"status": "advertised", "evidence": evidence}
        else:
            origin["provenance"].update({
                "advertised": False,
                "path": "rhtl-index.pep691.json",
                "reference": "rhtl-index.pep691.json",
                "sha256": None,
                "status": "not-advertised",
                "url": None,
            })
            origin["provenance"]["rhtl"] = {"status": "not-advertised", "evidence": evidence}
    else:
        origin["provenance"]["rhtl"] = {"status": "not-advertised", "evidence": evidence}

    origin_path = root / "source-origin.json"
    origin_path.write_text(json.dumps(origin, sort_keys=True, separators=(",", ":")) + "\n")

    return AcquiredSourceArtifact(
        root=root,
        sdist=sdist_path,
        source_origin=origin_path,
        rhtl_index=rhtl_index_path if (rhtl_index_path and rhtl_index_path.is_file()) else None,
        pep740_provenance=pep740_path if (pep740_path and pep740_path.is_file()) else None,
    )
