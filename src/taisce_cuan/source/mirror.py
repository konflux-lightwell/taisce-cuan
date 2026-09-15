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

import json
import logging
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, List, Optional, Tuple

import httpx
from packaging.version import InvalidVersion, Version

from taisce_cuan.models import IngestionMetadata
from taisce_cuan.provenance.attest import (
    CosignAttestationSigner,
    PublishSourceRequest,
    PublishSourceResult,
    attest_source_mirror,
)
from taisce_cuan.provenance.verify import (
    SourceRoute,
    adapt_rhtl_pep740,
    copy_verified_evidence,
    extract_pep740_predicate_type,
    verify_blob_attestation,
    verify_lightwell_attestations,
    verify_normalized_source_artifact,
)
from taisce_cuan.sdist import canonicalize_name, extract_sdist_to_source

logger = logging.getLogger(__name__)


def parse_version_safe(ver_str: str) -> Optional[Version]:
    try:
        return Version(ver_str)
    except InvalidVersion:
        return None


def _is_rhtl_registry(registry: Optional[str]) -> bool:
    return (registry or "").strip().lower() in {"rhtl", "packages.redhat.com"}


class GitMirrorPublisher:
    """Manages Git initialization, metadata creation, and pushing to Git forges with SemVer topology."""

    adapt_rhtl_pep740 = staticmethod(adapt_rhtl_pep740)
    extract_pep740_predicate_type = staticmethod(extract_pep740_predicate_type)
    verify_blob_attestation = staticmethod(verify_blob_attestation)

    def __init__(
        self,
        forge_url: str = "https://gitlab.cee.redhat.com",
        group: str = "lightwell/lightwell-builds",
        auth_token: Optional[str] = None,
        username: str = "oauth2",
        committer_name: str = "taisce-cuan bot",
        committer_email: str = "lightwell@redhat.com",
        remote_url: Optional[str] = None,
    ):
        self.forge_url = forge_url.rstrip("/")
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
            with httpx.Client(timeout=15.0, verify=True) as client:
                resp = client.get(f"{self.forge_url}/api/v4/projects/{encoded_project}", headers=headers)
                if resp.status_code == 200:
                    logger.info(f"Forge repository {self.group}/{repo_name} exists")
                    return resp.json()["http_url_to_repo"]

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
        metadata: IngestionMetadata,
        source_file: Path,
        sign_key: Optional[str],
        output_provenance_file: Path,
    ) -> Optional[Path]:
        """Delegate wrapper for CosignAttestationSigner."""
        signer = CosignAttestationSigner()
        return signer.sign(metadata, source_file, sign_key, output_provenance_file)

    def publish(self, request: PublishSourceRequest) -> PublishSourceResult:
        """
        Preferred typed API for publishing a verified source artifact.
        """
        canonical = canonicalize_name(request.package)
        tag_name = f"{canonical}/{request.version}"
        target_ver = parse_version_safe(request.version)

        repo_name = f"pypi.org-{canonical}"
        repo_dir = request.workspace_dir / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)
        lightwell_dir = repo_dir / ".lightwell"
        lightwell_dir.mkdir(exist_ok=True)

        # 1. Verify source artifact closure before repository mutation
        verified = verify_normalized_source_artifact(
            request.source_path,
            package=request.package,
            version=request.version,
        )
        source_sha256 = verified.normalized_sha256
        carrier_root = verified.artifact.root
        original_archive = verified.artifact.acquired.sdist
        is_rhtl = verified.route == SourceRoute.RHTL

        # If RHTL, perform adaptation and verification of advertised PEP 740 evidence
        raw_pep740 = carrier_root / "provenance.pep740.json"
        if is_rhtl and raw_pep740.is_file():
            adapted_pep740 = lightwell_dir / "provenance.dsse.json"
            self.adapt_rhtl_pep740(raw_pep740, adapted_pep740)
            effective_pred = (
                request.rhtl_predicate_type
                or self.extract_pep740_predicate_type(raw_pep740)
                or "https://slsa.dev/provenance/v1"
            )
            self.verify_blob_attestation(
                original_archive,
                adapted_pep740,
                request.public_key or "",
                effective_pred,
            )

        logger.info(f"Publishing {request.package} {request.version} ({source_sha256}) to {repo_name}")

        # 2. Git init if repo not present
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", self.committer_name], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", self.committer_email], cwd=repo_dir, check=True)

        if self.auth_token:
            if "gitlab" in self.forge_url.lower():
                import base64
                basic_auth = base64.b64encode(f"{self.username}:{self.auth_token}".encode()).decode()
                header = f"Authorization: Basic {basic_auth}"
            else:
                header = f"Authorization: Bearer {self.auth_token}"
            subprocess.run(["git", "config", "http.extraHeader", header], cwd=repo_dir, check=True)

        remote_url = self.ensure_remote_project(repo_name) if not request.dry_run else None

        if remote_url and not request.dry_run:
            ls_res = subprocess.run(
                ["git", "ls-remote", remote_url],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if ls_res.returncode != 0:
                raise RuntimeError(f"Could not access remote repository {remote_url}: {ls_res.stderr.strip()}")

            fetch_res = subprocess.run(
                ["git", "fetch", "--force", "--tags", remote_url, "+refs/heads/*:refs/remotes/origin/*"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if fetch_res.returncode != 0:
                logger.info("Remote repository appears empty or has no matching refs; initializing fresh tree")

        # 3. Check existing tags and idempotency / overwrite protection
        existing_tags = self.get_existing_tags(repo_dir, canonical)
        existing_tag_names = [t[1] for t in existing_tags]

        if tag_name in existing_tag_names:
            if not request.allow_overwrite:
                if self.check_existing_tag_content(repo_dir, tag_name, source_sha256):
                    logger.info(f"Tag {tag_name} already exists with identical SHA-256 ({source_sha256}). Nothing to do.")
                    return PublishSourceResult(
                        tag_name=tag_name,
                        repo_name=repo_name,
                        target_branch="main",
                        normalized_sha256=source_sha256,
                        route=verified.route,
                        disposition="already-present",
                    )
                raise ValueError(
                    f"Tag {tag_name} already exists with different content and allow_overwrite is False."
                )
            logger.warning(f"Tag {tag_name} exists but allow_overwrite=True; updating tag and baseline content.")

        # 4. Determine target branch and base commit based on SemVer topology
        target_branch = "main"

        if target_ver is not None and existing_tags:
            highest_ver, highest_tag = existing_tags[-1]

            if target_ver >= highest_ver:
                target_branch = "main"
                if subprocess.run(["git", "rev-parse", "--verify", "refs/remotes/origin/main"], cwd=repo_dir, capture_output=True).returncode == 0:
                    subprocess.run(["git", "checkout", "-B", "main", "refs/remotes/origin/main"], cwd=repo_dir, capture_output=True, check=False)
                else:
                    try:
                        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, capture_output=True, check=False)
                    except Exception:
                        pass
            else:
                predecessors = [t for t in existing_tags if t[0] < target_ver]
                stream_epoch = f"{target_ver.epoch}!" if target_ver.epoch else ""
                major_minor_stream = f"stream/{stream_epoch}{target_ver.major}.{target_ver.minor}"

                existing_local = subprocess.check_output(["git", "branch", "--list", major_minor_stream], cwd=repo_dir, text=True).strip()
                has_remote_stream = subprocess.run(["git", "rev-parse", "--verify", f"refs/remotes/origin/{major_minor_stream}"], cwd=repo_dir, capture_output=True).returncode == 0

                if existing_local:
                    logger.info(f"Checking out existing stream branch {major_minor_stream}")
                    subprocess.run(["git", "checkout", major_minor_stream], cwd=repo_dir, check=True)
                elif has_remote_stream:
                    logger.info(f"Checking out remote stream branch {major_minor_stream}")
                    subprocess.run(["git", "checkout", "-b", major_minor_stream, f"refs/remotes/origin/{major_minor_stream}"], cwd=repo_dir, check=True)
                elif predecessors:
                    nearest_ver, nearest_tag = predecessors[-1]
                    logger.info(f"Backfill detected: branching {major_minor_stream} from predecessor {nearest_tag}")
                    subprocess.run(["git", "checkout", "-b", major_minor_stream, nearest_tag], cwd=repo_dir, check=True)
                else:
                    logger.info(f"Backfill detected with no predecessor: creating orphan stream {major_minor_stream}")
                    subprocess.run(["git", "checkout", "--orphan", major_minor_stream], cwd=repo_dir, check=True)
                    subprocess.run(["git", "rm", "-rf", "."], cwd=repo_dir, capture_output=True, check=False)

                target_branch = major_minor_stream

        # 5. Extract source archive
        source_dir = repo_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        extract_sdist_to_source(request.source_path, source_dir)

        # 6. Copy verified evidence closure and archive copies
        copy_verified_evidence(verified.evidence_files, lightwell_dir)
        final_downloads = lightwell_dir / "downloads"
        final_downloads.mkdir(exist_ok=True)
        shutil.copyfile(original_archive, final_downloads / original_archive.name)
        normalized_archive = lightwell_dir / request.source_path.name
        shutil.copyfile(request.source_path, normalized_archive)

        # Cleanup legacy unextended DSSE files
        (lightwell_dir / "metadata.dsse").unlink(missing_ok=True)
        (lightwell_dir / "provenance.dsse").unlink(missing_ok=True)

        if is_rhtl:
            if raw_pep740.is_file():
                shutil.copyfile(raw_pep740, lightwell_dir / "provenance.pep740.json")
                (lightwell_dir / "rhtl-index.pep691.json").unlink(missing_ok=True)
            else:
                shutil.copyfile(carrier_root / "rhtl-index.pep691.json", lightwell_dir / "rhtl-index.pep691.json")
                (lightwell_dir / "provenance.pep740.json").unlink(missing_ok=True)
                (lightwell_dir / "provenance.dsse.json").unlink(missing_ok=True)
        else:
            (lightwell_dir / "rhtl-index.pep691.json").unlink(missing_ok=True)
            (lightwell_dir / "provenance.pep740.json").unlink(missing_ok=True)

        # 7. Attest source mirror: build metadata & sign required outputs
        attest_source_mirror(
            verified=verified,
            repo_dir=repo_dir,
            repo_name=repo_name,
            sign_key=request.sign_key,
            public_key=request.public_key,
            dry_run=request.dry_run,
        )

        # 8. Verify Lightwell attestations
        verify_lightwell_attestations(
            lightwell_dir,
            route=verified.route,
            signed=bool(request.sign_key),
        )

        # 9. Stage, commit, tag, and atomic push
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        has_staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir).returncode != 0
        if has_staged:
            commit_msg = f"ingest: {canonical} {request.version} from {verified.registry}\n\nsha256: {source_sha256}"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=True)

        tag_flag = ["-f"] if request.allow_overwrite else []
        subprocess.run(["git", "tag", *tag_flag, tag_name], cwd=repo_dir, check=True)
        logger.info(f"Tagged {tag_name} on branch {target_branch}")

        baseline_tag = f"baseline/{request.version}"
        existing_baseline = subprocess.run(
            ["git", "tag", "--list", baseline_tag], cwd=repo_dir, capture_output=True, text=True
        ).stdout.strip()

        tags_to_push = [tag_name]
        if not existing_baseline or request.allow_overwrite:
            subprocess.run(["git", "tag", *tag_flag, baseline_tag], cwd=repo_dir, check=True)
            logger.info(f"Tagged initial {baseline_tag} on branch {target_branch}")
            tags_to_push.append(baseline_tag)

        if request.dry_run or not remote_url:
            logger.info("Dry-run requested; skipping git push")
            return PublishSourceResult(
                tag_name=tag_name,
                repo_name=repo_name,
                target_branch=target_branch,
                normalized_sha256=source_sha256,
                route=verified.route,
                disposition="published",
            )

        push_cmd = ["git", "push", "--atomic", remote_url, target_branch, *tags_to_push]
        if request.allow_overwrite:
            push_cmd.insert(2, "-f")

        try:
            subprocess.run(push_cmd, cwd=repo_dir, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            sanitized_err = e.stderr.replace(self.auth_token, "********") if self.auth_token else e.stderr
            raise RuntimeError(f"Failed to push branch {target_branch} and tag {tag_name}: {sanitized_err}") from None

        logger.info(f"Pushed {repo_name} branch {target_branch} and tag {tag_name} to remote")
        return PublishSourceResult(
            tag_name=tag_name,
            repo_name=repo_name,
            target_branch=target_branch,
            normalized_sha256=source_sha256,
            route=verified.route,
            disposition="published",
        )

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
        public_key: Optional[str] = None,
        rhtl_predicate_type: Optional[str] = None,
        dry_run: bool = False,
    ) -> str:
        """Compatibility adapter for publish_source -> returns result.tag_name."""
        request = PublishSourceRequest(
            source_path=source_path,
            package=package,
            version=version,
            workspace_dir=workspace_dir,
            source_registry=source_registry,
            allow_overwrite=allow_overwrite,
            sign_key=sign_key,
            provenance_path=provenance_path,
            public_key=public_key,
            rhtl_predicate_type=rhtl_predicate_type,
            dry_run=dry_run,
        )
        result = self.publish(request)
        return result.tag_name

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
        provenance_path: Optional[Path] = None,
        public_key: Optional[str] = None,
        rhtl_predicate_type: Optional[str] = None,
        dry_run: bool = False,
    ) -> str:
        """Compatibility adapter for publish_sdist -> returns result.tag_name."""
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
            provenance_path=provenance_path,
            public_key=public_key,
            rhtl_predicate_type=rhtl_predicate_type,
            dry_run=dry_run,
        )
