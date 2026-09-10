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

import datetime
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse

import httpx

from taisce_cuan.models import (
    BuildDefinition,
    Builder,
    ExternalParameters,
    FromagerInfo,
    IngestionMetadata,
    LightwellBuildsInfo,
    Predicate,
    ResolvedDependency,
    RunDetails,
    RunDetailsMetadata,
    Subject,
    Digest,
)
from taisce_cuan.sdist import canonicalize_name, compute_sha256, extract_sdist_to_source
from taisce_cuan.bindings import validate_bindings

logger = logging.getLogger(__name__)


class GitMirrorPublisher:
    """Manages git initialization, metadata creation, and pushing to lightwell-builds."""

    def __init__(
        self,
        gitlab_url: str = "https://gitlab.cee.redhat.com",
        group: str = "lightwell/lightwell-builds",
        auth_token_file: Optional[Path] = None,
        username: str = "oauth2",
    ):
        self.gitlab_url = gitlab_url.rstrip("/")
        self.group = group.strip("/")
        self.auth_token_file = auth_token_file
        self.username = username

    def _auth_token(self) -> Optional[str]:
        if not self.auth_token_file:
            return None
        try:
            token = self.auth_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"unable to read GitLab auth token file: {self.auth_token_file}") from exc
        if not token:
            raise ValueError("GitLab auth token file is empty")
        return token

    def ensure_gitlab_project(self, repo_name: str) -> str:
        """Ensure the project exists under the GitLab group, creating it if missing."""
        token = self._auth_token()
        if not token:
            logger.info("No GitLab auth token provided; using standard repo URL")
            return f"{self.gitlab_url}/{self.group}/{repo_name}.git"

        headers = {"PRIVATE-TOKEN": token}
        encoded_project = urllib.parse.quote(f"{self.group}/{repo_name}", safe="")

        # httpx verifies TLS by default and honors SSL_CERT_FILE/SSL_CERT_DIR
        # for deployments using a configured private CA.
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{self.gitlab_url}/api/v4/projects/{encoded_project}", headers=headers)
            if resp.status_code == 200:
                logger.info(f"GitLab repository {self.group}/{repo_name} exists")
                return resp.json()["http_url_to_repo"]

            # Try to create project under group
            encoded_group = urllib.parse.quote(self.group, safe="")
            group_resp = client.get(f"{self.gitlab_url}/api/v4/groups/{encoded_group}", headers=headers)
            if group_resp.status_code == 200:
                group_id = group_resp.json()["id"]
                create_payload = {
                    "name": repo_name,
                    "path": repo_name,
                    "namespace_id": group_id,
                    "initialize_with_readme": False,
                    "visibility": "internal",
                }
                create_resp = client.post(f"{self.gitlab_url}/api/v4/projects", headers=headers, json=create_payload)
                if create_resp.status_code == 201:
                    logger.info(f"Created new GitLab repository {self.group}/{repo_name}")
                    return create_resp.json()["http_url_to_repo"]

        return f"{self.gitlab_url}/{self.group}/{repo_name}.git"

    @staticmethod
    def _copy_artifact(source: Path, destination: Path) -> None:
        import shutil
        if not source.is_file():
            raise ValueError(f"binding artifact does not exist: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_file() and destination.read_bytes() == source.read_bytes():
                return
            raise ValueError(f"repository artifact already exists with different bytes: {destination}")
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            shutil.copyfile(source, temporary); temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _binding_paths(binding: dict) -> list[str]:
        paths = []
        for name, value in binding.items():
            if name in {"schema", "version"}: continue
            if name == "upstream_provenance_response":
                value = [value]
            else: value = [value]
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("path"), str): paths.append(item["path"])
            # unavailable evidence nests the response artifact one level deeper.
            if isinstance(item, dict) and name == "upstream_provenance_unavailable":
                response = item.get("index_response")
                if isinstance(response, dict) and isinstance(response.get("path"), str):
                    paths.append(response["path"])
        return paths

    def publish_sdist(self, sdist_path: Path, package: str, version: str, workspace_dir: Path,
                      upstream_pypi_url: Optional[str] = None, upstream_pypi_sha256: Optional[str] = None,
                      source_registry: str = "pypi.org", source_origin_path: Optional[Path] = None,
                      signer_authorization_path: Optional[Path] = None, artifact_boundary_path: Optional[Path] = None,
                      dry_run: bool = False) -> str:
        """Publish catalog-finalized artifacts without generating or changing metadata."""
        del upstream_pypi_url, upstream_pypi_sha256
        canonical = canonicalize_name(package); repo_name = f"pypi.org-{canonical}"
        repo_dir = workspace_dir / repo_name; repo_dir.mkdir(parents=True, exist_ok=True)
        if not source_origin_path or not signer_authorization_path or not artifact_boundary_path:
            raise ValueError("catalog-finalized artifacts are required; unsigned metadata generation is disabled")
        try:
            binding_bytes = signer_authorization_path.read_bytes(); boundary_bytes = artifact_boundary_path.read_bytes()
            binding = json.loads(binding_bytes); boundary = json.loads(boundary_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("catalog authorization and boundary must be valid JSON") from exc
        if binding_bytes != boundary_bytes or binding != boundary or not isinstance(binding, dict):
            raise ValueError("authorization and artifact boundary must be byte-identical assertions")

        # Copy the complete binding closure before validating it; catalog metadata is immutable.
        paths = self._binding_paths(binding) + ["source/", "source-origin.json"]
        seen = set()
        for relative in paths:
            if relative in seen: continue
            seen.add(relative)
            candidate = Path(relative)
            if not relative or candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"binding artifact path is not repository-relative: {relative!r}")
            destination = repo_dir / relative
            if relative == "source/":
                extract_sdist_to_source(sdist_path, destination); continue
            source = workspace_dir / relative
            if relative == "source-origin.json": source = source_origin_path
            if relative == binding.get("normalized_archive", {}).get("path"):
                # The finalized normalized archive is also a workspace artifact;
                # use that exact file rather than rewriting it from the input.
                if not source.is_file() or compute_sha256(source) != binding["normalized_archive"].get("sha256"):
                    raise ValueError("workspace normalized archive does not match catalog binding")
            self._copy_artifact(source, destination)

        if not dry_run:
            validate_bindings(repo_dir, sdist_path, source_origin_path, signer_authorization_path,
                              artifact_boundary_path, canonical, version)
        subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "taisce-cuan bot"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "lightwell@redhat.com"], cwd=repo_dir, check=True)
        explicit = ["source"] + [p for p in seen if p != "source/"]
        subprocess.run(["git", "add", "--", *explicit], cwd=repo_dir, check=True)
        digest = compute_sha256(sdist_path)
        tag_name = f"{canonical}/{version}"
        askpass = repo_dir / ".git-askpass"
        env = os.environ.copy()
        if self.auth_token_file:
            askpass.write_text(
                f'#!/bin/sh\ncase "$1" in\n  *Username*) printf "%s\\n" "{self.username}" ;;\n  *) cat "{self.auth_token_file}" ;;\nesac\n',
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
        if not dry_run:
            remote_url = self.ensure_gitlab_project(repo_name)
            # Check all immutable targets before creating the commit or tag.
            local_tag = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag_name}"], cwd=repo_dir)
            if local_tag.returncode == 0:
                raise ValueError(f"target tag already exists locally: {tag_name}")
            remote_refs = subprocess.run(["git", "ls-remote", "--exit-code", remote_url,
                                          f"refs/heads/main", f"refs/tags/{tag_name}"], cwd=repo_dir,
                                         capture_output=True, text=True)
            if remote_refs.returncode == 0 and remote_refs.stdout.strip():
                raise ValueError(f"target branch or tag already exists remotely: {tag_name}")
            if remote_refs.returncode not in (0, 2):
                raise ValueError("unable to preflight remote target refs")
        subprocess.run(["git", "commit", "-m", f"ingest: {canonical} {version} from {source_registry}\n\nsha256: {digest}"], cwd=repo_dir, check=True)
        subprocess.run(["git", "tag", tag_name], cwd=repo_dir, check=True)
        if dry_run:
            askpass.unlink(missing_ok=True)
            return tag_name
        try:
            subprocess.run(["git", "push", "--atomic", remote_url, "main", "--tags"], cwd=repo_dir, check=True, env=env)
        finally:
            askpass.unlink(missing_ok=True)
        return tag_name
