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

logger = logging.getLogger(__name__)


class GitMirrorPublisher:
    """Manages git initialization, metadata creation, and pushing to lightwell-builds."""

    def __init__(
        self,
        gitlab_url: str = "https://gitlab.cee.redhat.com",
        group: str = "lightwell/lightwell-builds",
        auth_token: Optional[str] = None,
        username: str = "oauth2",
    ):
        self.gitlab_url = gitlab_url.rstrip("/")
        self.group = group.strip("/")
        self.auth_token = auth_token
        self.username = username

    def ensure_gitlab_project(self, repo_name: str) -> str:
        """Ensure the project exists under the GitLab group, creating it if missing."""
        if not self.auth_token:
            logger.info("No auth_token provided for GitLab API check; using standard repo URL")
            return f"{self.gitlab_url}/{self.group}/{repo_name}.git"

        headers = {"PRIVATE-TOKEN": self.auth_token}
        encoded_project = urllib.parse.quote(f"{self.group}/{repo_name}", safe="")

        with httpx.Client(timeout=15.0, verify=False) as client:
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

    def publish_sdist(
        self,
        sdist_path: Path,
        package: str,
        version: str,
        workspace_dir: Path,
        upstream_pypi_url: Optional[str] = None,
        upstream_pypi_sha256: Optional[str] = None,
        source_registry: str = "pypi.org",
        dry_run: bool = False,
    ) -> str:
        """Explode sdist, record SLSA metadata, commit, tag, and push."""
        canonical = canonicalize_name(package)
        repo_name = f"pypi.org-{canonical}"
        repo_dir = workspace_dir / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)

        sdist_sha256 = compute_sha256(sdist_path)
        logger.info(f"Publishing {package} {version} ({sdist_sha256}) to {repo_name}")

        # Git init
        subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "taisce-cuan bot"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "lightwell@redhat.com"], cwd=repo_dir, check=True)

        # Unpack sdist into source/
        source_dir = repo_dir / "source"
        extract_sdist_to_source(sdist_path, source_dir)

        # Prepare .lightwell/metadata.json
        lightwell_dir = repo_dir / ".lightwell"
        lightwell_dir.mkdir(exist_ok=True)
        metadata_file = lightwell_dir / "metadata.json"

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        subjects = [
            Subject(name=f"{canonical}-{version}.tar.gz", digest=Digest(sha256=sdist_sha256)),
            Subject(name="source/", digest=Digest(gitTree="pending")),
        ]

        resolved_deps: List[ResolvedDependency] = []
        if upstream_pypi_url:
            resolved_deps.append(
                ResolvedDependency(
                    name=f"{canonical}-{version}.tar.gz (pypi.org)",
                    uri=upstream_pypi_url,
                    digest={"sha256": upstream_pypi_sha256 or sdist_sha256},
                )
            )

        metadata = IngestionMetadata(
            subject=subjects,
            predicate=Predicate(
                buildDefinition=BuildDefinition(
                    externalParameters=ExternalParameters(
                        package=package,
                        canonical_name=canonical,
                        version=version,
                    ),
                    resolvedDependencies=resolved_deps,
                ),
                runDetails=RunDetails(
                    builder=Builder(),
                    metadata=RunDetailsMetadata(startedOn=now_str, finishedOn=now_str),
                ),
            ),
            lightwell_builds=LightwellBuildsInfo(
                repo=repo_name,
                source_registry_used=source_registry,
            ),
            fromager=FromagerInfo(),
        )

        with open(metadata_file, "w") as f:
            f.write(metadata.model_dump_json(by_alias=True, indent=2))

        # Git stage and commit
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        commit_msg = f"ingest: {canonical} {version} from {source_registry}\n\nsha256: {sdist_sha256}"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=True)

        # Compute git tree sha and amend metadata
        tree_res = subprocess.run(
            ["git", "ls-tree", "HEAD", "source"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        git_tree_sha = tree_res.stdout.split()[2] if len(tree_res.stdout.split()) >= 3 else ""

        if git_tree_sha:
            metadata.subject[1].digest.gitTree = git_tree_sha
            with open(metadata_file, "w") as f:
                f.write(metadata.model_dump_json(by_alias=True, indent=2))
            subprocess.run(["git", "add", str(metadata_file)], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=repo_dir, check=True)

        tag_name = f"{canonical}/{version}"
        subprocess.run(["git", "tag", "-f", tag_name], cwd=repo_dir, check=True)
        logger.info(f"Tagged {tag_name} (gitTree: {git_tree_sha})")

        if dry_run:
            logger.info("Dry-run requested; skipping git push")
            return tag_name

        # Remote URL & Authentication
        remote_url = self.ensure_gitlab_project(repo_name)

        # In Tekton / CI environments, Git credentials can come from:
        # 1. An explicit auth_token parameter (passed in URL netloc)
        # 2. Standard ambient Git credential helpers / ~/.git-credentials / /tekton/home/.git-credentials
        # 3. An SSH key or preexisting git credential configuration
        if self.auth_token:
            parsed = urllib.parse.urlparse(remote_url)
            auth_netloc = f"{self.username}:{self.auth_token}@{parsed.netloc}"
            push_url = urllib.parse.urlunparse(parsed._replace(netloc=auth_netloc))
        else:
            push_url = remote_url

        subprocess.run(["git", "push", "-f", push_url, "main", "--tags"], cwd=repo_dir, check=True)
        logger.info(f"Pushed {repo_name} main and tag {tag_name} to GitLab")
        return tag_name
