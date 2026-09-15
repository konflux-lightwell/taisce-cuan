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

from taisce_cuan.provenance.attest import (
    CosignAttestationSigner,
    PublishSourceRequest,
    PublishSourceResult,
    attest_source_mirror,
    build_ingestion_metadata,
)
from taisce_cuan.provenance.source_origin import (
    ProvenanceOriginError,
    capture_source_origin,
)
from taisce_cuan.provenance.verify import (
    ProvenanceVerificationError,
    SourceRoute,
    VerifiedSourceArtifact,
    adapt_rhtl_pep740,
    copy_verified_evidence,
    extract_pep740_predicate_type,
    load_json_object,
    locate_source_artifact_paths,
    normalize_source_route,
    verify_acquired_source,
    verify_blob_attestation,
    verify_bundle_attestation,
    verify_lightwell_attestations,
    verify_normalized_source_artifact,
    verify_relative_artifact_path,
    verify_sha256,
    verify_transformation,
    verify_upstream_evidence,
)

__all__ = [
    "CosignAttestationSigner",
    "ProvenanceOriginError",
    "ProvenanceVerificationError",
    "PublishSourceRequest",
    "PublishSourceResult",
    "SourceRoute",
    "VerifiedSourceArtifact",
    "adapt_rhtl_pep740",
    "attest_source_mirror",
    "build_ingestion_metadata",
    "capture_source_origin",
    "copy_verified_evidence",
    "extract_pep740_predicate_type",
    "load_json_object",
    "locate_source_artifact_paths",
    "normalize_source_route",
    "verify_acquired_source",
    "verify_blob_attestation",
    "verify_bundle_attestation",
    "verify_lightwell_attestations",
    "verify_normalized_source_artifact",
    "verify_relative_artifact_path",
    "verify_sha256",
    "verify_transformation",
    "verify_upstream_evidence",
]
