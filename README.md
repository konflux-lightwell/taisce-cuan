# taisce-cuan (Taisce Cúan)

`taisce-cuan` fetches and archives Python source distributions (`sdists`) into canonical `lightwell-builds` Git repositories with SLSA v1 Build Provenance metadata.

## Acquisition contract

`fetch` writes the archive and `source-origin.json` atomically in the output workspace. The sidecar preserves the plan's top-level `verified_sha256` and adds the unambiguous Fromager-compatible carrier:

```json
{"verified_sha256":"<sha256>","acquired_artifact":{"path":"sniffio-1.3.1.tar.gz","sha256":"<same sha256>"}}
```

`acquired_artifact.path` is workspace-relative and names the actual downloaded archive; its digest must equal both `verified_sha256` and the bytes consumed downstream. TLS verification remains enabled and existing files are never overwritten with different bytes.

For an RHTL artifact with a provenance URL, the opaque HTTP response is fetched byte-for-byte to the stable carrier filename `provenance-response.bin` (not the RHTL Simple Index response). The sidecar and v3 catalog binding carry matching `path`, `url`, `sha256`, and HTTP `status`; the binding rejects DSSE-looking opaque responses. v3 also supports an unavailable-provenance alternative only when the preserved PEP 691 response has no provenance field for the selected artifact. PyPI acquisitions do not require or fetch an RHTL provenance file.

## Tekton image contract

Both Tasks require deployment to supply the `image` parameter. It has no mutable default and is validated as `registry/reference@sha256:<64 lowercase hex>` before work starts. Deployment must resolve and provide a known existing registry digest (and ensure the image contains `taisce-cuan`); this repository intentionally does not invent a digest.

## CLI usage

```bash
taisce-cuan fetch sniffio 1.3.1 --output-dir /tmp/sdists
taisce-cuan push --sdist /tmp/sdists/sniffio-1.3.1.tar.gz --package sniffio --version 1.3.1 \
  --gitlab-url https://gitlab.cee.redhat.com --group lightwell/lightwell-builds --auth-token-file /var/run/secrets/gitlab/token
```
