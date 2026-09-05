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
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

import httpx
from packaging.version import InvalidVersion, Version

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
)
from taisce_cuan.sdist import canonicalize_name, compute_sha256, extract_sdist_to_source

logger = logging.getLogger(__name__)


def parse_version_safe(ver_str: str) -> Optional[Version]:
    try:
        return Version(ver_str)
    except InvalidVersion:
        return None


def resolve_provenance_file(
    source_path: Path,
    provenance_path: Optional[Path] = None,
) -> Optional[Tuple[Path, str]]:
    """
    Two-tier provenance resolution:
    - Priority 1: Explicit provenance_path or embedded sdist-provenance.json in the same folder as the sdist.
    - Priority 2: PipelineRun Chains provenance (chains-provenance/*.json) in the extracted artifact directory.
    Returns (Path, description) or None.
    """
    if provenance_path is not None:
        p = Path(provenance_path)
        if p.is_file():
            return p, f"explicit provenance path ({p.name})"
        raise ValueError(f"Explicit provenance_path specified but file does not exist: {provenance_path}")

    if not source_path or not source_path.parent.is_dir():
        return None

    sdist_dir = source_path.parent

    # Priority 1: Embedded sdist provenance in the artifact root
    sdist_prov = sdist_dir / "sdist-provenance.json"
    if sdist_prov.is_file():
        return sdist_prov, f"embedded sdist provenance ({sdist_prov.name})"

    # Priority 2: PipelineRun Chains provenance fetched by extract-py-artifacts
    chains_dir = sdist_dir / "chains-provenance"
    if chains_dir.is_dir():
        chains_files = sorted([f for f in chains_dir.glob("*.json") if f.is_file()])
        if chains_files:
            return chains_files[0], f"Chains provenance ({chains_files[0].name})"

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
            with httpx.Client(timeout=15.0, verify=True) as client:
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
        """Create a signed attestation via cosign if signing key is provided (fail-closed)."""
        key_str = (sign_key or "").strip()
        if not key_str:
            logger.debug("No sign_key provided to sign_attestation; skipping")
            return None

        is_kms = any(
            key_str.startswith(prefix)
            for prefix in ["awskms://", "k8s://", "gcpkms://", "azurekms://", "vault://"]
        )
        if not is_kms:
            key_path = Path(key_str)
            if not key_path.exists() or not key_path.is_file():
                raise ValueError(f"Signing key file '{key_str}' does not exist or is not a file.")

        cosign_bin = shutil.which("cosign")
        if not cosign_bin:
            raise RuntimeError("Signing key provided but 'cosign' CLI binary is not installed in PATH.")

        output_provenance_file.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile("w", suffix="-predicate.json", delete=False) as pred_tmp:
            pred_tmp.write(metadata.predicate.model_dump_json(by_alias=True, exclude_none=True))
            pred_tmp_path = pred_tmp.name

        try:
            cmd = [
                cosign_bin,
                "attest-blob",
                str(source_file),
                f"--predicate={pred_tmp_path}",
                "--type=https://slsa.dev/provenance/v1",
                f"--key={key_str}",
                "--yes",
                "--tlog-upload=false",
                f"--output-file={output_provenance_file}",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0 or not output_provenance_file.exists():
                raise RuntimeError(f"cosign attest-blob failed (exit {res.returncode}): {res.stderr}")
            logger.info(f"Successfully created signed attestation ({output_provenance_file})")
            return output_provenance_file
        finally:
            if os.path.exists(pred_tmp_path):
                os.unlink(pred_tmp_path)

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

        # Configure Git forge credentials safely via config rather than netloc URL
        if self.auth_token:
            if "gitlab" in self.forge_url.lower():
                import base64
                basic_auth = base64.b64encode(f"{self.username}:{self.auth_token}".encode()).decode()
                header = f"Authorization: Basic {basic_auth}"
            else:
                header = f"Authorization: Bearer {self.auth_token}"
            subprocess.run(["git", "config", "http.extraHeader", header], cwd=repo_dir, check=True)

        remote_url = self.ensure_remote_project(repo_name) if not dry_run else None

        # Fetch remote tags and refs if remote is accessible
        if remote_url and not dry_run:
            fetch_res = subprocess.run(
                ["git", "fetch", "--force", "--tags", remote_url, "+refs/heads/*:refs/remotes/origin/*"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if fetch_res.returncode != 0:
                err = fetch_res.stderr.strip().lower()
                # If remote is empty, git fetch returns non-zero (e.g. fatal: couldn't find remote ref)
                if "couldn't find remote ref" in err or "empty repository" in err or "fatal: no remote repository" not in err:
                    logger.info("Remote repository appears empty or new; initializing fresh tree")
                else:
                    raise RuntimeError(f"git fetch failed against {remote_url}: {fetch_res.stderr.strip()}")

        # Check existing tags
        existing_tags = self.get_existing_tags(repo_dir, canonical)
        existing_tag_names = [t[1] for t in existing_tags]

        # Overwrite / Idempotency check
        if tag_name in existing_tag_names:
            if not allow_overwrite:
                if self.check_existing_tag_content(repo_dir, tag_name, source_sha256):
                    logger.info(f"Tag {tag_name} already exists with identical SHA-256 ({source_sha256}). Nothing to do.")
                    return tag_name
                raise ValueError(
                    f"Tag {tag_name} already exists with different content and allow_overwrite is False."
                )
            logger.warning(f"Tag {tag_name} exists but allow_overwrite=True; updating tag and baseline content.")

        # Determine target branch and base commit based on SemVer topology
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

                # Check if stream branch already exists locally or on remote
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
            Subject(name=f"{canonical}-{version}.tar.gz", digest={"sha256": source_sha256}),
            Subject(name="source/", digest={"gitTree": "pending"}),
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
                    metadata=RunDetailsMetadata(
                        startedOn=now_str,
                        finishedOn=now_str,
                        lightwell_builds=LightwellBuildsInfo(
                            repo=repo_name,
                            source_registry_used=source_registry,
                        ),
                        fromager=FromagerInfo(),
                    ),
                ),
            ),
        )

        with open(metadata_file, "w") as f:
            f.write(metadata.model_dump_json(by_alias=True, exclude_none=True, indent=2))

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
            metadata.subject[1].digest = {"gitTree": git_tree_sha}
            with open(metadata_file, "w") as f:
                f.write(metadata.model_dump_json(by_alias=True, exclude_none=True, indent=2))
            subprocess.run(["git", "add", str(metadata_file)], cwd=repo_dir, check=True)

        # Provenance Resolution (Tier 1 -> Tier 2 -> Tier 3)
        provenance_target = lightwell_dir / "provenance.json"
        resolved_prov = resolve_provenance_file(
            source_path=source_path,
            provenance_path=provenance_path,
        )

        if resolved_prov is not None:
            resolved_path, tier_desc = resolved_prov
            shutil.copyfile(resolved_path, provenance_target)
            subprocess.run(["git", "add", str(provenance_target)], cwd=repo_dir, check=True)
            logger.info(f"Resolved {tier_desc} from {resolved_path}; copied to {provenance_target}")
        elif sign_key:
            logger.info("Signing key provided; generating signed attestation")
            self.sign_attestation(
                metadata=metadata,
                source_file=source_path,
                sign_key=sign_key,
                output_provenance_file=provenance_target,
            )
            subprocess.run(["git", "add", "-A", str(lightwell_dir)], cwd=repo_dir, check=True)
        else:
            logger.info("No provenance found and no signing key provided; recording unsigned inventory")

        # Amend commit with updated metadata and signature
        subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=repo_dir, check=True)

        # Tag creation
        tag_flag = ["-f"] if allow_overwrite else []
        subprocess.run(["git", "tag", *tag_flag, tag_name], cwd=repo_dir, check=True)
        logger.info(f"Tagged {tag_name} (gitTree: {git_tree_sha}) on branch {target_branch}")

        # Baseline tag creation (ADR-0005 initial baseline anchor)
        baseline_tag = f"baseline/{version}"
        existing_baseline = subprocess.run(
            ["git", "tag", "--list", baseline_tag], cwd=repo_dir, capture_output=True, text=True
        ).stdout.strip()

        tags_to_push = [tag_name]
        if not existing_baseline or allow_overwrite:
            subprocess.run(["git", "tag", *tag_flag, baseline_tag], cwd=repo_dir, check=True)
            logger.info(f"Tagged initial {baseline_tag} on branch {target_branch}")
            tags_to_push.append(baseline_tag)

        if dry_run or not remote_url:
            logger.info("Dry-run requested; skipping git push")
            return tag_name

        push_cmd = ["git", "push", "--atomic", remote_url, target_branch, *tags_to_push]
        if allow_overwrite:
            push_cmd.insert(2, "-f")

        try:
            subprocess.run(push_cmd, cwd=repo_dir, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            # Sanitize error message to avoid any potential auth leakage
            sanitized_err = e.stderr.replace(self.auth_token, "********") if self.auth_token else e.stderr
            raise RuntimeError(f"Failed to push branch {target_branch} and tag {tag_name}: {sanitized_err}") from None

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
        provenance_path: Optional[Path] = None,
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
            provenance_path=provenance_path,
            dry_run=dry_run,
        )
