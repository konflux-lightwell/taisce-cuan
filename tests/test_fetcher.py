import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from taisce_cuan.fetcher import MAX_PROVENANCE_RESPONSE_BYTES, SdistFetcher


def response(status=200, body=b"exact\x00bytes", headers=None):
    value = Mock()
    value.status_code = status
    value.headers = headers or {"content-length": str(len(body))}
    value.iter_bytes.return_value = iter((body[:2], body[2:]))
    return value


def test_retrieve_provenance_streams_exact_bytes_and_digest(tmp_path: Path):
    body = b"exact\x00bytes"
    client = Mock()
    item = response(body=body)
    context = Mock()
    context.__enter__ = Mock(return_value=item)
    context.__exit__ = Mock(return_value=False)
    client.stream.return_value = context
    origin = {}

    SdistFetcher(client=client)._retrieve_provenance("https://example.test/p", tmp_path, origin)

    assert (tmp_path / "provenance-response.bin").read_bytes() == body
    assert origin["provenance_response_sha256"] == hashlib.sha256(body).hexdigest()
    client.get.assert_not_called()


def test_retrieve_provenance_rejects_oversized_content_length(tmp_path: Path):
    client = Mock()
    item = response(headers={"content-length": str(MAX_PROVENANCE_RESPONSE_BYTES + 1)})
    context = Mock()
    context.__enter__ = Mock(return_value=item)
    context.__exit__ = Mock(return_value=False)
    client.stream.return_value = context

    with pytest.raises(ValueError, match="maximum size"):
        SdistFetcher(client=client)._retrieve_provenance("https://example.test/p", tmp_path, {})
    assert not (tmp_path / "provenance-response.bin").exists()


def test_retrieve_provenance_rejects_oversized_chunked_body(tmp_path: Path):
    client = Mock()
    item = response(headers={}, body=b"")
    item.iter_bytes.return_value = iter((b"x" * (MAX_PROVENANCE_RESPONSE_BYTES + 1),))
    context = Mock()
    context.__enter__ = Mock(return_value=item)
    context.__exit__ = Mock(return_value=False)
    client.stream.return_value = context

    with pytest.raises(ValueError, match="maximum size"):
        SdistFetcher(client=client)._retrieve_provenance("https://example.test/p", tmp_path, {})
    assert not (tmp_path / "provenance-response.bin").exists()
