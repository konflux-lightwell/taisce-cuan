import tarfile
import pytest
from pathlib import Path
from taisce_cuan.sdist import canonicalize_name, compute_sha256, extract_sdist_to_source


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

