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
from typing import Optional

import httpx

from taisce_cuan.source.fetch import (
    MAX_DOWNLOAD_BYTES,
    PYPI_API_DEFAULT,
    RHTL_SIMPLE_DEFAULT,
    SUPPORTED_REGISTRIES,
    SdistSourceFetcher,
    SdistSourceInfo,
)

# Backwards-compatibility aliases
SdistInfo = SdistSourceInfo
SdistSourceInfo = SdistSourceInfo


class SdistFetcher(SdistSourceFetcher):
    """Compatibility facade over SdistSourceFetcher."""

    def __init__(
        self,
        rhtl_simple_url: str = RHTL_SIMPLE_DEFAULT,
        pypi_api_url: str = PYPI_API_DEFAULT,
        client: Optional[httpx.Client] = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
    ):
        super().__init__(
            rhtl_simple_url=rhtl_simple_url,
            pypi_api_url=pypi_api_url,
            client=client,
            max_download_bytes=max_download_bytes,
        )

    def _download_exact(self, url: str, destination: Path, expected_sha: str, expected_size: int = 0) -> None:
        return self.download_exact(url, destination, expected_sha, expected_size)


__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "PYPI_API_DEFAULT",
    "RHTL_SIMPLE_DEFAULT",
    "SUPPORTED_REGISTRIES",
    "SdistFetcher",
    "SdistInfo",
    "SdistSourceInfo",
]
