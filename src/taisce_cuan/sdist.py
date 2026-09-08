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
from pathlib import Path


def canonicalize_name(name: str) -> str:
    """Normalize package name according to PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def extract_sdist_to_source(sdist_path: Path, dest_source_dir: Path) -> str:
    """Safely unpack an sdist tarball into a destination directory.

    Strips the top-level directory inside the tarball (e.g., package-1.0.0/)
    so that dest_source_dir directly contains the package source code.
    Returns the root directory name found in the sdist.
    """
    dest_source_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(sdist_path, "r:*") as tar:
        members = tar.getmembers()
        if not members:
            raise ValueError(f"Empty sdist archive: {sdist_path}")

        # Extract to temporary staging folder
        staging_dir = dest_source_dir.parent / f".staging_{os.getpid()}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        resolved_staging = staging_dir.resolve()
        for member in members:
            target = (staging_dir / member.name).resolve()
            if not target.is_relative_to(resolved_staging):
                raise ValueError(f"Dangerous path traversal tar entry: {member.name}")

        try:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=staging_dir, filter="data")
            else:
                tar.extractall(path=staging_dir)

            top_entries = list(staging_dir.iterdir())
            if len(top_entries) == 1 and top_entries[0].is_dir():
                source_content = top_entries[0]
                root_dir_name = top_entries[0].name
            else:
                source_content = staging_dir
                root_dir_name = ""

            # Clear destination directory and copy extracted content
            for item in dest_source_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

            for item in source_content.iterdir():
                dest_item = dest_source_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest_item)
                else:
                    shutil.copy2(item, dest_item)
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)

    return root_dir_name
