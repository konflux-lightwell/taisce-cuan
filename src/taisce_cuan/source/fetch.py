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
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from taisce_cuan.provenance.source_origin import capture_source_origin
from taisce_cuan.sdist import canonicalize_name, compute_sha256
from taisce_cuan.source.artifact import AcquiredSourceArtifact

logger = logging.getLogger(__name__)

RHTL_SIMPLE_DEFAULT = "https://packages.redhat.com/api/pypi/public-trusted-libraries/main/simple"
PYPI_API_DEFAULT = "https://pypi.org/pypi"
SUPPORTED_REGISTRIES = {"rhtl", "packages.redhat.com", "pypi", "pypi.org", "pypi.python.org"}
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def check_https(url: str) -> None:
    if urlparse(url).scheme.lower() != "https":
        raise ValueError(f"TLS is required for source and provenance URLs: {url}")


@dataclass(frozen=True)
class SdistSourceInfo:
    registry: str
    download_url: str
    sha256: str
    size: int
    upload_time: Optional[str]
    provenance_url: Optional[str] = None


class SdistSourceFetcher:
    """Resolve, download, and preserve raw source distributions and origin evidence."""

    def __init__(
        self,
        rhtl_simple_url: str = RHTL_SIMPLE_DEFAULT,
        pypi_api_url: str = PYPI_API_DEFAULT,
        client: Optional[httpx.Client] = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
    ):
        check_https(rhtl_simple_url)
        check_https(pypi_api_url)
        self.rhtl_simple_url = rhtl_simple_url.rstrip("/")
        self.pypi_api_url = pypi_api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self.max_download_bytes = max_download_bytes
        self._reset_state()

    def _reset_state(self) -> None:
        self.last_rhtl_index: Optional[bytes] = None
        self.last_rhtl_url: Optional[str] = None
        self.last_rhtl_status: Optional[int] = None
        self.last_rhtl_reason: Optional[str] = None
        self.last_advertised_provenance: Optional[bytes] = None
        self.last_advertised_provenance_sha256: Optional[str] = None
        self.last_advertised_provenance_status: Optional[int] = None
        self.last_advertised_provenance_remote_url: Optional[str] = None

    def query_rhtl(self, package: str, version: str) -> Optional[SdistSourceInfo]:
        canonical = canonicalize_name(package)
        url = f"{self.rhtl_simple_url}/{canonical}/"
        self.last_rhtl_url = url
        try:
            response = self._client.get(url, headers={"Accept": "application/vnd.pypi.simple.v1+json"})
            self.last_rhtl_status = response.status_code
            self.last_rhtl_index = response.content
            if response.status_code != 200:
                self.last_rhtl_reason = "unavailable"
                return None
            data = response.json()
            pattern = re.compile(rf"^{re.escape(canonical).replace('-', '[-_.]')}-{re.escape(version)}\.tar\.gz$", re.I)
            for entry in data.get("files", []):
                if pattern.match(entry.get("filename", "")):
                    provenance = entry.get("provenance")
                    if provenance is None:
                        self.last_rhtl_reason = "not-advertised"
                    elif not isinstance(provenance, str) or not provenance.strip():
                        raise ValueError(f"Invalid provenance URL in RHTL index: {provenance!r}")
                    else:
                        check_https(provenance)
                    return SdistSourceInfo(
                        "rhtl",
                        entry["url"],
                        entry.get("hashes", {}).get("sha256", ""),
                        entry.get("size", 0),
                        entry.get("upload-time"),
                        provenance,
                    )
            self.last_rhtl_reason = "not-advertised"
        except ValueError:
            raise
        except Exception as exc:
            self.last_rhtl_reason = "unavailable"
            logger.warning("Error querying RHTL for %s %s: %s", package, version, exc)
        return None

    def query_pypi(self, package: str, version: str) -> Optional[SdistSourceInfo]:
        url = f"{self.pypi_api_url}/{package}/{version}/json"
        try:
            response = self._client.get(url)
            if response.status_code != 200:
                return None
            for entry in response.json().get("urls", []):
                if entry.get("filename", "").endswith(".tar.gz"):
                    return SdistSourceInfo(
                        "pypi.org",
                        entry["url"],
                        entry.get("digests", {}).get("sha256", ""),
                        entry.get("size", 0),
                        entry.get("upload_time"),
                    )
        except Exception as exc:
            logger.warning("Error querying PyPI for %s %s: %s", package, version, exc)
        return None

    def download_exact(self, url: str, destination: Path, expected_sha: str, expected_size: int = 0) -> None:
        check_https(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        Path(temporary).unlink(missing_ok=True)
        try:
            with self._client.stream("GET", url) as response:
                response.raise_for_status()
                if urlparse(str(response.url)).scheme.lower() != "https":
                    raise ValueError("source request redirected away from TLS")
                declared = int(response.headers.get("content-length", "0") or 0)
                limit = expected_size or declared or self.max_download_bytes
                if limit > self.max_download_bytes:
                    raise ValueError("download exceeds configured size bound")
                size = 0
                with open(temporary, "wb") as out:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_download_bytes or (expected_size and size > expected_size):
                            raise ValueError("download exceeded advertised size")
                        out.write(chunk)
                if expected_size and size != expected_size:
                    raise ValueError(f"download size mismatch: expected {expected_size}, got {size}")
            actual = compute_sha256(Path(temporary))
            if actual.lower() != expected_sha.lower():
                raise ValueError(f"SHA-256 mismatch: expected {expected_sha}, got {actual}")
            Path(temporary).replace(destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def fetch(
        self,
        package: str,
        version: str,
        output_dir: Path,
        registries: Optional[list[str] | str] = None,
        rhtl_only: bool = False,
    ) -> tuple[Path, SdistSourceInfo, Optional[SdistSourceInfo]]:
        self._reset_state()

        if isinstance(registries, str):
            req = [x.strip().lower() for x in registries.split(",") if x.strip()]
        elif registries is not None:
            req = [x.strip().lower() for x in registries if x.strip()]
        else:
            req = ["rhtl"] if rhtl_only else ["rhtl", "pypi.org"]
        for reg in req:
            if reg not in SUPPORTED_REGISTRIES:
                raise ValueError(f"Unrecognized registry '{reg}'")

        rhtl = self.query_rhtl(package, version) if any(x in ("rhtl", "packages.redhat.com") for x in req) else None
        pypi = self.query_pypi(package, version) if any(x in ("pypi", "pypi.org", "pypi.python.org") for x in req) else None
        selected = rhtl or pypi
        if selected is None:
            raise RuntimeError(f"Package {package} {version} could not be resolved from {req}")
        if not selected.sha256:
            raise ValueError(f"No SHA-256 digest provided by registry '{selected.registry}'")

        root = output_dir
        root.mkdir(parents=True, exist_ok=True)
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        dest = downloads / f"{canonicalize_name(package)}-{version}.tar.gz"
        self.download_exact(selected.download_url, dest, selected.sha256, selected.size)

        if self.last_rhtl_index is not None:
            (root / "rhtl-index.pep691.json").write_bytes(self.last_rhtl_index)

        raw_advertised: Optional[bytes] = None
        if selected.provenance_url is not None:
            check_https(selected.provenance_url)
            response = self._client.get(selected.provenance_url)
            self.last_advertised_provenance_status = response.status_code
            self.last_advertised_provenance_remote_url = str(response.url)
            response.raise_for_status()
            if urlparse(str(response.url)).scheme.lower() != "https":
                raise ValueError("provenance request redirected away from TLS")
            body = response.content
            if len(body) > self.max_download_bytes:
                raise ValueError("provenance exceeds configured size bound")
            raw_advertised = body
            self.last_advertised_provenance = body
            self.last_advertised_provenance_sha256 = hashlib.sha256(body).hexdigest()
            (root / "provenance.pep740.json").write_bytes(body)
        elif selected.registry in {"rhtl", "packages.redhat.com"}:
            (root / "provenance.pep740.json").unlink(missing_ok=True)

        capture_source_origin(
            root=root,
            package=package,
            version=version,
            sdist_path=dest,
            registry=selected.registry,
            download_url=selected.download_url,
            sha256=selected.sha256,
            size=selected.size,
            provenance_url=selected.provenance_url,
            raw_rhtl_index=self.last_rhtl_index,
            raw_advertised_provenance=raw_advertised,
            last_rhtl_url=self.last_rhtl_url,
            last_rhtl_status=self.last_rhtl_status,
            last_rhtl_reason=self.last_rhtl_reason,
            last_advertised_provenance_status=self.last_advertised_provenance_status,
            last_advertised_provenance_remote_url=self.last_advertised_provenance_remote_url,
        )

        return dest, selected, pypi
