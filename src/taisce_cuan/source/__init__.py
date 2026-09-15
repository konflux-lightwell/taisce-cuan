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

from typing import Any

from taisce_cuan.source.artifact import (
    AcquiredSourceArtifact,
    ArtifactError,
    NormalizedSourceArtifact,
)
from taisce_cuan.source.fetch import (
    MAX_DOWNLOAD_BYTES,
    PYPI_API_DEFAULT,
    RHTL_SIMPLE_DEFAULT,
    SUPPORTED_REGISTRIES,
    SdistSourceFetcher,
    SdistSourceInfo,
)

__all__ = [
    "AcquiredSourceArtifact",
    "ArtifactError",
    "GitMirrorPublisher",
    "MAX_DOWNLOAD_BYTES",
    "NormalizedSourceArtifact",
    "PYPI_API_DEFAULT",
    "RHTL_SIMPLE_DEFAULT",
    "SUPPORTED_REGISTRIES",
    "SdistSourceFetcher",
    "SdistSourceInfo",
    "_is_rhtl_registry",
    "parse_version_safe",
]


def __getattr__(name: str) -> Any:
    # ``mirror`` is exported lazily to break an import cycle. Eagerly importing
    # it here would run ``source.mirror`` -> ``provenance.attest``/``verify``,
    # and ``provenance.verify`` imports back from ``source.artifact`` -- which
    # re-enters this partially-initialized package and fails. Deferring the
    # import until an attribute is actually accessed lets ``provenance`` finish
    # loading first. (``artifact`` and ``fetch`` are cycle-free, so they stay
    # eager above.)
    if name in ("GitMirrorPublisher", "_is_rhtl_registry", "parse_version_safe"):
        from taisce_cuan.source import mirror

        return getattr(mirror, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
