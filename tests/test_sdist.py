import tarfile
import pytest
from pathlib import Path
from taisce_cuan.sdist import canonicalize_name, compute_sha256, extract_sdist_to_source
from taisce_cuan.fetcher import SdistSourceInfo, provenance_state


def test_canonicalize_name():
    assert canonicalize_name("Sniffio") == "sniffio"
    assert canonicalize_name("osv_client.test") == "osv-client-test"
    assert canonicalize_name("foo---bar") == "foo-bar"


def test_fetcher_registries_parsing(tmp_path: Path):
    from taisce_cuan.fetcher import SdistFetcher
    fetcher = SdistFetcher()
    try:
        fetcher.fetch("non-existent-pkg-xyz", "0.0.1", tmp_path, registries="rhtl,pypi.org")
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "could not be resolved" in str(e)


def test_fetcher_unrecognized_registry_raises(tmp_path: Path):
    from taisce_cuan.fetcher import SdistFetcher
    fetcher = SdistFetcher()
    with pytest.raises(ValueError, match="Unrecognized registry 'unknown-registry'"):
        fetcher.fetch("requests", "2.31.0", tmp_path, registries="unknown-registry")

    with pytest.raises(ValueError, match="Unrecognized registry 'foobar'"):
        fetcher.fetch("requests", "2.31.0", tmp_path, registries=["rhtl", "foobar"])



def test_extract_sdist_and_sha256(tmp_path: Path):
    sdist_file = tmp_path / "testpkg-1.0.0.tar.gz"
    source_dir = tmp_path / "source"
    
    # Create dummy sdist
    pkg_dir = tmp_path / "testpkg-1.0.0"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text("[project]\nname='testpkg'\n")
    (pkg_dir / "testpkg.py").write_text("print('hello')\n")

    with tarfile.open(sdist_file, "w:gz") as tar:
        tar.add(pkg_dir, arcname="testpkg-1.0.0")

    digest = compute_sha256(sdist_file)
    assert len(digest) == 64

    root_name = extract_sdist_to_source(sdist_file, source_dir)
    assert root_name == "testpkg-1.0.0"
    assert (source_dir / "pyproject.toml").exists()
    assert (source_dir / "testpkg.py").exists()


def test_extract_sdist_path_traversal_rejection(tmp_path: Path):
    evil_sdist = tmp_path / "evil-1.0.0.tar.gz"
    source_dir = tmp_path / "source"
    
    with tarfile.open(evil_sdist, "w:gz") as tar:
        info = tarfile.TarInfo(name="../evil.txt")
        data = b"malicious content"
        import io
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises(ValueError, match="Dangerous path traversal"):
        extract_sdist_to_source(evil_sdist, source_dir)


def test_extract_zip_sdist_and_zip_slip_prevention(tmp_path: Path):
    import zipfile
    source_dir = tmp_path / "source"

    # 1. Test safe zip extraction
    safe_zip = tmp_path / "my_zip_pkg-1.0.0.zip"
    with zipfile.ZipFile(safe_zip, "w") as zf:
        zf.writestr("my_zip_pkg-1.0.0/pyproject.toml", "[project]\nname='my-zip-pkg'\n")
        zf.writestr("my_zip_pkg-1.0.0/my_zip_pkg.py", "print('hello from zip')\n")

    root_name = extract_sdist_to_source(safe_zip, source_dir)
    assert root_name == "my_zip_pkg-1.0.0"
    assert (source_dir / "pyproject.toml").exists()
    assert (source_dir / "my_zip_pkg.py").exists()

    # 2. Test zip-slip path traversal rejection
    evil_zip = tmp_path / "evil-1.0.0.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../evil.txt", "malicious content")

    with pytest.raises(ValueError, match="Dangerous path traversal zip entry"):
        extract_sdist_to_source(evil_zip, source_dir)


def test_fetcher_missing_sha256_fail_closed(tmp_path: Path):
    from taisce_cuan.fetcher import SdistFetcher, SdistSourceInfo
    fetcher = SdistFetcher()
    # Mock query_rhtl returning entry with empty sha256
    fetcher.query_rhtl = lambda pkg, ver: SdistSourceInfo(
        registry="rhtl",
        download_url="https://example.com/foo.tar.gz",
        sha256="",
        size=100,
        upload_time=None,
        provenance_url=None,
    )
    with pytest.raises(ValueError, match="No SHA-256 digest provided"):
        fetcher.fetch("foo", "1.0.0", tmp_path, registries="rhtl")


def _source_info(registry: str, provenance_url):
    return SdistSourceInfo(
        registry=registry,
        download_url="https://example.com/foo.tar.gz",
        sha256="deadbeef",
        size=100,
        upload_time=None,
        provenance_url=provenance_url,
    )


def test_provenance_state_not_advertised_only_when_none():
    assert provenance_state(_source_info("rhtl", None)) == "not-advertised"
    assert provenance_state(_source_info("pypi.org", None)) == "not-advertised"


def test_provenance_state_advertised_for_any_registry():
    prov = "https://example.com/foo.tar.gz.provenance"
    assert provenance_state(_source_info("rhtl", prov)) == "advertised"
    assert provenance_state(_source_info("pypi.org", prov)) == "advertised"


@pytest.mark.parametrize(
    "bad_url",
    ["", "   ", "not a url", "http://example.com/prov", "ftp://example.com/prov"],
)
def test_provenance_state_rejects_malformed_url(bad_url):
    with pytest.raises(ValueError, match="Invalid provenance URL"):
        provenance_state(_source_info("rhtl", bad_url))


def test_inspect_sdist_metadata_tarball(tmp_path: Path):
    from taisce_cuan.sdist import inspect_sdist_metadata
    sdist_file = tmp_path / "my-cool-package-2.4.1.tar.gz"
    pkg_dir = tmp_path / "my-cool-package-2.4.1"
    pkg_dir.mkdir()
    (pkg_dir / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: my-cool-package\nVersion: 2.4.1\n")
    with tarfile.open(sdist_file, "w:gz") as tar:
        tar.add(pkg_dir, arcname="my-cool-package-2.4.1")

    pkg, ver = inspect_sdist_metadata(sdist_file)
    assert pkg == "my-cool-package"
    assert ver == "2.4.1"


def test_inspect_sdist_metadata_zip(tmp_path: Path):
    import zipfile
    from taisce_cuan.sdist import inspect_sdist_metadata
    zip_sdist = tmp_path / "zip-pkg-3.1.4.zip"
    with zipfile.ZipFile(zip_sdist, "w") as zf:
        zf.writestr("zip-pkg-3.1.4/PKG-INFO", "Metadata-Version: 2.1\nName: zip-pkg\nVersion: 3.1.4\n")

    pkg, ver = inspect_sdist_metadata(zip_sdist)
    assert pkg == "zip-pkg"
    assert ver == "3.1.4"


def test_inspect_sdist_metadata_fallback_filename(tmp_path: Path):
    from taisce_cuan.sdist import inspect_sdist_metadata
    sdist_file = tmp_path / "fallback_pkg-0.10.2b1.tar.gz"
    pkg_dir = tmp_path / "other-dir"
    pkg_dir.mkdir()
    with tarfile.open(sdist_file, "w:gz") as tar:
        tar.add(pkg_dir, arcname="other-dir")

    pkg, ver = inspect_sdist_metadata(sdist_file)
    assert pkg == "fallback_pkg"
    assert ver == "0.10.2b1"


