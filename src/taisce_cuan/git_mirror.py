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

from pathlib import Path
from typing import Optional, Tuple

from taisce_cuan.source.mirror import (
    GitMirrorPublisher,
    _is_rhtl_registry,
    parse_version_safe,
)


def resolve_provenance_file(
    source_path: Path,
    provenance_path: Optional[Path] = None,
) -> Optional[Tuple[Path, str]]:
    """Legacy helper for provenance resolution."""
    if provenance_path is not None:
        p = Path(provenance_path)
        if p.is_file():
            return p, f"explicit provenance path ({p.name})"
        raise ValueError(f"Explicit provenance_path specified but file does not exist: {provenance_path}")

    if not source_path or not source_path.parent.is_dir():
        return None

    sdist_dir = source_path.parent
    sdist_prov = sdist_dir / "sdist-provenance.json"
    if sdist_prov.is_file():
        return sdist_prov, f"embedded sdist provenance ({sdist_prov.name})"

    chains_dir = sdist_dir / "chains-provenance"
    if chains_dir.is_dir():
        chains_files = sorted([f for f in chains_dir.glob("*.json") if f.is_file()])
        if chains_files:
            return chains_files[0], f"Chains provenance ({chains_files[0].name})"

    return None


__all__ = [
    "GitMirrorPublisher",
    "_is_rhtl_registry",
    "parse_version_safe",
    "resolve_provenance_file",
]
