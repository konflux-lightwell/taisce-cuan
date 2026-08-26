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

import os
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


def get_builder_id() -> str:
    """Resolve builder ID pinned to commit ref or container image digest if available."""
    # 1. Explicit builder ID / image digest env var (injected in container build or Tekton step)
    builder_id = os.getenv("TAISCE_CUAN_BUILDER_ID")
    if builder_id and builder_id.strip():
        return builder_id.strip()

    # 2. Git commit SHA injected at build time
    commit_sha = os.getenv("GIT_COMMIT_SHA") or os.getenv("GITHUB_SHA") or os.getenv("CI_COMMIT_SHA")
    if commit_sha and commit_sha.strip():
        return f"https://github.com/konflux-lightwell/taisce-cuan@{commit_sha.strip()}"

    # 3. Fallback to package version tag
    from taisce_cuan import __version__
    return f"https://github.com/konflux-lightwell/taisce-cuan@v{__version__}"


class Digest(BaseModel):
    sha256: Optional[str] = None
    gitTree: Optional[str] = None


class Subject(BaseModel):
    name: str
    digest: Digest


class ExternalParameters(BaseModel):
    package: str
    canonical_name: str
    version: str
    upstream_registry: str = "pypi.org"


class ResolvedDependency(BaseModel):
    name: str
    uri: str
    digest: Dict[str, str]
    annotations: Optional[Dict[str, Any]] = None


class BuildDefinition(BaseModel):
    buildType: str = "https://lightwell.dev/buildTypes/python-source-ingest/v1"
    externalParameters: ExternalParameters
    resolvedDependencies: List[ResolvedDependency] = Field(default_factory=list)


class Completeness(BaseModel):
    parameters: bool = True
    environment: bool = False
    materials: bool = True


class RunDetailsMetadata(BaseModel):
    startedOn: str
    finishedOn: str
    completeness: Completeness = Field(default_factory=Completeness)


class Builder(BaseModel):
    id: str = Field(default_factory=get_builder_id)


class RunDetails(BaseModel):
    builder: Builder = Field(default_factory=Builder)
    metadata: RunDetailsMetadata


class Predicate(BaseModel):
    buildDefinition: BuildDefinition
    runDetails: RunDetails


class LightwellBuildsInfo(BaseModel):
    repo: str
    source_registry_used: str


class FromagerInfo(BaseModel):
    build_extra: List[str] = Field(default_factory=list)


class IngestionMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = "1"
    attestation_level: str = "unsigned-inventory"
    note: str = (
        "Unsigned SLSA Build Provenance inventory. "
        "Signed attestation produced by Tekton Chains / Cosign when this ingestion runs as part of Konflux."
    )
    type_: str = Field("https://in-toto.io/Statement/v0.1", alias="_type")
    predicateType: str = "https://slsa.dev/provenance/v1"
    subject: List[Subject]
    predicate: Predicate
    lightwell_builds: LightwellBuildsInfo
    fromager: FromagerInfo = Field(default_factory=FromagerInfo)
