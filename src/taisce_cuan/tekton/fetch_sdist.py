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
from pathlib import Path

from taisce_cuan.sdist import canonicalize_name
from taisce_cuan.source import SdistSourceFetcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the fetch-sdist step.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Fetch source sdist — Tekton fetch-sdist step"
    )
    parser.add_argument("package", help="Package name (e.g. sniffio)")
    parser.add_argument("version", help="Package version (e.g. 1.3.1)")
    parser.add_argument(
        "--output-dir",
        default="/var/workdir/sdists-repo",
        help="Directory to save the downloaded source archive",
    )
    parser.add_argument(
        "--registries",
        default="rhtl,pypi.org",
        help="Comma-separated ordered list of source registries",
    )
    parser.add_argument(
        "--result-path",
        help="Write the canonical sdist relative path to this file (Tekton result)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Fetch a source sdist and write the result path for the Tekton step.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        0 on success, 1 on failure.
    """
    args = create_parser().parse_args(argv)

    fetcher = SdistSourceFetcher()
    try:
        fetcher.fetch(
            package=args.package,
            version=args.version,
            output_dir=Path(args.output_dir),
            registries=args.registries,
        )
    except Exception as e:
        logger.error("Failed to fetch %s %s: %s", args.package, args.version, e)
        return 1

    if args.result_path:
        canonical = canonicalize_name(args.package)
        Path(args.result_path).write_text(
            f"downloads/{canonical}-{args.version}.tar.gz"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
