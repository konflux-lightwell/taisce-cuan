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

import gzip
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zlib
from pathlib import Path
from uuid import uuid4

import httpx
from packaging.version import InvalidVersion, Version

from taisce_cuan.models import IngestionMetadata
from taisce_cuan.provenance.attest import (
    CosignAttestationSigner,
    PublishGitSourceRequest,
    PublishSourceRequest,
    PublishSourceResult,
    attest_git_source_mirror,
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
from taisce_cuan.source.git_source import (
    ClonedGitSource,
    archive_git_source_to_dir,
    clone_and_resolve_git_source,
)

logger = logging.getLogger(__name__)

_PROTECTED_TAG_PATTERNS = ("baseline/*", "novel-head/*")


def _is_already_protected(body: dict | None) -> bool:
    """True when a GitLab 409/422 body indicates the pattern is already protected."""
    if not isinstance(body, dict):
        return False
    message = body.get("message")
    if isinstance(message, str):
        return "already been taken" in message
    if isinstance(message, list):
        return any("already been taken" in str(m) for m in message)
    return False


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
        tag_protection_user_ids: list[int] | None = None,
    ):
        self.forge_url = forge_url.rstrip("/")
        self.group = group.strip("/")
        self.auth_token = auth_token
        self.username = username
        self.committer_name = committer_name
        self.committer_email = committer_email
        self.explicit_remote_url = remote_url
        self.tag_protection_user_ids: list[int] = tag_protection_user_ids or []

    def _gitlab_get_project(
        self, client: httpx.Client, encoded_project: str, headers: dict
    ) -> tuple[str, int] | None:
        """GET a GitLab project. Returns (http_url_to_repo, project_id) or None."""
        resp = client.get(
            f"{self.forge_url}/api/v4/projects/{encoded_project}",
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["http_url_to_repo"], data["id"]
        return None

    def _gitlab_protect_tag_patterns(
        self, client: httpx.Client, project_id: int, headers: dict
    ) -> None:
        """Best-effort: protect baseline/* and novel-head/* on a mirror project.

        Allows the user IDs supplied via tag_protection_user_ids.
        Never raises — a failure must not block the ingestion push.
        """
        if not self.tag_protection_user_ids:
            return

        allowed: list[dict] = [{"user_id": uid} for uid in self.tag_protection_user_ids]

        for pattern in _PROTECTED_TAG_PATTERNS:
            try:
                resp = client.post(
                    f"{self.forge_url}/api/v4/projects/{project_id}/protected_tags",
                    headers=headers,
                    json={"name": pattern, "allowed_to_create": allowed},
                )
                if resp.status_code in (200, 201):
                    logger.info(
                        "Protected tag pattern %r on project %s", pattern, project_id
                    )
                elif resp.status_code in (409, 422) and _is_already_protected(
                    resp.json()
                ):
                    logger.debug(
                        "Tag pattern %r already protected on project %s",
                        pattern,
                        project_id,
                    )
                else:
                    logger.warning(
                        "Could not protect tag pattern %r on project %s: HTTP %s",
                        pattern,
                        project_id,
                        resp.status_code,
                    )
            except Exception:
                logger.warning(
                    "Failed to protect tag pattern %r on project %s",
                    pattern,
                    project_id,
                    exc_info=True,
                )

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
    ) -> tuple[str, int] | None:
        """Create a GitLab project. On 400 with path-taken, retries GET to handle
        race conditions where a concurrent job created the project between our
        initial GET and this POST. Returns (http_url_to_repo, project_id) or None."""
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
            data = resp.json()
            return data["http_url_to_repo"], data["id"]

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
                result = self._gitlab_get_project(client, encoded_project, headers)
                if result:
                    return result
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
                result = self._gitlab_get_project(client, encoded_project, headers)
                if result:
                    url, project_id = result
                    logger.info(f"Forge repository {self.group}/{repo_name} exists")
                    self._gitlab_protect_tag_patterns(client, project_id, headers)
                    return url

                group_id = self._gitlab_get_group_id(client, encoded_group, headers)
                if group_id is not None:
                    result = self._gitlab_create_project(
                        client, group_id, repo_name, encoded_project, headers
                    )
                    if result:
                        url, project_id = result
                        self._gitlab_protect_tag_patterns(client, project_id, headers)
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

    def _existing_tag_canonical_source_sha(
        self, repo_dir: Path, tag_name: str
    ) -> str | None:
        """Read the stored canonical_source_sha from a git-source tag's metadata.

        Used for publish_git_source's idempotency decision instead of a
        tar-hash comparison: the commit SHA is already known and stable,
        whereas the archive's tar bytes can drift across `git` versions or
        (absent attribute neutralization) `.gitattributes` export-subst
        content, producing a spuriously different sha256 for an unchanged
        source commit.

        Returns None if the tag's metadata is missing, invalid, or has no
        canonical_source_sha field (e.g. it was published via the sdist
        route rather than the git route).
        """
        try:
            metadata = json.loads(
                subprocess.check_output(
                    ["git", "show", f"{tag_name}:.lightwell/metadata.json"],
                    cwd=repo_dir,
                    stderr=subprocess.PIPE,
                )
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logger.debug("Error reading canonical_source_sha for %s: %s", tag_name, e)
            return None
        sha = metadata.get("canonical_source_sha")
        return sha if isinstance(sha, str) else None

    def _existing_tag_archive_identity(
        self, repo_dir: Path, tag_name: str
    ) -> tuple[str, str]:
        """Read the stored archive filename and SHA-256 from the tag's metadata."""
        metadata = json.loads(
            subprocess.check_output(
                ["git", "show", f"{tag_name}:.lightwell/metadata.json"],
                cwd=repo_dir,
                stderr=subprocess.PIPE,
            )
        )
        subjects = metadata["subject"]
        if not isinstance(subjects, list) or len(subjects) != 1:
            raise ValueError("Expected exactly one archive subject")
        name = subjects[0]["name"]
        digest = subjects[0]["digest"]["sha256"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".tar.gz")
            or not isinstance(digest, str)
        ):
            raise ValueError("Expected a .tar.gz filename and SHA-256 digest")
        return name, digest.lower()

    def _existing_tag_tar_sha256(self, repo_dir: Path, tag_name: str) -> bytes:
        """Verify the stored archive's digest, then hash its decompressed tar bytes."""
        name, digest = self._existing_tag_archive_identity(repo_dir, tag_name)
        # Stream the Git blob to disk rather than buffering large archives.
        with tempfile.TemporaryFile() as stored:
            subprocess.run(
                ["git", "show", f"{tag_name}:.lightwell/{name}"],
                cwd=repo_dir,
                stdout=stored,
                stderr=subprocess.PIPE,
                check=True,
            )
            stored.seek(0)
            # Trust the stored archive only if its recorded digest matches.
            if hashlib.file_digest(stored, "sha256").hexdigest() != digest:
                raise ValueError("Stored archive does not match its recorded SHA-256")
            stored.seek(0)
            with gzip.GzipFile(fileobj=stored, mode="rb") as stream:
                return hashlib.file_digest(stream, "sha256").digest()

    def check_existing_tag_tar_content(
        self, repo_dir: Path, tag_name: str, source_path: Path
    ) -> bool:
        """Return True when both archives contain exactly the same tar bytes.

        Read the published .tar.gz archive from the existing Git tag and check
        that its SHA-256 matches the tag's metadata. Then decompress both archives
        and compare their SHA-256 hashes. Gzip timestamps and compression settings
        may differ, but tar metadata, file order, and file contents must match.

        Return False if the stored evidence is missing, invalid, or different.
        This check does not modify either archive or the existing Git tag.
        """
        if not source_path.name.endswith(".tar.gz"):
            return False
        try:
            stored_tar_sha256 = self._existing_tag_tar_sha256(repo_dir, tag_name)
            # Ignore gzip headers and compression; compare every tar byte.
            with gzip.open(source_path, "rb") as stream:
                incoming_tar_sha256 = hashlib.file_digest(stream, "sha256").digest()
            return stored_tar_sha256 == incoming_tar_sha256
        except (
            OSError,
            EOFError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.CalledProcessError,
            zlib.error,
        ) as exc:
            logger.debug("Cannot compare tar content for %s: %s", tag_name, exc)
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
            exact_match = self.check_existing_tag_content(
                repo_dir, tag_name, source_sha256
            )
            tar_match = not exact_match and self.check_existing_tag_tar_content(
                repo_dir, tag_name, request.source_path
            )
            if exact_match or tar_match:
                logger.info(
                    "Tag %s already exists with %s. Nothing to do; preserving "
                    "the existing archive and provenance.",
                    tag_name,
                    f"identical SHA-256 ({source_sha256})"
                    if exact_match
                    else "identical decompressed tar content (gzip bytes differ)",
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

    def _resolve_and_archive_git_source(
        self, request: PublishGitSourceRequest, clone_scratch_dir: Path
    ) -> tuple[ClonedGitSource, Path, str]:
        """Clone upstream, resolve ref -> commit SHA, and archive that
        commit's tree to a scratch directory, returning a content hash used
        by publish_git_source's idempotency check."""
        cloned = clone_and_resolve_git_source(
            request.upstream_url,
            request.ref,
            clone_scratch_dir,
            package=request.package,
            version=request.version,
        )
        scratch_source_dir = clone_scratch_dir / "archived-source"
        source_sha256 = archive_git_source_to_dir(cloned, scratch_source_dir)
        return cloned, scratch_source_dir, source_sha256

    def _init_git_repo(self, repo_dir: Path) -> None:
        """Git init the repo directory with committer identity, if not
        already a git repo."""
        if (repo_dir / ".git").exists():
            return
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

    def _configure_git_auth(self, repo_dir: Path) -> None:
        """Configure git http.extraHeader for forge auth, if an auth_token is
        set."""
        if not self.auth_token:
            return
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

    def _fetch_existing_remote_refs(self, repo_dir: Path, remote_url: str) -> None:
        """Validate access to remote_url, then fetch its branches and tags
        for the idempotency and branch-advance checks below."""
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

    def _check_git_source_idempotency(
        self, repo_dir: Path, canonical: str, tag_name: str, resolved_sha: str
    ) -> bool:
        """Return True when tag_name already exists pinned to resolved_sha
        (nothing to do). Raise ValueError when tag_name exists with a
        different canonical_source_sha."""
        existing_tags = self.get_existing_tags(repo_dir, canonical)
        existing_tag_names = [t[1] for t in existing_tags]
        if tag_name not in existing_tag_names:
            return False

        stored_sha = self._existing_tag_canonical_source_sha(repo_dir, tag_name)
        if stored_sha == resolved_sha:
            logger.info(
                "Tag %s already exists pinned to commit %s. Nothing to do; "
                "preserving the existing archive and provenance.",
                tag_name,
                resolved_sha,
            )
            return True
        raise ValueError(
            f"Tag {tag_name} already exists with a different "
            f"canonical_source_sha ({stored_sha!r} vs resolved "
            f"{resolved_sha!r}); refusing to overwrite an ingested version."
        )

    def _checkout_orphan_stream_branch(
        self, repo_dir: Path, target_branch: str
    ) -> None:
        """Refuse to advance a stream branch that already exists locally or
        on the remote; otherwise check out a clean orphan branch for it."""
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
                f"Stream branch {target_branch} already exists locally; "
                f"refusing to advance."
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

    def _write_git_source_tree(
        self,
        repo_dir: Path,
        scratch_source_dir: Path,
        cloned: ClonedGitSource,
        repo_name: str,
        source_sha256: str,
        request: PublishGitSourceRequest,
    ) -> None:
        """Move the already-archived source tree into place, then build,
        write, and (if request.sign_key is set) sign the git ingestion
        metadata (sets canonical_source_sha to the resolved upstream
        commit).

        The orphan-branch checkout + `git rm -rf .` in
        _checkout_orphan_stream_branch deletes .lightwell/ from disk (it was
        tracked from a prior version's commit when repo_dir/workspace_dir is
        reused across calls); attest_git_source_mirror re-creates it,
        immediately before writing into it, rather than relying solely on
        the mkdir done near the top of publish_git_source.
        """
        source_dir = repo_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        shutil.move(str(scratch_source_dir), str(source_dir))

        attest_git_source_mirror(
            cloned,
            repo_dir=repo_dir,
            repo_name=repo_name,
            source_sha256=source_sha256,
            sign_key=request.sign_key,
            public_key=request.public_key,
        )

    def _commit_and_tag_git_source(
        self,
        repo_dir: Path,
        canonical: str,
        request: PublishGitSourceRequest,
        cloned: ClonedGitSource,
        tag_name: str,
        target_branch: str,
    ) -> list[str]:
        """Stage, commit, and tag <canonical>/<version> plus
        baseline/<version> (the latter only if not already present). Returns
        the tag names to push."""
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        commit_msg = (
            f"ingest: {canonical} {request.version} from git "
            f"({cloned.upstream_url})\n\ncommit: {cloned.resolved_sha}"
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

        return tags_to_push

    def _push_git_source_tags(
        self,
        repo_dir: Path,
        remote_url: str,
        repo_name: str,
        target_branch: str,
        tags_to_push: list[str],
        tag_name: str,
    ) -> None:
        """Atomically push the stream branch and tags; raise RuntimeError
        with sanitized stderr on failure."""
        push_cmd = [
            "git",
            "push",
            "--atomic",
            remote_url,
            target_branch,
            *tags_to_push,
        ]

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

    def publish_git_source(
        self, request: PublishGitSourceRequest
    ) -> PublishSourceResult:
        """
        Publish a git-sourced package, resolving ``request.ref`` to its exact
        upstream commit SHA and pinning it as ``canonical_source_sha`` in
        ``.lightwell/metadata.json``.

        Mirrors publish()'s git/tag/push mechanics (idempotency via
        check_existing_tag_content, the stream/{version} orphan branch,
        <canonical>/<version> + baseline/<version> tagging, and atomic push)
        but sources the tree from clone_and_resolve_git_source and
        archive_git_source_to_dir instead of a verified sdist artifact.

        Signs .lightwell/metadata.json via cosign when request.sign_key is
        set, same as the sdist route's attest_source_mirror. There is no
        separate provenance.dsse.json for this route: the archived git tree
        is the sole subject and is already covered by the signed metadata.
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

        # The clone lives under workspace_dir in a unique subdirectory (not
        # in system temp) so it benefits from the provisioned storage (PVC)
        # rather than a small tmpfs, avoiding OOM in production. We clean up
        # the clone directory explicitly afterward.
        clone_scratch_dir = request.workspace_dir / f"clone-{uuid4().hex}"
        clone_scratch_dir.mkdir(parents=True, exist_ok=True)

        try:
            cloned, scratch_source_dir, source_sha256 = (
                self._resolve_and_archive_git_source(request, clone_scratch_dir)
            )

            logger.info(
                f"Publishing git source {request.package} {request.version} "
                f"({cloned.resolved_sha}) to {repo_name}"
            )

            self._init_git_repo(repo_dir)
            self._configure_git_auth(repo_dir)

            remote_url = (
                self.ensure_remote_project(repo_name) if not request.dry_run else None
            )
            if remote_url and not request.dry_run:
                self._fetch_existing_remote_refs(repo_dir, remote_url)

            target_branch = f"stream/{request.version}"

            if self._check_git_source_idempotency(
                repo_dir, canonical, tag_name, cloned.resolved_sha
            ):
                return PublishSourceResult(
                    tag_name=tag_name,
                    repo_name=repo_name,
                    target_branch=target_branch,
                    normalized_sha256=source_sha256,
                    route=SourceRoute.GIT,
                    disposition="already-present",
                )

            self._checkout_orphan_stream_branch(repo_dir, target_branch)
            self._write_git_source_tree(
                repo_dir,
                scratch_source_dir,
                cloned,
                repo_name,
                source_sha256,
                request,
            )

            tags_to_push = self._commit_and_tag_git_source(
                repo_dir, canonical, request, cloned, tag_name, target_branch
            )

            if request.dry_run or not remote_url:
                logger.info("Dry-run requested; skipping git push")
                return PublishSourceResult(
                    tag_name=tag_name,
                    repo_name=repo_name,
                    target_branch=target_branch,
                    normalized_sha256=source_sha256,
                    route=SourceRoute.GIT,
                    disposition="published",
                )

            self._push_git_source_tags(
                repo_dir, remote_url, repo_name, target_branch, tags_to_push, tag_name
            )

            return PublishSourceResult(
                tag_name=tag_name,
                repo_name=repo_name,
                target_branch=target_branch,
                normalized_sha256=source_sha256,
                route=SourceRoute.GIT,
                disposition="published",
            )
        finally:
            # Clean up the clone directory to prevent lingering temporary
            # files under workspace_dir (mirrors tempfile.TemporaryDirectory's
            # cleanup behavior)
            shutil.rmtree(clone_scratch_dir, ignore_errors=True)

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
