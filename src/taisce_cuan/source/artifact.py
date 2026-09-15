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

from dataclasses import dataclass
from pathlib import Path


class ArtifactError(ValueError):
    """Raised when source artifact paths, structure, or required files are invalid."""


@dataclass(frozen=True)
class AcquiredSourceArtifact:
    """Carried paths for an acquired source distribution and its raw origin evidence."""

    root: Path
    sdist: Path
    source_origin: Path
    rhtl_index: Path | None
    pep740_provenance: Path | None


@dataclass(frozen=True)
class NormalizedSourceArtifact:
    """Carried paths for a normalized source distribution and its transformation record."""

    root: Path
    acquired: AcquiredSourceArtifact
    sdist: Path
    transformation: Path
