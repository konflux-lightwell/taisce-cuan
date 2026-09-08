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

import argparse
import logging
import os
import sys
from pathlib import Path

from taisce_cuan.fetcher import SdistFetcher
from taisce_cuan.git_mirror import GitMirrorPublisher

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("taisce-cuan")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taisce-cuan",
        description="Lightwell Python source distribution ingestion and preservation tool",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch command
    fetch_parser = subparsers.add_parser("fetch", help="Fetch a source archive from RHTL/PyPI and verify its checksum")
    fetch_parser.add_argument("package", help="Package name (e.g. sniffio)")
    fetch_parser.add_argument("version", help="Package version (e.g. 1.3.1)")
    fetch_parser.add_argument("--output-dir", "-o", default="./sdists", help="Directory to save downloaded source archive")
    fetch_parser.add_argument("--registries", default="rhtl,pypi.org", help="Comma-separated ordered list of source registries to query (e.g. 'rhtl,pypi.org', 'rhtl', 'pypi.org')")
    fetch_parser.add_argument("--rhtl-only", action="store_true", help="Fail if not found in RHTL (deprecated: use --registries=rhtl)")

    # push command
    push_parser = subparsers.add_parser("push", help="Unpack source archive, generate SLSA metadata, commit and push to Git forge")
    push_parser.add_argument("--source", "--sdist", "-s", required=True, dest="source", help="Path to local source archive (.tar.gz)")
    push_parser.add_argument("--package", "-p", required=True, help="Package name")
    push_parser.add_argument("--version", "-v", required=True, help="Package version")
    push_parser.add_argument("--workspace-dir", "-w", default="/tmp/taisce-work", help="Working directory for git repo")
    push_parser.add_argument("--forge-url", "--gitlab-url", required=True, help="Git forge base URL")
    push_parser.add_argument("--group", required=True, help="Target group or organization on the forge")
    push_parser.add_argument("--remote-url", default=os.getenv("GIT_REMOTE_URL"), help="Explicit full remote Git repository URL")
    push_parser.add_argument("--auth-token", default=os.getenv("GITLAB_TOKEN") or os.getenv("GIT_AUTH_TOKEN"), help="Git forge access token")
    push_parser.add_argument("--committer-name", required=True, help="Git author and committer name")
    push_parser.add_argument("--committer-email", required=True, help="Git author and committer email")
    push_parser.add_argument("--allow-overwrite", "--overwrite", action="store_true", help="Allow updating existing tag with different content")
    push_parser.add_argument("--sign-key", default=os.getenv("SIGN_KEY"), help="Path or KMS key ID for cosign attestation signing")
    push_parser.add_argument("--provenance-path", type=Path, help="Explicit file path where signed provenance should be written")
    push_parser.add_argument("--dry-run", action="store_true", help="Do not push to remote")

    return parser


def handle_fetch(args: argparse.Namespace) -> int:
    fetcher = SdistFetcher()
    output_dir = Path(args.output_dir)
    try:
        sdist_path, source_info, _ = fetcher.fetch(
            package=args.package,
            version=args.version,
            output_dir=output_dir,
            registries=args.registries,
            rhtl_only=args.rhtl_only,
        )
        logger.info(f"Successfully fetched source archive: {sdist_path}")
        return 0
    except Exception as e:
        logger.error(f"Failed to fetch {args.package} {args.version}: {e}")
        return 1


def handle_push(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    if not source_path.exists():
        logger.error(f"Source file does not exist: {source_path}")
        return 1

    publisher = GitMirrorPublisher(
        forge_url=args.forge_url,
        group=args.group,
        auth_token=args.auth_token,
        committer_name=args.committer_name,
        committer_email=args.committer_email,
        remote_url=args.remote_url,
    )

    try:
        tag_name = publisher.publish_source(
            source_path=source_path,
            package=args.package,
            version=args.version,
            workspace_dir=Path(args.workspace_dir),
            allow_overwrite=args.allow_overwrite,
            sign_key=args.sign_key,
            provenance_path=args.provenance_path,
            dry_run=args.dry_run,
        )
        logger.info(f"Successfully published {args.package} {args.version} with tag {tag_name}")
        return 0
    except Exception as e:
        logger.error(f"Failed to publish {args.package} {args.version}: {e}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command == "fetch":
        return handle_fetch(args)
    elif args.command == "push":
        return handle_push(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
