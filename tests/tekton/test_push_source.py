import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from taisce_cuan.tekton.push_source import (
    apply_ca_bundle,
    discover_source_path,
    export_signing_env_vars,
    main,
    read_git_auth_token,
    resolve_package_version,
    resolve_public_key,
    resolve_sign_key,
    resolve_source_path,
)

# ---------------------------------------------------------------------------
# apply_ca_bundle
# ---------------------------------------------------------------------------


def test_apply_ca_bundle_sets_ssl_env_vars(tmp_path, monkeypatch):
    ca = tmp_path / "ca-bundle.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    # Pre-register keys with monkeypatch so teardown removes them even though
    # apply_ca_bundle sets them via direct os.environ assignment.
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"):
        monkeypatch.setenv(key, "")

    apply_ca_bundle(ca)

    assert os.environ["SSL_CERT_FILE"] == str(ca)
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(ca)
    assert os.environ["GIT_SSL_CAINFO"] == str(ca)


def test_apply_ca_bundle_skips_when_file_absent(tmp_path, monkeypatch):
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"):
        monkeypatch.setenv(key, "")

    apply_ca_bundle(tmp_path / "nonexistent.crt")

    assert os.environ["SSL_CERT_FILE"] == ""


# ---------------------------------------------------------------------------
# read_git_auth_token
# ---------------------------------------------------------------------------


def test_read_git_auth_token_prefers_password_file(tmp_path):
    d = tmp_path / "git-secret"
    d.mkdir()
    (d / "password").write_text("mytoken\n")
    (d / "token").write_text("othertoken\n")

    assert read_git_auth_token(d) == "mytoken"


def test_read_git_auth_token_falls_back_to_token_file(tmp_path):
    d = tmp_path / "git-secret"
    d.mkdir()
    (d / "token").write_text("fallback\n")

    assert read_git_auth_token(d) == "fallback"


def test_read_git_auth_token_returns_none_when_absent(tmp_path):
    d = tmp_path / "git-secret"
    d.mkdir()

    assert read_git_auth_token(d) is None


# ---------------------------------------------------------------------------
# export_signing_env_vars
# ---------------------------------------------------------------------------


def test_export_signing_env_vars_sets_valid_keys(tmp_path, monkeypatch):
    d = tmp_path / "signing-secret"
    d.mkdir()
    (d / "AWS_ACCESS_KEY_ID").write_text("AKIAIOSFODNN7EXAMPLE")
    (d / "AWS_SECRET_ACCESS_KEY").write_text("wJalrXUtnFEMI")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    export_signing_env_vars(d)

    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "wJalrXUtnFEMI"


def test_export_signing_env_vars_skips_invalid_key_names(tmp_path):
    d = tmp_path / "signing-secret"
    d.mkdir()
    (d / "123invalid").write_text("value")

    export_signing_env_vars(d)

    assert "123invalid" not in os.environ


def test_export_signing_env_vars_strips_trailing_newline(tmp_path, monkeypatch):
    d = tmp_path / "signing-secret"
    d.mkdir()
    (d / "AWS_ACCESS_KEY_ID").write_text("AKIAIOSFODNN7EXAMPLE\n")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")

    export_signing_env_vars(d)

    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"


def test_export_signing_env_vars_is_noop_for_missing_dir(tmp_path):
    export_signing_env_vars(tmp_path / "no-such-dir")


# ---------------------------------------------------------------------------
# discover_source_path
# ---------------------------------------------------------------------------


def test_discover_finds_exact_match_by_name_and_version(tmp_path):
    (tmp_path / "sniffio-1.3.1.tar.gz").touch()

    expected = tmp_path / "sniffio-1.3.1.tar.gz"
    assert discover_source_path(tmp_path, "sniffio", "1.3.1") == expected


def test_discover_uses_canonical_name(tmp_path):
    (tmp_path / "my-package-2.0.0.tar.gz").touch()

    expected = tmp_path / "my-package-2.0.0.tar.gz"
    assert discover_source_path(tmp_path, "My.Package", "2.0.0") == expected


def test_discover_auto_discovers_single_archive(tmp_path):
    archive = tmp_path / "somepackage-3.0.0.tar.gz"
    archive.touch()

    assert discover_source_path(tmp_path, None, None) == archive


def test_discover_raises_on_multiple_archives(tmp_path):
    (tmp_path / "pkg1-1.0.0.tar.gz").touch()
    (tmp_path / "pkg2-2.0.0.tar.gz").touch()

    with pytest.raises(RuntimeError, match="Multiple"):
        discover_source_path(tmp_path, None, None)


def test_discover_raises_when_no_archives_found(tmp_path):
    with pytest.raises(RuntimeError, match="Could not locate"):
        discover_source_path(tmp_path, None, None)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_calls_publisher_with_explicit_source(tmp_path, monkeypatch):
    source = tmp_path / "sniffio-1.3.1.tar.gz"
    source.touch()
    monkeypatch.delenv("SIGN_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_KEY", raising=False)

    with patch("taisce_cuan.tekton.push_source.GitMirrorPublisher") as MockPublisher:
        MockPublisher.return_value.publish_source.return_value = "sniffio/1.3.1"

        rc = main(
            [
                "--source-path",
                str(source),
                "--package",
                "sniffio",
                "--version",
                "1.3.1",
                "--forge-url",
                "https://gitlab.example.com",
                "--group",
                "lightwell/builds",
                "--committer-name",
                "Bot",
                "--committer-email",
                "bot@example.com",
                "--workspace-dir",
                str(tmp_path / "work"),
                "--git-secret-dir",
                str(tmp_path / "nosecrets"),
                "--signing-secret-dir",
                str(tmp_path / "nosigning"),
                "--ca-bundle",
                str(tmp_path / "noca.crt"),
            ]
        )

    assert rc == 0
    MockPublisher.return_value.publish_source.assert_called_once()


def test_main_prefers_forge_url_over_gitlab_url(tmp_path, monkeypatch):
    source = tmp_path / "pkg-1.0.tar.gz"
    source.touch()
    monkeypatch.delenv("SIGN_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_KEY", raising=False)

    with patch("taisce_cuan.tekton.push_source.GitMirrorPublisher") as MockPublisher:
        MockPublisher.return_value.publish_source.return_value = "pkg/1.0"

        main(
            [
                "--source-path",
                str(source),
                "--package",
                "pkg",
                "--version",
                "1.0",
                "--forge-url",
                "https://new.example.com",
                "--gitlab-url",
                "https://old.example.com",
                "--group",
                "org/builds",
                "--committer-name",
                "Bot",
                "--committer-email",
                "bot@example.com",
                "--workspace-dir",
                str(tmp_path / "work"),
                "--git-secret-dir",
                str(tmp_path / "nosecrets"),
                "--signing-secret-dir",
                str(tmp_path / "nosigning"),
                "--ca-bundle",
                str(tmp_path / "noca.crt"),
            ]
        )

    assert MockPublisher.call_args[1]["forge_url"] == "https://new.example.com"


def test_main_passes_tag_protection_user_ids_to_publisher(tmp_path, monkeypatch):
    source = tmp_path / "pkg-1.0.tar.gz"
    source.touch()
    monkeypatch.delenv("SIGN_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_KEY", raising=False)

    with patch("taisce_cuan.tekton.push_source.GitMirrorPublisher") as MockPublisher:
        MockPublisher.return_value.publish_source.return_value = "pkg/1.0"

        main(
            [
                "--source-path",
                str(source),
                "--package",
                "pkg",
                "--version",
                "1.0",
                "--forge-url",
                "https://gitlab.example.com",
                "--group",
                "org/builds",
                "--committer-name",
                "Bot",
                "--committer-email",
                "bot@example.com",
                "--workspace-dir",
                str(tmp_path / "work"),
                "--git-secret-dir",
                str(tmp_path / "nosecrets"),
                "--signing-secret-dir",
                str(tmp_path / "nosigning"),
                "--ca-bundle",
                str(tmp_path / "noca.crt"),
                "--tag-protection-user-ids",
                "11111,22222",
            ]
        )

    assert MockPublisher.call_args[1]["tag_protection_user_ids"] == [11111, 22222]


def test_main_omits_tag_protection_user_ids_when_empty(tmp_path, monkeypatch):
    source = tmp_path / "pkg-1.0.tar.gz"
    source.touch()
    monkeypatch.delenv("SIGN_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_KEY", raising=False)

    with patch("taisce_cuan.tekton.push_source.GitMirrorPublisher") as MockPublisher:
        MockPublisher.return_value.publish_source.return_value = "pkg/1.0"

        main(
            [
                "--source-path",
                str(source),
                "--package",
                "pkg",
                "--version",
                "1.0",
                "--forge-url",
                "https://gitlab.example.com",
                "--group",
                "org/builds",
                "--committer-name",
                "Bot",
                "--committer-email",
                "bot@example.com",
                "--workspace-dir",
                str(tmp_path / "work"),
                "--git-secret-dir",
                str(tmp_path / "nosecrets"),
                "--signing-secret-dir",
                str(tmp_path / "nosigning"),
                "--ca-bundle",
                str(tmp_path / "noca.crt"),
            ]
        )

    assert "tag_protection_user_ids" not in MockPublisher.call_args[1]


def test_main_returns_1_when_source_file_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGN_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_KEY", raising=False)

    rc = main(
        [
            "--source-path",
            str(tmp_path / "nonexistent.tar.gz"),
            "--package",
            "pkg",
            "--version",
            "1.0",
            "--forge-url",
            "https://gitlab.example.com",
            "--group",
            "org/builds",
            "--committer-name",
            "Bot",
            "--committer-email",
            "bot@example.com",
            "--workspace-dir",
            str(tmp_path / "work"),
            "--git-secret-dir",
            str(tmp_path / "nosecrets"),
            "--signing-secret-dir",
            str(tmp_path / "nosigning"),
            "--ca-bundle",
            str(tmp_path / "noca.crt"),
        ]
    )

    assert rc == 1


# ---------------------------------------------------------------------------
# resolve_sign_key
# ---------------------------------------------------------------------------


def test_resolve_sign_key_prefers_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGN_KEY", "awskms://my-key")

    assert resolve_sign_key(tmp_path) == "awskms://my-key"


def test_resolve_sign_key_falls_back_to_key_pem(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGN_KEY", raising=False)
    (tmp_path / "key.pem").touch()

    assert resolve_sign_key(tmp_path) == str(tmp_path / "key.pem")


def test_resolve_sign_key_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGN_KEY", raising=False)

    assert resolve_sign_key(tmp_path) is None


# ---------------------------------------------------------------------------
# resolve_public_key
# ---------------------------------------------------------------------------


def test_resolve_public_key_materialises_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_KEY", "-----BEGIN PUBLIC KEY-----\n")

    with resolve_public_key(tmp_path) as path:
        assert path is not None
        assert Path(path).read_text() == "-----BEGIN PUBLIC KEY-----\n"

    assert not Path(path).exists()


def test_resolve_public_key_uses_file_when_no_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_KEY", raising=False)
    pub = tmp_path / "cosign.pub"
    pub.touch()

    with resolve_public_key(tmp_path) as path:
        assert path == str(pub)


def test_resolve_public_key_yields_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_KEY", raising=False)

    with resolve_public_key(tmp_path) as path:
        assert path is None


# ---------------------------------------------------------------------------
# resolve_source_path
# ---------------------------------------------------------------------------


def test_resolve_source_path_uses_explicit_arg(tmp_path):
    source = tmp_path / "sniffio-1.3.1.tar.gz"
    source.touch()

    args = argparse.Namespace(
        source_path=str(source),
        sdist_path="",
        data_dir=str(tmp_path),
        files_dir="files",
        package="",
        version="",
    )
    assert resolve_source_path(args) == source


def test_resolve_source_path_uses_sdist_path_alias(tmp_path):
    source = tmp_path / "pkg-1.0.tar.gz"
    source.touch()

    args = argparse.Namespace(
        source_path="",
        sdist_path=str(source),
        data_dir=str(tmp_path),
        files_dir="files",
        package="",
        version="",
    )
    assert resolve_source_path(args) == source


def test_resolve_source_path_discovers_from_files_dir(tmp_path):
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    archive = files_dir / "pkg-2.0.tar.gz"
    archive.touch()

    args = argparse.Namespace(
        source_path="",
        sdist_path="",
        data_dir=str(tmp_path),
        files_dir="files",
        package="",
        version="",
    )
    assert resolve_source_path(args) == archive


def test_resolve_source_path_returns_none_when_files_dir_missing(tmp_path):
    args = argparse.Namespace(
        source_path="",
        sdist_path="",
        data_dir=str(tmp_path),
        files_dir="nonexistent",
        package="",
        version="",
    )
    assert resolve_source_path(args) is None


# ---------------------------------------------------------------------------
# resolve_package_version
# ---------------------------------------------------------------------------


def test_resolve_package_version_returns_provided_values(tmp_path):
    pkg, ver = resolve_package_version(tmp_path / "dummy.tar.gz", "mypkg", "1.0")

    assert pkg == "mypkg"
    assert ver == "1.0"
