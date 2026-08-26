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
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

import httpx
from packaging.version import InvalidVersion, Version

from taisce_cuan.models import (
    BuildDefinition,
    Builder,
    Digest,
    ExternalParameters,
    FromagerInfo,
    IngestionMetadata,
    LightwellBuildsInfo,
    Predicate,
    ResolvedDependency,
    RunDetails,
    RunDetailsMetadata,
    Subject,
)
from taisce_cuan.sdist import canonicalize_name, compute_sha256, extract_sdist_to_source

logger = logging.getLogger(__name__)


def parse_version_safe(ver_str: str) -> Optional[Version]:
    try:
        return Version(ver_str)
    except InvalidVersion:
        return None


class GitMirrorPublisher:
    """Manages git initialization, metadata creation, and pushing to Git forges with SemVer topology."""

    def __init__(
        self,
        forge_url: str = "https://gitlab.cee.redhat.com",
        group: str = "lightwell/lightwell-builds",
        auth_token: Optional[str] = None,
        username: str = "oauth2",
        committer_name: str = "taisce-cuan bot",
        committer_email: str = "lightwell@redhat.com",
        remote_url: Optional[str] = None,
        gitlab_url: Optional[str] = None,
    ):
        base_url = (gitlab_url or forge_url).rstrip("/")
        self.forge_url = base_url
        self.gitlab_url = base_url
        self.group = group.strip("/")
        self.auth_token = auth_token
        self.username = username
        self.committer_name = committer_name
        self.committer_email = committer_email
        self.explicit_remote_url = remote_url

    def ensure_remote_project(self, repo_name: str) -> str:
        """Ensure the project exists on the remote forge, creating it via API if supported."""
        if self.explicit_remote_url:
            return self.explicit_remote_url

        if not self.auth_token:
            logger.info("No auth_token provided for forge API check; using standard repo URL")
            return f"{self.forge_url}/{self.group}/{repo_name}.git"

        # Check if the forge is GitLab (either domain or API check)
        is_gitlab = "gitlab" in self.forge_url.lower()

        if not is_gitlab:
            logger.warning(
                f"Forge '{self.forge_url}' is not GitLab. Automatic repo creation via API is not supported. "
                f"Assuming repo exists at {self.forge_url}/{self.group}/{repo_name}.git"
            )
            return f"{self.forge_url}/{self.group}/{repo_name}.git"

        headers = {"PRIVATE-TOKEN": self.auth_token}
        encoded_project = urllib.parse.quote(f"{self.group}/{repo_name}", safe="")

        try:
            with httpx.Client(timeout=15.0, verify=False) as client:
                resp = client.get(f"{self.forge_url}/api/v4/projects/{encoded_project}", headers=headers)
                if resp.status_code == 200:
                    logger.info(f"Forge repository {self.group}/{repo_name} exists")
                    return resp.json()["http_url_to_repo"]

                # Try to create project under group in GitLab
                encoded_group = urllib.parse.quote(self.group, safe="")
                group_resp = client.get(f"{self.forge_url}/api/v4/groups/{encoded_group}", headers=headers)
                if group_resp.status_code == 200:
                    group_id = group_resp.json()["id"]
                    create_payload = {
                        "name": repo_name,
                        "path": repo_name,
                        "namespace_id": group_id,
                        "initialize_with_readme": False,
                        "visibility": "internal",
                    }
                    create_resp = client.post(f"{self.forge_url}/api/v4/projects", headers=headers, json=create_payload)
                    if create_resp.status_code == 201:
                        logger.info(f"Created new forge repository {self.group}/{repo_name}")
                        return create_resp.json()["http_url_to_repo"]
        except Exception as e:
            logger.warning(f"Could not verify or create forge project via API: {e}")

        return f"{self.forge_url}/{self.group}/{repo_name}.git"

    def ensure_gitlab_project(self, repo_name: str) -> str:
        return self.ensure_remote_project(repo_name)

    def get_existing_tags(self, repo_dir: Path, canonical: str) -> List[Tuple[Version, str]]:
        """List and parse existing tags matching <canonical>/<version>."""
        res = subprocess.run(
            ["git", "tag", "--list", f"{canonical}/*"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        tags: List[Tuple[Version, str]] = []
        for line in res.stdout.strip().splitlines():
            tag_name = line.strip()
            if not tag_name:
                continue
            ver_part = tag_name.split("/", 1)[-1]
            pv = parse_version_safe(ver_part)
            if pv is not None:
                tags.append((pv, tag_name))
        tags.sort(key=lambda t: t[0])
        return tags

    def check_existing_tag_content(
        self, repo_dir: Path, tag_name: str, expected_source_sha256: str
    ) -> bool:
        """Check if existing tag has matching metadata/source sha256."""
        try:
            show_res = subprocess.run(
                ["git", "show", f"{tag_name}:.lightwell/metadata.json"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if show_res.returncode == 0:
                meta = json.loads(show_res.stdout)
                for subj in meta.get("subject", []):
                    if subj.get("digest", {}).get("sha256") == expected_source_sha256:
                        return True
        except Exception as e:
            logger.debug(f"Error checking existing tag content for {tag_name}: {e}")
        return False

    def sign_attestation(
        self,
        metadata_file: Path,
        source_file: Path,
        sign_key: Optional[str],
        output_provenance_file: Path,
    ) -> Optional[Path]:
        """Optionally create a signed attestation / signature via cosign if signing key is provided."""
        if not sign_key or not sign_key.strip():
            logger.debug("No sign_key provided to sign_attestation; skipping")
            return None

        # Check if signing key is a file path and verify existence
        if (
            sign_key.startswith("/")
            or sign_key.startswith("./")
            or sign_key.startswith("../")
            or sign_key.endswith(".key")
            or sign_key.endswith(".pem")
        ):
            if not Path(sign_key).exists():
                logger.warning(f"Signing key file '{sign_key}' does not exist; skipping attestation signing")
                return None

        cosign_bin = shutil.which("cosign")
        if not cosign_bin:
            logger.warning("Signing key was provided but cosign binary is not installed in PATH; skipping attestation signing")
            return None

        output_provenance_file.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            cosign_bin,
            "attest-blob",
            str(source_file),
            f"--predicate={metadata_file}",
            "--type=https://slsa.dev/provenance/v1",
            f"--key={sign_key}",
            "--yes",
            "--tlog-upload=false",
            f"--output-file={output_provenance_file}",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and output_provenance_file.exists():
                logger.info(f"Successfully created signed attestation ({output_provenance_file})")
                return output_provenance_file
            else:
                logger.warning(f"cosign attest-blob failed: {res.stderr}")
        except Exception as e:
            logger.warning(f"Error executing cosign signing: {e}")
        return None

    def publish_source(
        self,
        source_path: Path,
        package: str,
        version: str,
        workspace_dir: Path,
        upstream_pypi_url: Optional[str] = None,
        upstream_pypi_sha256: Optional[str] = None,
        source_registry: str = "pypi.org",
        allow_overwrite: bool = False,
        sign_key: Optional[str] = None,
        provenance_path: Optional[Path] = None,
        dry_run: bool = False,
    ) -> str:
        """Explode source archive, maintain SemVer topology, record metadata/signing, commit, tag, and push."""
        canonical = canonicalize_name(package)
        tag_name = f"{canonical}/{version}"
        target_ver = parse_version_safe(version)

        repo_name = f"pypi.org-{canonical}"
        repo_dir = workspace_dir / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)

        source_sha256 = compute_sha256(source_path)
        logger.info(f"Publishing {package} {version} ({source_sha256}) to {repo_name}")

        # Git init if repo not present
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", self.committer_name], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", self.committer_email], cwd=repo_dir, check=True)

        # Check existing tags
        existing_tags = self.get_existing_tags(repo_dir, canonical)
        existing_tag_names = [t[1] for t in existing_tags]

        # Overwrite / Idempotency check
        if tag_name in existing_tag_names:
            if self.check_existing_tag_content(repo_dir, tag_name, source_sha256):
                logger.info(f"Tag {tag_name} already exists with identical SHA-256 ({source_sha256}). Nothing to do.")
                return tag_name
            if not allow_overwrite:
                raise ValueError(
                    f"Tag {tag_name} already exists with different content and allow_overwrite is False."
                )
            logger.warning(f"Tag {tag_name} exists but allow_overwrite=True; updating tag content.")

        # Determine target branch and base commit based on SemVer topology
        target_branch = "main"

        if target_ver is not None and existing_tags:
            highest_ver, highest_tag = existing_tags[-1]

            if target_ver >= highest_ver:
                target_branch = "main"
                try:
                    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, capture_output=True, check=False)
                except Exception:
                    pass
            else:
                predecessors = [t for t in existing_tags if t[0] < target_ver]
                major_minor_stream = f"stream/{target_ver.major}.{target_ver.minor}"

                if predecessors:
                    nearest_ver, nearest_tag = predecessors[-1]
                    logger.info(f"Backfill detected: branching {major_minor_stream} from predecessor {nearest_tag}")
                    subprocess.run(["git", "checkout", "-B", major_minor_stream, nearest_tag], cwd=repo_dir, check=True)
                else:
                    logger.info(f"Backfill detected with no predecessor: creating orphan stream {major_minor_stream}")
                    subprocess.run(["git", "checkout", "--orphan", major_minor_stream], cwd=repo_dir, check=True)
                    subprocess.run(["git", "rm", "-rf", "."], cwd=repo_dir, capture_output=True, check=False)

                target_branch = major_minor_stream

        # Clean existing source/ directory before unpacking
        source_dir = repo_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        extract_sdist_to_source(source_path, source_dir)

        # Prepare .lightwell/metadata.json
        lightwell_dir = repo_dir / ".lightwell"
        lightwell_dir.mkdir(exist_ok=True)
        metadata_file = lightwell_dir / "metadata.json"

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        subjects = [
            Subject(name=f"{canonical}-{version}.tar.gz", digest=Digest(sha256=source_sha256)),
            Subject(name="source/", digest=Digest(gitTree="pending")),
        ]

        resolved_deps: List[ResolvedDependency] = []
        if upstream_pypi_url:
            resolved_deps.append(
                ResolvedDependency(
                    name=f"{canonical}-{version}.tar.gz (pypi.org)",
                    uri=upstream_pypi_url,
                    digest={"sha256": upstream_pypi_sha256 or source_sha256},
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

        # Git stage and commit initial state
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        commit_msg = f"ingest: {canonical} {version} from {source_registry}\n\nsha256: {source_sha256}"
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

        # Optional Ingestion Signing
        if sign_key:
            logger.info("Signing key provided; generating signed attestation")
            target_att_file = provenance_path or (lightwell_dir / "provenance.json")
            self.sign_attestation(
                metadata_file=metadata_file,
                source_file=source_path,
                sign_key=sign_key,
                output_provenance_file=target_att_file,
            )
            subprocess.run(["git", "add", "-A", str(lightwell_dir)], cwd=repo_dir, check=True)
        else:
            logger.info("No signing key provided; recording unsigned inventory")

        # Amend commit with updated metadata and signature
        subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=repo_dir, check=True)

        # Tag creation
        tag_flag = ["-f"] if allow_overwrite else []
        subprocess.run(["git", "tag", *tag_flag, tag_name], cwd=repo_dir, check=True)
        logger.info(f"Tagged {tag_name} (gitTree: {git_tree_sha}) on branch {target_branch}")

        if dry_run:
            logger.info("Dry-run requested; skipping git push")
            return tag_name

        # Remote URL & Authentication
        remote_url = self.ensure_remote_project(repo_name)

        if self.auth_token:
            parsed = urllib.parse.urlparse(remote_url)
            auth_netloc = f"{self.username}:{self.auth_token}@{parsed.netloc}"
            push_url = urllib.parse.urlunparse(parsed._replace(netloc=auth_netloc))
        else:
            push_url = remote_url

        push_cmd = ["git", "push", push_url, target_branch, tag_name]
        if allow_overwrite:
            push_cmd.insert(2, "-f")

        subprocess.run(push_cmd, cwd=repo_dir, check=True)
        logger.info(f"Pushed {repo_name} branch {target_branch} and tag {tag_name} to remote")
        return tag_name

    def publish_sdist(
        self,
        sdist_path: Path,
        package: str,
        version: str,
        workspace_dir: Path,
        upstream_pypi_url: Optional[str] = None,
        upstream_pypi_sha256: Optional[str] = None,
        source_registry: str = "pypi.org",
        allow_overwrite: bool = False,
        sign_key: Optional[str] = None,
        dry_run: bool = False,
    ) -> str:
        return self.publish_source(
            source_path=sdist_path,
            package=package,
            version=version,
            workspace_dir=workspace_dir,
            upstream_pypi_url=upstream_pypi_url,
            upstream_pypi_sha256=upstream_pypi_sha256,
            source_registry=source_registry,
            allow_overwrite=allow_overwrite,
            sign_key=sign_key,
            dry_run=dry_run,
        )
