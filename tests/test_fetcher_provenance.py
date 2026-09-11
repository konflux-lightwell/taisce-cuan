import hashlib
import json
from pathlib import Path

import httpx

from taisce_cuan.fetcher import SdistFetcher


def test_rhtl_advertised_origin_binds_raw_provenance(tmp_path: Path):
    archive = b"raw sdist bytes"
    provenance = b'{"subject":"upstream"}\n'
    index_url = "https://rhtl.example/simple/demo/"
    archive_url = "https://rhtl.example/files/demo.tar.gz"
    provenance_url = "https://rhtl.example/files/provenance.json"
    index = {"files": [{"filename": "demo-1.0.tar.gz", "url": archive_url,
                         "hashes": {"sha256": hashlib.sha256(archive).hexdigest()},
                         "size": len(archive), "provenance": provenance_url}]}

    def handler(request):
        if str(request.url) == index_url:
            return httpx.Response(200, json=index, request=request)
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
    assert origin["provenance"] == {
        "advertised": True, "http_status": 200, "mode": "rhtl",
        "path": "provenance.pep740.json", "reference": "provenance.pep740.json",
        "remote_url": provenance_url, "sha256": hashlib.sha256(provenance).hexdigest(),
        "status": "advertised", "url": provenance_url,
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
