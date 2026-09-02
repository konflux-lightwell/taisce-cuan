# taisce-cuan (Taisce Cúan)

`taisce-cuan` (*Repository / Preservation Harbor*) is the Lightwell tool for fetching, inspecting, and archiving Python source distributions (`sdists`) into canonical `lightwell-builds` Git repositories with SLSA v1 Build Provenance metadata.

## Overview

In the Lightwell architecture (ADR-0020 & LTWL-5395):
1. **`fetch`**: Resolves upstream Python sdists from RHTL (PEP 691 Simple Index) or PyPI, verifies SHA-256 hashes, and downloads the `.tar.gz` archive.
2. **`push`**: Takes a source archive, unpacks it into a canonical repository tree, records `.lightwell/metadata.json` (SLSA Build Provenance inventory with git tree SHA), commits with SemVer branch topology, tags `<canonical-pkg>/<version>`, and pushes to the Git forge.

## CLI Usage

### 1. Fetching a Source Archive
```bash
taisce-cuan fetch sniffio 1.3.1 --output-dir /tmp/sdists
```

### 2. Ingesting & Pushing to Git Forge
```bash
taisce-cuan push \
  --source /tmp/sdists/sniffio-1.3.1.tar.gz \
  --package sniffio \
  --version 1.3.1 \
  --forge-url https://gitlab.cee.redhat.com \
  --group lightwell/lightwell-builds \
  --committer-name "Lightwell Robot" \
  --committer-email "robot@lightwell.dev" \
  --auth-token "$GIT_AUTH_TOKEN"
```

### Optional Flags
- `--sign-key <path|kms-id>`: Path to private key or KMS key URI for signing cosign attestation blobs (`cosign attest-blob`).
- `--allow-overwrite`: Allow updating an existing tag with different content (fails closed by default).
- `--remote-url <url>`: Explicit full remote Git repository URL.
- `--dry-run`: Perform all unpacking, SLSA metadata generation, git commits and tags locally without pushing.
