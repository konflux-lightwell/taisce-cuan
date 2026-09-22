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
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path


def canonicalize_name(name: str) -> str:
    """Normalize package name according to PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_pkg_info(content: str) -> tuple[str | None, str | None]:
    """Extract Name and Version from RFC 822 PKG-INFO metadata content."""
    name: str | None = None
    version: str | None = None
    for line in content.splitlines():
        if line.startswith("Name:") and not name:
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Version:") and not version:
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
    return name, version


def inspect_sdist_metadata(sdist_path: Path) -> tuple[str, str]:
    """Inspect sdist archive to discover package name and version.

    Attempts to read PKG-INFO from the archive.
    Falls back to inspecting root directory inside archive, then parsing
    the filename according to PEP 625:
    {name}-{version}.(tar.gz|tar.bz2|tar.xz|zip)
    Returns (name, version).
    Raises ValueError if neither method succeeds.
    """
    if not sdist_path.is_file():
        raise ValueError(f"Source archive does not exist: {sdist_path}")

    # 1. Try reading PKG-INFO from tarball
    if tarfile.is_tarfile(sdist_path):
        try:
            with tarfile.open(sdist_path, "r:*") as tar:
                members = tar.getmembers()
                for member in members:
                    if member.name == "PKG-INFO" or member.name.endswith("/PKG-INFO"):
                        f = tar.extractfile(member)
                        if f is not None:
                            content = f.read().decode("utf-8", errors="replace")
                            name, version = _parse_pkg_info(content)
                            if name and version:
                                return name, version
                # Check root directory name pattern (e.g. package-1.0.0/)
                root_parts = [m.name.split("/")[0] for m in members if "/" in m.name or m.isdir()]
                if root_parts:
                    m = re.match(r"^([a-zA-Z0-9_.-]+?)-([0-9][a-zA-Z0-9_.+]*)$", root_parts[0])
                    if m:
                        return m.group(1), m.group(2)
        except Exception:
            pass

    # 2. Try reading PKG-INFO from zip
    if zipfile.is_zipfile(sdist_path):
        try:
            with zipfile.ZipFile(sdist_path) as z:
                for zname in z.namelist():
                    if zname == "PKG-INFO" or zname.endswith("/PKG-INFO"):
                        with z.open(zname) as f:
                            content = f.read().decode("utf-8", errors="replace")
                            name, version = _parse_pkg_info(content)
                            if name and version:
                                return name, version
                # Check root directory name in zip
                roots = [name.split("/")[0] for name in z.namelist() if "/" in name]
                if roots:
                    m = re.match(r"^([a-zA-Z0-9_.-]+?)-([0-9][a-zA-Z0-9_.+]*)$", roots[0])
                    if m:
                        return m.group(1), m.group(2)
        except Exception:
            pass

    # 3. Fallback: Parse filename
    filename = sdist_path.name
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tgz"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            m = re.match(r"^([a-zA-Z0-9_.-]+?)-([0-9][a-zA-Z0-9_.+]*)$", stem)
            if m:
                return m.group(1), m.group(2)

    raise ValueError(f"Could not determine package name and version from sdist archive: {sdist_path}")


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def extract_sdist_to_source(sdist_path: Path, dest_source_dir: Path) -> str:
    """Safely unpack an sdist archive (tarball or zip) into a destination directory.

    Strips the top-level directory inside the archive (e.g., package-1.0.0/)
    so that dest_source_dir directly contains the package source code.
    Protects against Zip Slip / path traversal attacks.
    Returns the root directory name found in the sdist.
    """
    dest_source_dir.mkdir(parents=True, exist_ok=True)

    # Extract to temporary staging folder
    staging_dir = dest_source_dir.parent / f".staging_{os.getpid()}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    resolved_staging = staging_dir.resolve()

    try:
        if zipfile.is_zipfile(sdist_path):
            with zipfile.ZipFile(sdist_path, "r") as zf:
                infolist = zf.infolist()
                if not infolist:
                    raise ValueError(f"Empty sdist archive: {sdist_path}")

                # Path traversal / Zip Slip prevention and link rejection
                for member in infolist:
                    target = (staging_dir / member.filename).resolve()
                    if not (target == resolved_staging or target.is_relative_to(resolved_staging)):
                        raise ValueError(f"Dangerous path traversal zip entry: {member.filename}")
                    unix_mode = (member.external_attr >> 16) & 0o170000
                    if unix_mode == 0o120000:
                        raise ValueError(f"Symbolic link zip entry is not allowed: {member.filename}")

                zf.extractall(staging_dir)
        elif tarfile.is_tarfile(sdist_path):
            with tarfile.open(sdist_path, "r:*") as tar:
                members = tar.getmembers()
                if not members:
                    raise ValueError(f"Empty sdist archive: {sdist_path}")

                for member in members:
                    target = (staging_dir / member.name).resolve()
                    if not target.is_relative_to(resolved_staging):
                        raise ValueError(f"Dangerous path traversal tar entry: {member.name}")
                    if member.issym() or member.islnk():
                        raise ValueError(f"Link tar entry is not allowed: {member.name}")

                if hasattr(tarfile, "data_filter"):
                    tar.extractall(path=staging_dir, filter="data")
                else:
                    tar.extractall(path=staging_dir)
        else:
            raise ValueError(f"Unsupported or corrupted sdist archive format: {sdist_path}")

        top_entries = list(staging_dir.iterdir())
        if len(top_entries) == 1 and top_entries[0].is_dir():
            source_content = top_entries[0]
            root_dir_name = top_entries[0].name
        else:
            source_content = staging_dir
            root_dir_name = ""

        # Clear destination directory and copy extracted content
        for item in dest_source_dir.iterdir():
            if item.is_symlink() or not item.is_dir():
                item.unlink()
            else:
                shutil.rmtree(item)

        for item in source_content.iterdir():
            if item.is_symlink():
                raise ValueError(f"Link encountered during archive copy: {item}")
            dest_item = dest_source_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest_item, symlinks=True)
            else:
                shutil.copy2(item, dest_item, follow_symlinks=False)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    return root_dir_name
