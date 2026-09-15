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

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from taisce_cuan.source import GitMirrorPublisher
from taisce_cuan.provenance.verify import adapt_rhtl_pep740, verify_blob_attestation


def test_rhtl_pep740_adaptation_preserves_base64_and_rejects_malformed(tmp_path: Path):
    raw = tmp_path / "provenance.pep740.json"
    output = tmp_path / "provenance.dsse.json"
    payload = "eyJwcmVjaXNlbHkiOiJub3QtZGVjb2RlZCJ9=="
    signature = "c2lnbmF0dXJlLXN0cmluZw=="
    raw.write_bytes(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": {
        "statement": payload, "signature": signature}}]}]}).encode())
    
    # Test direct module function
    adapt_rhtl_pep740(raw, output)
    adapted = json.loads(output.read_text())
    assert adapted == {"payloadType": "application/vnd.in-toto+json", "payload": payload,
                       "signatures": [{"sig": signature}]}
    assert json.loads(raw.read_bytes())["attestation_bundles"][0]["attestations"][0]["envelope"]["statement"] == payload

    # Test publisher facade
    output.unlink()
    GitMirrorPublisher.adapt_rhtl_pep740(raw, output)
    assert output.exists()

    raw.write_text("{malformed")
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    # Rejects multiple bundles
    raw.write_text(json.dumps({"attestation_bundles": [
        {"attestations": [{"envelope": {"statement": payload, "signature": signature}}]},
        {"attestations": [{"envelope": {"statement": payload, "signature": signature}}]},
    ]}))
    with pytest.raises(ValueError, match="bundle"):
        adapt_rhtl_pep740(raw, output)
    # Rejects multiple attestations in a bundle
    raw.write_text(json.dumps({"attestation_bundles": [
        {"attestations": [
            {"envelope": {"statement": payload, "signature": signature}},
            {"envelope": {"statement": payload, "signature": signature}},
        ]},
    ]}))
    with pytest.raises(ValueError, match="attestation"):
        adapt_rhtl_pep740(raw, output)
    # Rejects malformed nested elements (would raise AttributeError if not handled)
    raw.write_text(json.dumps({"attestation_bundles": [{"attestations": ["not-a-dict"]}]}))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    raw.write_text(json.dumps({"attestation_bundles": [{"attestations": [{"envelope": "not-a-dict"}]}]}))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    raw.write_text(json.dumps({"attestation_bundles": ["not-a-dict"]}))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)
    raw.write_text(json.dumps(["not-a-dict"]))
    with pytest.raises(ValueError, match="malformed"):
        adapt_rhtl_pep740(raw, output)


def test_verify_blob_attestation_targets_acquired_and_never_resigns(monkeypatch, tmp_path: Path):
    source = tmp_path / "downloads" / "original.tar.gz"
    signature = tmp_path / "provenance.dsse.json"
    key = tmp_path / "release3.pub"
    source.parent.mkdir(); source.write_bytes(b"original"); signature.write_text("{}\n"); key.write_text("public")
    captured = []
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", lambda command, **kwargs: captured.append(command) or SimpleNamespace(returncode=0, stderr=""))
    
    # Test direct module function
    verify_blob_attestation(source, signature, str(key))
    assert captured[0][1] == "verify-blob-attestation"
    assert captured[0][-1] == str(source)
    assert "--key" in captured[0]
    assert str(key) in captured[0]
    assert "--signature" in captured[0] or any(arg.startswith("--signature=") or arg.startswith("--bundle=") for arg in captured[0])
    assert "--type" in captured[0] or any(arg.startswith("--type=") for arg in captured[0])
    assert "attest-blob" not in captured[0]

    # Test publisher facade
    captured.clear()
    GitMirrorPublisher.verify_blob_attestation(source, signature, str(key))
    assert len(captured) == 1
    assert captured[0][1] == "verify-blob-attestation"

    # Fails closed on missing or empty key
    with pytest.raises(ValueError, match="public verification key is required"):
        verify_blob_attestation(source, signature, "")
    with pytest.raises(ValueError, match="does not exist or is not a file"):
        verify_blob_attestation(source, signature, "/non/existent/key.pub")

    # Fails closed if cosign CLI binary is missing
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="Cosign CLI is required"):
        verify_blob_attestation(source, signature, str(key))

    # Fails closed if cosign returns non-zero exit code
    monkeypatch.setattr("taisce_cuan.provenance.verify.shutil.which", lambda _: "/bin/cosign")
    monkeypatch.setattr("taisce_cuan.provenance.verify.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="signature verification failed"))
    with pytest.raises(RuntimeError, match="cosign verify-blob-attestation failed"):
        verify_blob_attestation(source, signature, str(key))
