import tarfile
import pytest
from pathlib import Path
from taisce_cuan.sdist import canonicalize_name, compute_sha256, extract_sdist_to_source


def test_canonicalize_name():
    assert canonicalize_name("Sniffio") == "sniffio"
    assert canonicalize_name("osv_client.test") == "osv-client-test"
    assert canonicalize_name("foo---bar") == "foo-bar"


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
