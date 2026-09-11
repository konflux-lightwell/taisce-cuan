import hashlib
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from taisce_cuan.fetcher import SdistFetcher
from taisce_cuan.git_mirror import GitMirrorPublisher


def test_rhtl_advertised_origin_binds_raw_provenance(tmp_path: Path):
    archive = b"raw sdist bytes"
    provenance = b'{"subject":"upstream"}\n'
    index_url = "https://rhtl.example/simple/demo/"
    archive_url = "https://rhtl.example/files/demo.tar.gz"
    provenance_url = "https://rhtl.example/files/provenance.json"
    index = {"files": [{"filename": "demo-1.0.tar.gz", "url": archive_url,
                         "hashes": {"sha256": hashlib.sha256(archive).hexdigest()},
                         "size": len(archive), "provenance": provenance_url}]}
    index_body = json.dumps(index).encode()

    def handler(request):
        if str(request.url) == index_url:
            return httpx.Response(200, content=index_body, headers={"content-type": "application/json"}, request=request)
        if str(request.url) == archive_url:
            return httpx.Response(200, content=archive, request=request)
        if str(request.url) == provenance_url:
            return httpx.Response(200, content=provenance, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = SdistFetcher("https://rhtl.example/simple", "https://pypi.example/pypi", client=client)
    fetcher.fetch("demo", "1.0", tmp_path, registries="rhtl")
    origin = json.loads((tmp_path / "source-origin.json").read_text())
    assert origin["acquired"]["path"] == "downloads/demo-1.0.tar.gz"
    assert (tmp_path / "downloads/demo-1.0.tar.gz").read_bytes() == archive
    assert (tmp_path / "provenance.pep740.json").read_bytes() == provenance
    assert (tmp_path / "rhtl-index.pep691.json").read_bytes() == index_body
    assert origin["provenance"] == {
        "advertised": True, "http_status": 200, "mode": "rhtl",
        "path": "provenance.pep740.json", "reference": "provenance.pep740.json",
        "remote_url": provenance_url, "sha256": hashlib.sha256(provenance).hexdigest(),
        "status": "advertised", "url": provenance_url,
        "rhtl": {
            "status": "advertised",
            "evidence": {
                "path": "rhtl-index.pep691.json",
                "reason": None,
                "sha256": hashlib.sha256(index_body).hexdigest(),
                "status": 200,
                "url": index_url,
            },
        },
    }


def test_rhtl_unavailable_provenance_binds_index_and_reason(tmp_path: Path):
    archive = b"raw sdist bytes"
    index_url = "https://rhtl.example/simple/demo/"
    archive_url = "https://rhtl.example/files/demo.tar.gz"
    index_body = json.dumps({"files": [{"filename": "demo-1.0.tar.gz", "url": archive_url,
        "hashes": {"sha256": hashlib.sha256(archive).hexdigest()}, "size": len(archive),
        "provenance": None}]}).encode()

    def handler(request):
        if str(request.url) == index_url:
            return httpx.Response(200, content=index_body, headers={"content-type": "application/json"}, request=request)
        if str(request.url) == archive_url:
            return httpx.Response(200, content=archive, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = SdistFetcher("https://rhtl.example/simple", "https://pypi.example/pypi", client=client)
    fetcher.fetch("demo", "1.0", tmp_path, registries="rhtl")
    origin = json.loads((tmp_path / "source-origin.json").read_text())
    assert (tmp_path / "rhtl-index.pep691.json").read_bytes() == index_body
    assert not (tmp_path / "provenance.pep740.json").exists()
    assert origin["provenance"] == {
        "advertised": False, "mode": "rhtl", "path": "rhtl-index.pep691.json",
        "reference": "rhtl-index.pep691.json", "sha256": None,
        "status": "not-advertised", "url": None,
        "rhtl": {"status": "not-advertised", "evidence": {
            "path": "rhtl-index.pep691.json", "reason": "not-advertised",
            "sha256": hashlib.sha256(index_body).hexdigest(), "status": 200, "url": index_url,
        }},
    }
    # The index evidence remains available in the nested RHTL record.
    assert origin["provenance"]["rhtl"]["evidence"] == {
        "path": "rhtl-index.pep691.json", "reason": "not-advertised", "sha256": hashlib.sha256(index_body).hexdigest(),
        "status": 200, "url": index_url,
    }


def test_pypi_only_omits_rhtl_evidence_path(tmp_path: Path):
    archive = b"raw sdist bytes"
    archive_url = "https://pypi.example/files/demo-1.0.tar.gz"
    pypi_data = {"urls": [{"filename": "demo-1.0.tar.gz", "url": archive_url,
                           "digests": {"sha256": hashlib.sha256(archive).hexdigest()}, "size": len(archive)}]}

    def handler(request):
        if str(request.url) == "https://pypi.example/pypi/demo/1.0/json":
            return httpx.Response(200, json=pypi_data, request=request)
        if str(request.url) == archive_url:
            return httpx.Response(200, content=archive, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = SdistFetcher("https://rhtl.example/simple", "https://pypi.example/pypi", client=client)
    fetcher.fetch("demo", "1.0", tmp_path, registries="pypi.org")
    assert not (tmp_path / "rhtl-index.pep691.json").exists()
    assert not (tmp_path / "provenance.pep740.json").exists()
    origin = json.loads((tmp_path / "source-origin.json").read_text())
    assert origin["provenance"]["mode"] == "pypi"
    assert origin["provenance"]["rhtl"] == {
        "status": "not-advertised",
        "evidence": {
            "path": None,
            "reason": None,
            "sha256": None,
            "status": None,
            "url": None,
        },
    }


def test_fetcher_resets_state_between_calls(tmp_path: Path):
    archive_a = b"archive a"
    archive_b = b"archive b"
    rhtl_url = "https://rhtl.example/simple/demo/"
    archive_a_url = "https://rhtl.example/files/demo-1.0.tar.gz"
    archive_b_url = "https://pypi.example/files/other-2.0.tar.gz"
    rhtl_index = json.dumps({"files": [{"filename": "demo-1.0.tar.gz", "url": archive_a_url,
        "hashes": {"sha256": hashlib.sha256(archive_a).hexdigest()}, "size": len(archive_a),
        "provenance": None}]}).encode()
    pypi_data = {"urls": [{"filename": "other-2.0.tar.gz", "url": archive_b_url,
                           "digests": {"sha256": hashlib.sha256(archive_b).hexdigest()}, "size": len(archive_b)}]}

    def handler(request):
        if str(request.url) == rhtl_url:
            return httpx.Response(200, content=rhtl_index, headers={"content-type": "application/json"}, request=request)
        if str(request.url) == archive_a_url:
            return httpx.Response(200, content=archive_a, request=request)
        if str(request.url) == "https://pypi.example/pypi/other/2.0/json":
            return httpx.Response(200, json=pypi_data, request=request)
        if str(request.url) == archive_b_url:
            return httpx.Response(200, content=archive_b, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = SdistFetcher("https://rhtl.example/simple", "https://pypi.example/pypi", client=client)

    dir_a = tmp_path / "a"
    fetcher.fetch("demo", "1.0", dir_a, registries="rhtl")
    assert (dir_a / "rhtl-index.pep691.json").exists()

    dir_b = tmp_path / "b"
    fetcher.fetch("other", "2.0", dir_b, registries="pypi.org")
    assert not (dir_b / "rhtl-index.pep691.json").exists()
    origin_b = json.loads((dir_b / "source-origin.json").read_text())
    assert origin_b["provenance"]["rhtl"]["evidence"]["path"] is None
    assert origin_b["provenance"]["rhtl"]["evidence"]["sha256"] is None
    assert origin_b["provenance"]["rhtl"]["evidence"]["url"] is None
    assert origin_b["provenance"]["rhtl"]["evidence"]["status"] is None


@pytest.mark.parametrize("invalid_prov", ["", "   ", "http://insecure.example/prov.json", False, {"nested": "dict"}])
def test_rhtl_malformed_provenance_raises_error(tmp_path: Path, invalid_prov):
    archive = b"raw sdist bytes"
    index_url = "https://rhtl.example/simple/demo/"
    archive_url = "https://rhtl.example/files/demo.tar.gz"
    index = {"files": [{"filename": "demo-1.0.tar.gz", "url": archive_url,
                         "hashes": {"sha256": hashlib.sha256(archive).hexdigest()},
                         "size": len(archive), "provenance": invalid_prov}]}

    def handler(request):
        if str(request.url) == index_url:
            return httpx.Response(200, json=index, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = SdistFetcher("https://rhtl.example/simple", "https://pypi.example/pypi", client=client)
    with pytest.raises(ValueError):
        fetcher.fetch("demo", "1.0", tmp_path, registries="rhtl")
    assert not (tmp_path / "downloads").exists()
    assert not (tmp_path / "rhtl-index.pep691.json").exists()
    assert not (tmp_path / "source-origin.json").exists()


def test_git_mirror_advertised_rhtl_evidence_digest_validation(tmp_path: Path):
    carrier = tmp_path / "carrier"
    downloads = carrier / "downloads"
    downloads.mkdir(parents=True)
    sdist = downloads / "demo-1.0.tar.gz"

    pkg_dir = carrier / "src_demo"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text("[project]\nname='demo'\nversion='1.0'\n")
    with tarfile.open(sdist, "w:gz") as tar:
        tar.add(pkg_dir, arcname="demo-1.0")

    archive = sdist.read_bytes()
    archive_sha = hashlib.sha256(archive).hexdigest()
    prov_bytes = b'{"subject":"upstream"}\n'
    prov_sha = hashlib.sha256(prov_bytes).hexdigest()
    index_bytes = b'{"files":[]}\n'
    index_sha = hashlib.sha256(index_bytes).hexdigest()

    (carrier / "rhtl-index.pep691.json").write_bytes(index_bytes)
    (carrier / "provenance.pep740.json").write_bytes(prov_bytes)

    origin = {
        "schema": "https://lightwell.dev/schemas/source-origin/v1",
        "acquired": {"registry": "rhtl", "url": "https://rhtl.example/demo.tar.gz", "sha256": archive_sha,
                     "size": len(archive), "package": "demo", "version": "1.0", "path": "downloads/demo-1.0.tar.gz"},
        "provenance": {
            "mode": "rhtl", "status": "advertised", "advertised": True,
            "path": "provenance.pep740.json", "reference": "provenance.pep740.json",
            "sha256": prov_sha, "url": "https://rhtl.example/provenance.json",
            "rhtl": {
                "status": "advertised",
                "evidence": {
                    "path": "rhtl-index.pep691.json", "sha256": index_sha,
                    "status": 200, "url": "https://rhtl.example/simple/demo/", "reason": None,
                },
            },
        },
    }
    (carrier / "source-origin.json").write_text(json.dumps(origin, sort_keys=True) + "\n")
    (carrier / "sdist-transformation.json").write_text(json.dumps({
        "schema": "https://lightwell.dev/schemas/sdist-transformation/v1",
        "input": {"sha256": archive_sha},
        "output": {"sha256": archive_sha, "path": "demo-1.0.tar.gz"},
        "source_origin_sha256": hashlib.sha256((carrier / "source-origin.json").read_bytes()).hexdigest(),
        "transformation": "normalized-sdist",
    }, sort_keys=True) + "\n")
    normalized_sdist = carrier / "demo-1.0.tar.gz"
    normalized_sdist.write_bytes(archive)

    publisher = GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup")

    # If rhtl-index.pep691.json is tampered, publish_source must fail closed
    (carrier / "rhtl-index.pep691.json").write_bytes(b"tampered index bytes")
    with pytest.raises(ValueError, match="rhtl-index.pep691.json does not match source-origin provenance digest"):
        publisher.publish_source(source_path=normalized_sdist, package="demo", version="1.0",
                                 workspace_dir=tmp_path / "ws", dry_run=True)

    # Restore valid index bytes; dry run succeeds
    (carrier / "rhtl-index.pep691.json").write_bytes(index_bytes)
    publisher.publish_source(source_path=normalized_sdist, package="demo", version="1.0",
                             workspace_dir=tmp_path / "ws", dry_run=True)
