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
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

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
    verify_normalized_source_artifact,
)
from taisce_cuan.sdist import canonicalize_name, extract_sdist_to_source

logger = logging.getLogger(__name__)


def parse_version_safe(ver_str: str) -> Version | None:
    try:
        return Version(ver_str)
    except InvalidVersion:
        return None


def _is_rhtl_registry(registry: str | None) -> bool:
    return (registry or "").strip().lower() in {"rhtl", "packages.redhat.com"}


class GitMirrorPublisher:
    """Manages Git initialization, metadata creation, and pushing to Git forges."""

    adapt_rhtl_pep740 = staticmethod(adapt_rhtl_pep740)
    extract_pep740_predicate_type = staticmethod(extract_pep740_predicate_type)
    verify_blob_attestation = staticmethod(verify_blob_attestation)

    def __init__(
        self,
        forge_url: str = "https://gitlab.cee.redhat.com",
        group: str = "lightwell/lightwell-builds",
        auth_token: str | None = None,
        username: str = "oauth2",
        committer_name: str = "taisce-cuan bot",
        committer_email: str = "lightwell@redhat.com",
        remote_url: str | None = None,
    ):
        self.forge_url = forge_url.rstrip("/")
        self.group = group.strip("/")
        self.auth_token = auth_token
        self.username = username
        self.committer_name = committer_name
        self.committer_email = committer_email
        self.explicit_remote_url = remote_url

    def _gitlab_get_project(
        self, client: httpx.Client, encoded_project: str, headers: dict
    ) -> str | None:
        """GET a GitLab project by encoded path. Returns http_url_to_repo or None."""
        resp = client.get(
            f"{self.forge_url}/api/v4/projects/{encoded_project}",
            headers=headers,
        )
        if resp.status_code == 200:
            return resp.json()["http_url_to_repo"]
        return None

    def _gitlab_get_group_id(
        self, client: httpx.Client, encoded_group: str, headers: dict
    ) -> int | None:
        """GET a GitLab group by encoded path. Returns the numeric group ID or None."""
        resp = client.get(
            f"{self.forge_url}/api/v4/groups/{encoded_group}",
            headers=headers,
        )
        if resp.status_code == 200:
            return resp.json()["id"]
        return None

    def _gitlab_create_project(
        self,
        client: httpx.Client,
        group_id: int,
        repo_name: str,
        encoded_project: str,
        headers: dict,
    ) -> str | None:
        """Create a GitLab project. On 400 with path-taken, retries GET to handle
        race conditions where a concurrent job created the project between our
        initial GET and this POST."""
        resp = client.post(
            f"{self.forge_url}/api/v4/projects",
            headers=headers,
            json={
                "name": repo_name,
                "path": repo_name,
                "namespace_id": group_id,
                "initialize_with_readme": False,
                "visibility": "internal",
            },
        )
        if resp.status_code == 201:
            logger.info(f"Created new forge repository {self.group}/{repo_name}")
            return resp.json()["http_url_to_repo"]

        already_exists = resp.status_code == 400 and (
            "has already been taken" in resp.text or "already exists" in resp.text
        )
        if already_exists:
            # The namespace was created by a concurrent job but the project record may
            # not yet be visible due to GitLab write-visibility lag — retry with
            # backoff.
            for attempt in range(1, 4):
                logger.info(
                    f"Project {self.group}/{repo_name} was created by a concurrent "
                    f"process; retrying GET (attempt {attempt})"
                )
                url = self._gitlab_get_project(client, encoded_project, headers)
                if url:
                    return url
                time.sleep(attempt * 2)
            logger.warning(
                f"Project {self.group}/{repo_name} still not visible after retries; "
                f"falling back to constructed URL"
            )

        logger.warning(
            f"Could not create forge project {self.group}/{repo_name}: "
            f"POST {resp.status_code}: {resp.text}"
        )
        return None

    def ensure_remote_project(self, repo_name: str) -> str:
        """Ensure the project exists on the remote forge, creating it via API if
        supported."""
        if self.explicit_remote_url:
            return self.explicit_remote_url

        if not self.auth_token:
            logger.info(
                "No auth_token provided for forge API check; using standard repo URL"
            )
            return f"{self.forge_url}/{self.group}/{repo_name}.git"

        if "gitlab" not in self.forge_url.lower():
            logger.warning(
                f"Forge '{self.forge_url}' is not GitLab. Automatic repo creation "
                f"via API is not supported. "
                f"Assuming repo exists at {self.forge_url}/{self.group}/{repo_name}.git"
            )
            return f"{self.forge_url}/{self.group}/{repo_name}.git"

        headers = {"PRIVATE-TOKEN": self.auth_token}
        encoded_project = urllib.parse.quote(f"{self.group}/{repo_name}", safe="")
        encoded_group = urllib.parse.quote(self.group, safe="")

        try:
            with httpx.Client(timeout=15.0, verify=True) as client:
                url = self._gitlab_get_project(client, encoded_project, headers)
                if url:
                    logger.info(f"Forge repository {self.group}/{repo_name} exists")
                    return url

                group_id = self._gitlab_get_group_id(client, encoded_group, headers)
                if group_id is not None:
                    url = self._gitlab_create_project(
                        client, group_id, repo_name, encoded_project, headers
                    )
                    if url:
                        return url
        except Exception as e:
            logger.warning(f"Could not verify or create forge project via API: {e}")

        return f"{self.forge_url}/{self.group}/{repo_name}.git"

    def get_existing_tags(
        self, repo_dir: Path, canonical: str
    ) -> list[tuple[Version, str]]:
        """List and parse existing tags matching <canonical>/<version>."""
        res = subprocess.run(
            ["git", "tag", "--list", f"{canonical}/*"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        tags: list[tuple[Version, str]] = []
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
        sign_key: str | None,
        output_provenance_file: Path,
    ) -> Path | None:
        """Delegate wrapper for CosignAttestationSigner."""
        signer = CosignAttestationSigner()
        return signer.sign(metadata, source_file, sign_key, output_provenance_file)

    def publish(self, request: PublishSourceRequest) -> PublishSourceResult:
        """
        Preferred typed API for publishing a verified source artifact.
        """
        canonical = canonicalize_name(request.package)
        tag_name = f"{canonical}/{request.version}"

        if parse_version_safe(request.version) is None:
            raise ValueError(
                f"Cannot parse version {request.version!r}; refusing to mirror an "
                f"unparseable version."
            )

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

        logger.info(
            f"Publishing {request.package} {request.version} ({source_sha256}) "
            f"to {repo_name}"
        )

        # 2. Git init if repo not present
        if not (repo_dir / ".git").exists():
            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", self.committer_name],
                cwd=repo_dir,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", self.committer_email],
                cwd=repo_dir,
                check=True,
            )

        if self.auth_token:
            if "gitlab" in self.forge_url.lower():
                import base64

                basic_auth = base64.b64encode(
                    f"{self.username}:{self.auth_token}".encode()
                ).decode()
                header = f"Authorization: Basic {basic_auth}"
            else:
                header = f"Authorization: Bearer {self.auth_token}"
            subprocess.run(
                ["git", "config", "http.extraHeader", header], cwd=repo_dir, check=True
            )

        remote_url = (
            self.ensure_remote_project(repo_name) if not request.dry_run else None
        )

        if remote_url and not request.dry_run:
            ls_res = subprocess.run(
                ["git", "ls-remote", remote_url],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if ls_res.returncode != 0:
                raise RuntimeError(
                    f"Could not access remote repository {remote_url}: "
                    f"{ls_res.stderr.strip()}"
                )

            fetch_res = subprocess.run(
                [
                    "git",
                    "fetch",
                    "--force",
                    "--tags",
                    remote_url,
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if fetch_res.returncode != 0:
                logger.info(
                    "Remote repository appears empty or has no matching refs; "
                    "initializing fresh tree"
                )

        # 3. Check existing tags and idempotency / overwrite protection
        existing_tags = self.get_existing_tags(repo_dir, canonical)
        existing_tag_names = [t[1] for t in existing_tags]

        target_branch = f"stream/{request.version}"

        if tag_name in existing_tag_names:
            if self.check_existing_tag_content(repo_dir, tag_name, source_sha256):
                logger.info(
                    f"Tag {tag_name} already exists with identical SHA-256 "
                    f"({source_sha256}). Nothing to do."
                )
                return PublishSourceResult(
                    tag_name=tag_name,
                    repo_name=repo_name,
                    target_branch=target_branch,
                    normalized_sha256=source_sha256,
                    route=verified.route,
                    disposition="already-present",
                )
            raise ValueError(
                f"Tag {tag_name} already exists with different content; refusing "
                f"to overwrite an ingested version."
            )

        # 4. Check out the version-derived stream branch, refusing to advance one
        # that already exists during ingestion.
        exists_local = (
            subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{target_branch}"],
                cwd=repo_dir,
                capture_output=True,
            ).returncode
            == 0
        )
        exists_remote = (
            subprocess.run(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    f"refs/remotes/origin/{target_branch}",
                ],
                cwd=repo_dir,
                capture_output=True,
            ).returncode
            == 0
        )
        if exists_local:
            raise ValueError(
                f"Stream branch {target_branch} already exists locally; refusing "
                f"to advance."
            )
        elif exists_remote:
            raise ValueError(
                f"Stream branch {target_branch} already exists; refusing to "
                f"advance it during ingestion."
            )

        subprocess.run(
            ["git", "checkout", "--orphan", target_branch], cwd=repo_dir, check=True
        )
        subprocess.run(
            ["git", "rm", "-rf", "."], cwd=repo_dir, capture_output=True, check=False
        )

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
                shutil.copyfile(
                    carrier_root / "rhtl-index.pep691.json",
                    lightwell_dir / "rhtl-index.pep691.json",
                )
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

        # 8. Stage, commit, tag, and atomic push
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        commit_msg = (
            f"ingest: {canonical} {request.version} from {verified.registry}"
            f"\n\nsha256: {source_sha256}"
        )
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=True)

        subprocess.run(["git", "tag", tag_name], cwd=repo_dir, check=True)
        logger.info(f"Tagged {tag_name} on branch {target_branch}")

        baseline_tag = f"baseline/{request.version}"
        existing_baseline = subprocess.run(
            ["git", "tag", "--list", baseline_tag],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        ).stdout.strip()

        tags_to_push = [tag_name]
        if not existing_baseline:
            subprocess.run(["git", "tag", baseline_tag], cwd=repo_dir, check=True)
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

        try:
            subprocess.run(
                push_cmd, cwd=repo_dir, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as e:
            sanitized_err = (
                e.stderr.replace(self.auth_token, "********")
                if self.auth_token
                else e.stderr
            )
            raise RuntimeError(
                f"Failed to push branch {target_branch} and tag {tag_name}: "
                f"{sanitized_err}"
            ) from None

        logger.info(
            f"Pushed {repo_name} branch {target_branch} and tag {tag_name} to remote"
        )
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
        upstream_pypi_url: str | None = None,
        upstream_pypi_sha256: str | None = None,
        source_registry: str = "pypi.org",
        sign_key: str | None = None,
        provenance_path: Path | None = None,
        public_key: str | None = None,
        rhtl_predicate_type: str | None = None,
        dry_run: bool = False,
    ) -> str:
        """Compatibility adapter for publish_source -> returns result.tag_name."""
        request = PublishSourceRequest(
            source_path=source_path,
            package=package,
            version=version,
            workspace_dir=workspace_dir,
            source_registry=source_registry,
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
        upstream_pypi_url: str | None = None,
        upstream_pypi_sha256: str | None = None,
        source_registry: str = "pypi.org",
        sign_key: str | None = None,
        provenance_path: Path | None = None,
        public_key: str | None = None,
        rhtl_predicate_type: str | None = None,
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
            sign_key=sign_key,
            provenance_path=provenance_path,
            public_key=public_key,
            rhtl_predicate_type=rhtl_predicate_type,
            dry_run=dry_run,
        )
