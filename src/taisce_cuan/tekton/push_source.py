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
import contextlib
import logging
import os
import re
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

from taisce_cuan.sdist import canonicalize_name, inspect_sdist_metadata
from taisce_cuan.source import GitMirrorPublisher

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_ARCHIVE_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".zip")


def apply_ca_bundle(ca_bundle: Path) -> None:
    """Set SSL environment variables to the given CA bundle if it exists.

    Args:
        ca_bundle: Path to the CA bundle file. No-op when the file does not exist.
    """
    if not ca_bundle.is_file():
        return
    path = str(ca_bundle)
    os.environ["SSL_CERT_FILE"] = path
    os.environ["REQUESTS_CA_BUNDLE"] = path
    os.environ["GIT_SSL_CAINFO"] = path


def read_git_auth_token(git_secret_dir: Path) -> str | None:
    """Read a Git auth token from a mounted Kubernetes secret directory.

    Checks for a ``password`` file first, then falls back to ``token``.

    Args:
        git_secret_dir: Directory containing the mounted Git secret files.

    Returns:
        Stripped token string, or ``None`` if no token file is found.
    """
    for name in ("password", "token"):
        f = git_secret_dir / name
        if f.is_file():
            return f.read_text().strip()
    return None


def export_signing_env_vars(signing_secret_dir: Path) -> None:
    """Export each file in the signing secret directory as an environment variable.

    Files whose names are not valid environment variable identifiers are skipped.
    Trailing newlines are stripped to match bash ``$(cat file)`` behaviour.

    Args:
        signing_secret_dir: Directory containing signing secret files. No-op when
            the directory does not exist.
    """
    if not signing_secret_dir.is_dir():
        return
    for secret_file in signing_secret_dir.iterdir():
        if not secret_file.is_file():
            continue
        key = secret_file.name
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            os.environ[key] = secret_file.read_text().rstrip("\n")


def discover_source_path(
    files_dir: Path,
    package: str | None,
    version: str | None,
) -> Path:
    """Locate the source archive in a directory.

    When package and version are given, the search is limited to archives whose
    stem matches the canonical or original package name. When they are omitted,
    all archives are collected and the single result returned. Ambiguity or the
    absence of any archive raises ``RuntimeError``.

    Args:
        files_dir: Directory to search for source archives.
        package: Package name used to filter candidates, or ``None`` for
            auto-discovery.
        version: Package version used to filter candidates, or ``None`` for
            auto-discovery.

    Returns:
        Path to the located source archive.

    Raises:
        RuntimeError: When no archive is found, or when multiple archives match
            and the result is ambiguous.
    """
    if package and version:
        canonical = canonicalize_name(package)
        candidates: list[Path] = []
        for ext in _ARCHIVE_EXTENSIONS:
            for stem in dict.fromkeys((canonical, package)):
                f = files_dir / f"{stem}-{version}{ext}"
                if f.is_file() and f not in candidates:
                    candidates.append(f)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(
                f"Multiple matching archives found for {package}=={version}: "
                + ", ".join(str(c) for c in candidates)
            )

    all_archives = [
        f
        for f in files_dir.iterdir()
        if f.is_file() and f.name.endswith(_ARCHIVE_EXTENSIONS)
    ]
    if len(all_archives) == 1:
        return all_archives[0]
    if len(all_archives) > 1:
        raise RuntimeError(
            f"Multiple sdist archives found in {files_dir}. Ambiguous autodiscovery."
        )
    raise RuntimeError(f"Could not locate any sdist archive in {files_dir}")


def resolve_source_path(args: argparse.Namespace) -> Path | None:
    """Return the source archive path from explicit args or directory discovery.

    Args:
        args: Parsed argument namespace. Reads ``source_path``, ``sdist_path``,
            ``data_dir``, ``files_dir``, ``package``, and ``version``.

    Returns:
        Resolved archive path, or ``None`` when discovery fails.
    """
    explicit = args.source_path or args.sdist_path
    if explicit:
        return Path(explicit)

    files_dir = Path(args.data_dir) / args.files_dir
    if not files_dir.is_dir():
        logger.error("Files directory does not exist: %s", files_dir)
        return None
    try:
        return discover_source_path(
            files_dir,
            args.package or None,
            args.version or None,
        )
    except RuntimeError as e:
        logger.error("%s", e)
        return None


def resolve_sign_key(signing_secret_dir: Path) -> str | None:
    """Return a sign key from ``SIGN_KEY`` or mounted secret files.

    Checks ``SIGN_KEY`` first, then falls back to ``key.pem`` and ``cosign.key``
    in the signing secret directory.

    Args:
        signing_secret_dir: Directory containing mounted signing secret files.

    Returns:
        Sign key string or path, or ``None`` when no key is found.
    """
    key = os.environ.get("SIGN_KEY", "").strip()
    if key:
        return key
    for fname in ("key.pem", "cosign.key"):
        f = signing_secret_dir / fname
        if f.is_file():
            return str(f)
    return None


@contextlib.contextmanager
def resolve_public_key(signing_secret_dir: Path) -> Generator[str | None, None, None]:
    """Yield the public verification key path, materialising ``PUBLIC_KEY`` if set.

    When the ``PUBLIC_KEY`` environment variable contains PEM content, it is
    written to a temporary file so cosign can receive a file path. The file is
    removed on exit regardless of whether an exception is raised.

    When ``PUBLIC_KEY`` is unset, the context manager looks for ``public.pem``
    then ``cosign.pub`` in the signing secret directory. Yields ``None`` when no
    key is found anywhere.

    Args:
        signing_secret_dir: Directory containing mounted signing secret files.

    Yields:
        Path string to the public key file, or ``None`` when no key is available.
    """
    public_key_env = os.environ.get("PUBLIC_KEY", "")
    tmpfile: str | None = None
    try:
        if public_key_env.strip():
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".pem", mode="w"
            ) as tmp:
                tmp.write(public_key_env)
                tmpfile = tmp.name
            yield tmpfile
        else:
            for fname in ("public.pem", "cosign.pub"):
                f = signing_secret_dir / fname
                if f.is_file():
                    yield str(f)
                    return
            yield None
    finally:
        if tmpfile:
            Path(tmpfile).unlink(missing_ok=True)


def resolve_package_version(
    source_path: Path,
    package: str | None,
    version: str | None,
) -> tuple[str, str]:
    """Return the package name and version, auto-discovering from the archive if needed.

    Args:
        source_path: Path to the source archive, used for metadata inspection when
            package or version are not provided.
        package: Explicit package name, or ``None`` to auto-discover.
        version: Explicit package version, or ``None`` to auto-discover.

    Returns:
        A ``(package, version)`` tuple with resolved values.
    """
    if package and version:
        return package, version
    disc_pkg, disc_ver = inspect_sdist_metadata(source_path)
    resolved_pkg = package or disc_pkg
    resolved_ver = version or disc_ver
    logger.info("Auto-discovered %s==%s from archive", resolved_pkg, resolved_ver)
    return resolved_pkg, resolved_ver


def create_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the push-source step.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Push source archive to Git forge — Tekton push-source step"
    )
    parser.add_argument("--source-path", default="", dest="source_path")
    parser.add_argument(
        "--sdist-path",
        default="",
        dest="sdist_path",
        help="Legacy alias for --source-path",
    )
    parser.add_argument("--data-dir", default="/var/workdir")
    parser.add_argument("--files-dir", default="files")
    parser.add_argument("--package", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--forge-url", default="", dest="forge_url")
    parser.add_argument(
        "--gitlab-url",
        default="",
        dest="gitlab_url",
        help="Legacy alias for --forge-url",
    )
    parser.add_argument("--group", default="")
    parser.add_argument(
        "--gitlab-group",
        default="",
        dest="gitlab_group",
        help="Legacy alias for --group",
    )
    parser.add_argument("--committer-name", required=True)
    parser.add_argument("--committer-email", required=True)
    parser.add_argument("--remote-url", default="", dest="remote_url")
    parser.add_argument("--workspace-dir", default="/var/workdir/work")
    parser.add_argument("--rhtl-predicate-type", default="", dest="rhtl_predicate_type")
    parser.add_argument("--provenance-path", default="", dest="provenance_path")
    parser.add_argument("--ca-bundle", default="/mnt/trusted-ca/ca-bundle.crt")
    parser.add_argument("--git-secret-dir", default="/etc/git-secret")
    parser.add_argument("--signing-secret-dir", default="/etc/signing-secret")
    parser.add_argument(
        "--tag-protection-user-ids",
        default="",
        dest="tag_protection_user_ids",
        help="Comma-separated list of GitLab user IDs allowed to create protected tags",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Push a source archive to a Git forge — Tekton push-source step.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        0 on success, 1 on failure.
    """
    args = create_parser().parse_args(argv)

    apply_ca_bundle(Path(args.ca_bundle))

    source_path = resolve_source_path(args)
    if source_path is None or not source_path.is_file():
        logger.error("Source archive does not exist: %s", source_path)
        return 1

    signing_secret_dir = Path(args.signing_secret_dir)
    auth_token = read_git_auth_token(Path(args.git_secret_dir))
    export_signing_env_vars(signing_secret_dir)
    sign_key = resolve_sign_key(signing_secret_dir)

    try:
        package, version = resolve_package_version(
            source_path,
            args.package or None,
            args.version or None,
        )
    except Exception as e:
        logger.error("Failed to discover package metadata: %s", e)
        return 1

    forge_url = args.forge_url or args.gitlab_url
    group = args.group or args.gitlab_group

    publisher_kwargs: dict = {
        "committer_name": args.committer_name,
        "committer_email": args.committer_email,
    }
    if forge_url:
        publisher_kwargs["forge_url"] = forge_url
    if group:
        publisher_kwargs["group"] = group
    if auth_token:
        publisher_kwargs["auth_token"] = auth_token
    if args.remote_url:
        publisher_kwargs["remote_url"] = args.remote_url
    if args.tag_protection_user_ids:
        publisher_kwargs["tag_protection_user_ids"] = [
            int(x.strip()) for x in args.tag_protection_user_ids.split(",") if x.strip()
        ]

    publisher = GitMirrorPublisher(**publisher_kwargs)

    with resolve_public_key(signing_secret_dir) as public_key:
        try:
            tag_name = publisher.publish_source(
                source_path=source_path,
                package=package,
                version=version,
                workspace_dir=Path(args.workspace_dir),
                sign_key=sign_key,
                provenance_path=(
                    Path(args.provenance_path) if args.provenance_path else None
                ),
                public_key=public_key,
                rhtl_predicate_type=args.rhtl_predicate_type or None,
                dry_run=args.dry_run,
            )
            logger.info("Published with tag %s", tag_name)
            return 0
        except Exception as e:
            logger.error("Failed to push source: %s", e)
            return 1


if __name__ == "__main__":
    sys.exit(main())
