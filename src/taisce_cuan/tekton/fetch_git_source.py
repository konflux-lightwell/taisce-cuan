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
import sys
import tempfile
from pathlib import Path

from taisce_cuan.source import git_source

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the fetch-git-source step.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Fetch source from a git repository — Tekton fetch-git-source step"
    )
    parser.add_argument("package", help="Package name (e.g. griffe)")
    parser.add_argument("version", help="Package version (e.g. 2.3.2)")
    parser.add_argument(
        "--upstream-url",
        required=True,
        help="URL of the upstream git repository to clone",
    )
    parser.add_argument(
        "--ref",
        required=True,
        help="Git ref (tag, branch, commit) to resolve",
    )
    parser.add_argument(
        "--output-dir",
        default="/var/workdir/source-repo",
        help="Directory to write the archived git source tree",
    )
    parser.add_argument(
        "--result-path",
        help="Write the resolved commit SHA to this file (Tekton result)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Clone a git repository, resolve a ref to its commit SHA, archive the
    resolved tree, and write the resolved SHA for the Tekton step.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        0 on success, 1 on failure.
    """
    args = create_parser().parse_args(argv)
    redacted_url = git_source._redact_url_credentials(args.upstream_url)

    logger.info(
        "Fetching %s==%s from %s@%s",
        args.package,
        args.version,
        redacted_url,
        args.ref,
    )
    logger.info("Output directory: %s", args.output_dir)

    output_dir = Path(args.output_dir)
    try:
        # Land the clone (and archive_git_source_to_dir's temporary tar) under
        # the output directory's parent -- the provisioned /var/workdir
        # emptyDir in a real Tekton pod -- rather than the default system
        # temp dir, which is a small tmpfs there. See
        # archive_git_source_to_dir's docstring for why tmpfs is avoided.
        with tempfile.TemporaryDirectory(dir=output_dir.parent) as workspace_dir:
            cloned = git_source.clone_and_resolve_git_source(
                args.upstream_url,
                args.ref,
                Path(workspace_dir),
                package=args.package,
                version=args.version,
            )
            git_source.archive_git_source_to_dir(cloned, output_dir)
    except Exception as e:
        logger.error(
            "Failed to fetch %s==%s from %s@%s: %s",
            args.package,
            args.version,
            redacted_url,
            args.ref,
            e,
        )
        return 1

    logger.info(
        "Successfully fetched %s==%s from %s@%s: resolved to %s",
        args.package,
        args.version,
        redacted_url,
        args.ref,
        cloned.resolved_sha,
    )

    if args.result_path:
        Path(args.result_path).write_text(cloned.resolved_sha)
        logger.info(
            "Tekton result written to %s: %s", args.result_path, cloned.resolved_sha
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
