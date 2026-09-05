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

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from taisce_cuan.sdist import canonicalize_name, compute_sha256

logger = logging.getLogger(__name__)

RHTL_SIMPLE_DEFAULT = "https://packages.redhat.com/api/pypi/public-trusted-libraries/main/simple"
PYPI_API_DEFAULT = "https://pypi.org/pypi"


@dataclass(frozen=True)
class SdistSourceInfo:
    registry: str
    download_url: str
    sha256: str
    size: int
    upload_time: Optional[str]
    provenance_url: Optional[str] = None


class SdistFetcher:
    """Resolves and downloads sdists from RHTL and PyPI."""

    def __init__(
        self,
        rhtl_simple_url: str = RHTL_SIMPLE_DEFAULT,
        pypi_api_url: str = PYPI_API_DEFAULT,
        client: Optional[httpx.Client] = None,
    ):
        self.rhtl_simple_url = rhtl_simple_url.rstrip("/")
        self.pypi_api_url = pypi_api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)

    def query_rhtl(self, package: str, version: str) -> Optional[SdistSourceInfo]:
        """Query RHTL PEP 691 Simple JSON index for the package version."""
        canonical = canonicalize_name(package)
        url = f"{self.rhtl_simple_url}/{canonical}/"
        headers = {"Accept": "application/vnd.pypi.simple.v1+json"}

        try:
            resp = self._client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            pattern = re.compile(
                rf"^{re.escape(canonical).replace('-', '[-_.]')}-{re.escape(version)}\.tar\.gz$",
                re.IGNORECASE,
            )
            for file_entry in data.get("files", []):
                fn = file_entry.get("filename", "")
                if pattern.match(fn):
                    return SdistSourceInfo(
                        registry="rhtl",
                        download_url=file_entry["url"],
                        sha256=file_entry.get("hashes", {}).get("sha256", ""),
                        size=file_entry.get("size", 0),
                        upload_time=file_entry.get("upload-time"),
                        provenance_url=file_entry.get("provenance"),
                    )
        except Exception as e:
            logger.warning(f"Error querying RHTL for {package} {version}: {e}")
        return None

    def query_pypi(self, package: str, version: str) -> Optional[SdistSourceInfo]:
        """Query PyPI JSON API for the package version."""
        url = f"{self.pypi_api_url}/{package}/{version}/json"
        try:
            resp = self._client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            for u in data.get("urls", []):
                if u.get("filename", "").endswith(".tar.gz"):
                    return SdistSourceInfo(
                        registry="pypi.org",
                        download_url=u["url"],
                        sha256=u.get("digests", {}).get("sha256", ""),
                        size=u.get("size", 0),
                        upload_time=u.get("upload_time"),
                        provenance_url=None,
                    )
        except Exception as e:
            logger.warning(f"Error querying PyPI for {package} {version}: {e}")
        return None

    def fetch(
        self,
        package: str,
        version: str,
        output_dir: Path,
        registries: Optional[list[str] | str] = None,
        rhtl_only: bool = False,
    ) -> tuple[Path, SdistSourceInfo, Optional[SdistSourceInfo]]:
        """Download sdist, verify sha256, and return downloaded file path + metadata."""
        output_dir.mkdir(parents=True, exist_ok=True)
        canonical = canonicalize_name(package)

        # Parse requested registries list
        if isinstance(registries, str):
            req_regs = [r.strip().lower() for r in registries.split(",") if r.strip()]
        elif registries is not None:
            req_regs = [r.strip().lower() for r in registries if r.strip()]
        elif rhtl_only:
            req_regs = ["rhtl"]
        else:
            req_regs = ["rhtl", "pypi.org"]

        target_info: Optional[SdistSourceInfo] = None
        rhtl_info: Optional[SdistSourceInfo] = None
        pypi_info: Optional[SdistSourceInfo] = None

        for reg in req_regs:
            if reg in ("rhtl", "packages.redhat.com"):
                if rhtl_info is None:
                    rhtl_info = self.query_rhtl(package, version)
                if rhtl_info:
                    target_info = rhtl_info
                    break
            elif reg in ("pypi", "pypi.org", "pypi.python.org"):
                if pypi_info is None:
                    pypi_info = self.query_pypi(package, version)
                if pypi_info:
                    target_info = pypi_info
                    break

        if not target_info:
            raise RuntimeError(
                f"Package {package} {version} could not be resolved from requested registries: {req_regs}"
            )

        if not target_info.sha256:
            raise ValueError(
                f"No SHA-256 digest provided by registry '{target_info.registry}' for {canonical}-{version}.tar.gz"
            )

        dest_file = output_dir / f"{canonical}-{version}.tar.gz"
        logger.info(f"Downloading {package} {version} from {target_info.registry}: {target_info.download_url}")

        with self._client.stream("GET", target_info.download_url) as response:
            response.raise_for_status()
            with open(dest_file, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)

        actual_sha256 = compute_sha256(dest_file)
        if actual_sha256 != target_info.sha256:
            dest_file.unlink(missing_ok=True)
            raise ValueError(
                f"SHA-256 mismatch for {dest_file.name}: expected {target_info.sha256}, got {actual_sha256}"
            )

        logger.info(f"Verified {dest_file.name} (sha256: {actual_sha256})")
        return dest_file, target_info, pypi_info
