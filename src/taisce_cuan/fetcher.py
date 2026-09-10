"""Secure acquisition of Python source distributions and origin evidence."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from taisce_cuan.sdist import canonicalize_name, compute_sha256

logger = logging.getLogger(__name__)
RHTL_SIMPLE_DEFAULT = "https://packages.redhat.com/api/pypi/public-trusted-libraries/main/simple"
PYPI_API_DEFAULT = "https://pypi.org/pypi"
# Provenance is opaque, but must still have a bounded resource commitment.
MAX_PROVENANCE_RESPONSE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SdistSourceInfo:
    registry: str
    download_url: str
    sha256: str
    size: int
    upload_time: Optional[str]
    provenance_url: Optional[str] = None
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    response_bytes: Optional[bytes] = None
    response_status: Optional[int] = None
    response_url: Optional[str] = None


class SdistFetcher:
    """Resolve and download sdists. Network TLS verification is never disabled."""
    def __init__(self, rhtl_simple_url: str = RHTL_SIMPLE_DEFAULT,
                 pypi_api_url: str = PYPI_API_DEFAULT, client: Optional[httpx.Client] = None):
        self.rhtl_simple_url = rhtl_simple_url.rstrip("/")
        self.pypi_api_url = pypi_api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)

    def query_rhtl(self, package: str, version: str) -> Optional[SdistSourceInfo]:
        url = f"{self.rhtl_simple_url}/{canonicalize_name(package)}/"
        try:
            response = self._client.get(url, headers={"Accept": "application/vnd.pypi.simple.v1+json"})
            if response.status_code != 200:
                return None
            for entry in response.json().get("files", []):
                filename = entry.get("filename", "")
                if filename.endswith(".tar.gz") and f"-{version}." in filename:
                    if "provenance" in entry:
                        advertised = entry["provenance"]
                        if not isinstance(advertised, str) or not advertised:
                            raise ValueError("RHTL advertised provenance is malformed or empty")
                    else:
                        advertised = None
                    return SdistSourceInfo("rhtl", entry["url"], entry.get("hashes", {}).get("sha256", ""),
                        entry.get("size", 0), entry.get("upload-time"), advertised,
                        response_bytes=response.content, response_status=response.status_code, response_url=url)
        except ValueError as exc:
            if "advertised provenance" in str(exc):
                raise
            logger.warning("Error querying RHTL for %s %s: %s", package, version, exc)
        except (httpx.HTTPError, KeyError) as exc:
            logger.warning("Error querying RHTL for %s %s: %s", package, version, exc)
        return None

    def query_pypi(self, package: str, version: str) -> Optional[SdistSourceInfo]:
        try:
            response = self._client.get(f"{self.pypi_api_url}/{package}/{version}/json")
            if response.status_code != 200:
                return None
            data = response.json()
            for entry in data.get("urls", []):
                if entry.get("filename", "").endswith(".tar.gz"):
                    # Keep PyPI's origin metadata verbatim; it is not provenance.
                    origin = entry.get("data-requires-python") or data.get("info", {}).get("project_urls")
                    return SdistSourceInfo("pypi.org", entry["url"], entry.get("digests", {}).get("sha256", ""),
                        entry.get("size", 0), entry.get("upload_time"), None,
                        {"project_urls": data.get("info", {}).get("project_urls", {}),
                         "requires_python": entry.get("data-requires-python")})
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("Error querying PyPI for %s %s: %s", package, version, exc)
        return None

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            # A directory fsync makes the rename durable. Windows does not support
            # opening directories this way, so durability is best-effort there.
            if os.name != "nt":
                flags = getattr(os, "O_DIRECTORY", 0)
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY | flags)
                except OSError:
                    directory_fd = None
                if directory_fd is not None:
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _retrieve_provenance(self, url: str, output_dir: Path, origin: dict[str, Any]) -> None:
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"provenance URL must use HTTP(S): {url}")
        # Stream opaque bytes so neither httpx nor this code materializes an unbounded body.
        with self._client.stream("GET", url) as response:
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise ValueError("provenance response has invalid Content-Length") from exc
                if declared_length < 0 or declared_length > MAX_PROVENANCE_RESPONSE_BYTES:
                    raise ValueError("provenance response exceeds maximum size")
            filename = "provenance-response.bin"  # explicitly not a DSSE envelope
            temporary = output_dir / f".{filename}.{os.getpid()}.tmp"
            digest = hashlib.sha256()
            total = 0
            try:
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_PROVENANCE_RESPONSE_BYTES:
                            raise ValueError("provenance response exceeds maximum size")
                        stream.write(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output_dir / filename)
                if os.name != "nt":
                    flags = getattr(os, "O_DIRECTORY", 0)
                    try:
                        directory_fd = os.open(output_dir, os.O_RDONLY | flags)
                    except OSError:
                        directory_fd = None
                    if directory_fd is not None:
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
        origin.update({"provenance_response_path": filename, "provenance_url": url,
                       "provenance_response_sha256": digest.hexdigest(),
                       "provenance_response_status": response.status_code})
        if response.status_code != 200:
            raise ValueError(f"provenance response returned HTTP {response.status_code}")

    def fetch(self, package: str, version: str, output_dir: Path, rhtl_only: bool = False
              ) -> tuple[Path, SdistSourceInfo, Optional[SdistSourceInfo]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        rhtl_info, pypi_info = self.query_rhtl(package, version), self.query_pypi(package, version)
        target = rhtl_info or (None if rhtl_only else pypi_info)
        if not target:
            raise RuntimeError(f"Package {package} {version} could not be resolved from RHTL or PyPI")
        destination = output_dir / f"{canonicalize_name(package)}-{version}.tar.gz"
        with self._client.stream("GET", target.download_url) as response:
            response.raise_for_status()
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            with temporary.open("wb") as stream:
                for chunk in response.iter_bytes(): stream.write(chunk)
            temporary.replace(destination)
        actual = compute_sha256(destination)
        if target.sha256 and actual.lower() != target.sha256.lower():
            destination.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 mismatch for {destination.name}: expected {target.sha256}, got {actual}")
        # Keep the acquired carrier explicit: Fromager can consume this path and digest
        # without guessing whether verified_sha256 describes the normalized archive.
        artifact_path = destination.name
        origin = {"schema_version": "1", "package": package, "canonical_name": canonicalize_name(package),
                  "version": version, "source_registry": target.registry, "artifact_url": target.download_url,
                  "declared_sha256": target.sha256 or None, "verified_sha256": actual,
                  "acquired_artifact": {"path": artifact_path, "sha256": actual},
                  "provenance_url": target.provenance_url, "retrieved_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                  "pypi_origin_metadata": (pypi_info.origin_metadata if pypi_info else {})}
        if target.registry == "rhtl" and target.response_bytes is not None:
            response_path = output_dir / "rhtl-response.json"
            self._write_atomic(response_path, target.response_bytes)
            origin.update({"rhtl_response_path": response_path.name,
                           "rhtl_response_sha256": hashlib.sha256(target.response_bytes).hexdigest(),
                           "rhtl_response_url": target.response_url,
                           "rhtl_response_status": target.response_status})
        if target.provenance_url:
            self._retrieve_provenance(target.provenance_url, output_dir, origin)
        origin_path = output_dir / "source-origin.json"
        self._write_atomic(origin_path, json.dumps(origin, indent=2, sort_keys=True).encode() + b"\n")
        return destination, target, pypi_info
