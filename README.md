# taisce-cuan (Taisce Cúan)

`taisce-cuan` (*Repository / Preservation Harbor*) is the Lightwell tool for fetching, inspecting, and archiving Python source distributions (`sdists`) into canonical `lightwell-builds` Git repositories with SLSA v1 Build Provenance metadata.

## Overview

In the Lightwell architecture (ADR-0020 & LTWL-5395):
1. **`fetch`**: Resolves upstream Python sdists from RHTL (PEP 691 Simple Index) or PyPI, verifies SHA-256 hashes, and downloads the `.tar.gz` archive.
2. **`push`**: Takes an sdist archive (local or from a build step), unpacks it into a canonical `source/` tree, records `.lightwell/metadata.json` (SLSA Build Provenance inventory with git tree SHA), commits, tags `<canonical-pkg>/<version>`, and pushes to `gitlab.cee.redhat.com/lightwell/lightwell-builds/pypi.org-<canonical-pkg>`.

## CLI Usage

### 1. Fetching an sdist
```bash
taisce-cuan fetch sniffio 1.3.1 --output-dir /tmp/sdists
```

### 2. Ingesting & Pushing to `lightwell-builds`
```bash
taisce-cuan push \
  --sdist /tmp/sdists/sniffio-1.3.1.tar.gz \
  --package sniffio \
  --version 1.3.1 \
  --gitlab-url https://gitlab.cee.redhat.com \
  --group lightwell/lightwell-builds \
  --auth-token "$GITLAB_TOKEN"
```
