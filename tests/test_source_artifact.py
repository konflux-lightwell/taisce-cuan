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

from pathlib import Path
import pytest

from taisce_cuan.source import (
    AcquiredSourceArtifact,
    ArtifactError,
    NormalizedSourceArtifact,
)


def test_acquired_source_artifact_immutability(tmp_path: Path):
    root = tmp_path
    sdist = root / "downloads" / "pkg-1.0.0.tar.gz"
    source_origin = root / "source-origin.json"
    rhtl_index = root / "rhtl-index.pep691.json"
    pep740 = root / "provenance.pep740.json"

    acquired = AcquiredSourceArtifact(
        root=root,
        sdist=sdist,
        source_origin=source_origin,
        rhtl_index=rhtl_index,
        pep740_provenance=pep740,
    )

    assert acquired.root == root
    assert acquired.sdist == sdist
    assert acquired.source_origin == source_origin
    assert acquired.rhtl_index == rhtl_index
    assert acquired.pep740_provenance == pep740

    # Frozen dataclass: cannot mutate fields
    with pytest.raises(AttributeError):
        acquired.sdist = root / "other.tar.gz"  # type: ignore[misc]


def test_normalized_source_artifact_immutability(tmp_path: Path):
    root = tmp_path
    acquired_sdist = root / "downloads" / "pkg-1.0.0.tar.gz"
    normalized_sdist = root / "pkg-1.0.0.tar.gz"
    transformation = root / "sdist-transformation.json"

    acquired = AcquiredSourceArtifact(
        root=root,
        sdist=acquired_sdist,
        source_origin=root / "source-origin.json",
        rhtl_index=None,
        pep740_provenance=None,
    )

    normalized = NormalizedSourceArtifact(
        root=root,
        acquired=acquired,
        sdist=normalized_sdist,
        transformation=transformation,
    )

    assert normalized.root == root
    assert normalized.acquired == acquired
    assert normalized.sdist == normalized_sdist
    assert normalized.transformation == transformation

    with pytest.raises(AttributeError):
        normalized.sdist = root / "other.tar.gz"  # type: ignore[misc]


def test_artifact_error_inheritance():
    err = ArtifactError("invalid artifact structure")
    assert isinstance(err, ValueError)
    assert str(err) == "invalid artifact structure"
