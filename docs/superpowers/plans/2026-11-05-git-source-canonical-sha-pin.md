# Git-Source `canonical_source_sha` Ingestion Pin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `GitMirrorPublisher` a git-source publishing path that resolves a `canonical_source.ref` to its exact upstream commit SHA and writes it as a top-level `canonical_source_sha` key in `.lightwell/metadata.json`, matching the field name/shape balor-fianna!2098's already-merged read-side gate expects — then pilot it end-to-end against a real package (griffe) to prove sync → pin → gate works, including failing closed on an induced mismatch.

**Architecture:** `GitMirrorPublisher` (`src/taisce_cuan/source/mirror.py`) currently has exactly one content-acquisition path, `publish()`, hard-wired to the sdist/fromager/RHTL verification pipeline (`provenance/verify.py`'s `verify_normalized_source_artifact`) — there is no sdist archive, transformation record, or PyPI/RHTL evidence for a git-sourced package, so that path cannot be reused as-is. This plan adds a parallel, independent path: a new `source/git_source.py` module clones the upstream repo and resolves `ref` to a commit SHA, a new `build_git_ingestion_metadata()` in `provenance/attest.py` builds the metadata (setting the new `canonical_source_sha` field), and a new `GitMirrorPublisher.publish_git_source()` method reuses the existing git/tag/push/idempotency mechanics already proven by `publish()` (`get_existing_tags`, `check_existing_tag_content`, `ensure_remote_project`, the `<canonical>/<version>` + `baseline/<version>` tagging convention). A new `push-git-source` CLI subcommand exposes it for the manual pilot.

**Deferred / explicit follow-on (not in this plan):** wiring this into calunga-python's automatic CI ingestion (filtering `new-refs.txt` by `canonical_source.type`), any Konflux/Tekton pipeline or `archive-sources` task-YAML param, and cosign signing support for the git-source path. Scoped out per the user's explicit choice to keep this plan to the core writer + a manual pilot; calunga-python's onboarding gate (`!157`) stays blocked until that follow-on work (tracked separately) ships.

**Tech Stack:** Python 3.12+, pydantic v2, `git` CLI via `subprocess`, pytest, `httpx` (already a dependency for GitLab API calls).

**Spec:** [LWLP-3402](https://redhat.atlassian.net/browse/LWLP-3402) ("Write canonical_source_sha ingestion pin for Python git-source syncing"); gate contract defined by `balor-fianna`'s `fetch_target_tree/python_source.py:232-262` (`_verify_python_mirror_base`) and `stage.py:696-749` (`_fetch_canonical_source_pin`), covered by `tests/test_pipeline/test_stages/fetch_target_tree/test_python.py::TestCanonicalSourcePin`.

## Global Constraints

- `canonical_source_sha` must be a non-empty string at the **top level** of `.lightwell/metadata.json` — a sibling of `_type`/`predicateType`/`subject`/`predicate`, never nested under `predicate` — because `_fetch_canonical_source_pin` reads `parsed.get("canonical_source_sha")` on the whole parsed document.
- The pin must be present at the commit tagged `<canonical_name>/<version>`, and that same commit must also be tagged `baseline/<version>` (first ingestion only) — the existing ADR-0005 baseline-anchor convention `publish()` already follows; balor-fianna's gate and other baseline-tag consumers depend on it existing for every mirror, sdist or git.
- `IngestionMetadata.model_dump_json(..., exclude_none=True)` must keep omitting `canonical_source_sha` for every existing sdist-route call site — it must never appear as `null` in sdist-sourced packages' metadata.
- This plan's pilot (Task 8) must push to an explicitly user-confirmed scratch destination — never `lightwell/lightwell-builds` (production, and in any case the write-protection guard in open PR #53 is not yet merged into `main`) and never `lightwell/lightwell-builds-midstream` (reserved for the unrelated sdist-midstream effort).
- This plan does not modify calunga-python, any Tekton task/pipeline, or konflux-release-data — the git-source onboarding gate in `calunga-python!157` stays blocked exactly as it is today.

## Review Focus

- **Annotated vs. lightweight tag resolution**: `canonical_source.ref` values seen in the wild (e.g. griffe's `2.3.2`) may be lightweight or annotated tags. Resolution must always return the dereferenced **commit** SHA (`^{commit}`), never a tag-object SHA, or the pin will never match balor-fianna's `git rev-parse <ref>^{commit}` comparison.
- **Re-ingesting an already-tagged version whose upstream ref has moved** (force-push/tag retag): must raise, never silently rewrite `<canonical>/<version>` to a new SHA — same overwrite-protection contract `publish()` already enforces for sdists.
- **Unreachable upstream URL or nonexistent ref**: must fail with a clear, attributable error naming the URL/ref — never commit/push a partial or empty mirror.
- **Package name canonicalization**: a git-sourced package's `canonical_source_sha` writer must canonicalize the package name through the same `canonicalize_name()` sdist packages use, so the repo name (`pypi.org-<canonical>`) and tag (`<canonical>/<version>`) balor-fianna resolves for a git-sourced package matches exactly what this writer produces — a mismatch here means the gate queries the wrong repo/tag and never finds the pin at all (silently permissive, not failing loud).
- **Idempotent re-run with identical content**: running the git-source publish twice against the same `ref` (unchanged upstream) must be a no-op success (`disposition="already-present"`), not a duplicate-tag error — mirrors the sdist path's existing idempotency test coverage.

---

### Task 1: Add `canonical_source_sha` to `IngestionMetadata`

**Files:**
- Modify: `src/taisce_cuan/models.py:118-125` (`IngestionMetadata`)
- Create: `tests/test_models.py`

**Interfaces:**
- Produces: `IngestionMetadata(..., canonical_source_sha: str | None = None)` — new optional field on the existing model, consumed by Task 4 and read back by balor-fianna's gate as a plain top-level JSON key.

- [ ] **Step 1: Write the failing tests**

```python
import json
from taisce_cuan.models import (
    BuildDefinition, ExternalParameters, IngestionMetadata, Predicate,
    RunDetails, RunDetailsMetadata, Subject,
)

def _minimal_predicate() -> Predicate:
    return Predicate(
        buildDefinition=BuildDefinition(
            externalParameters=ExternalParameters(
                package="x", canonical_name="x", version="1.0",
            ),
        ),
        runDetails=RunDetails(
            metadata=RunDetailsMetadata(startedOn="t", finishedOn="t"),
        ),
    )

def test_canonical_source_sha_omitted_when_none():
    metadata = IngestionMetadata(
        subject=[Subject(name="x", digest={"sha256": "a" * 64})],
        predicate=_minimal_predicate(),
    )
    dumped = json.loads(metadata.model_dump_json(by_alias=True, exclude_none=True))
    assert "canonical_source_sha" not in dumped

def test_canonical_source_sha_included_when_set():
    metadata = IngestionMetadata(
        canonical_source_sha="a" * 40,
        subject=[Subject(name="x", digest={"sha256": "a" * 64})],
        predicate=_minimal_predicate(),
    )
    dumped = json.loads(metadata.model_dump_json(by_alias=True, exclude_none=True))
    assert dumped["canonical_source_sha"] == "a" * 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL (`canonical_source_sha` is not a valid keyword argument for `IngestionMetadata`)

- [ ] **Step 3: Add `canonical_source_sha: str | None = None` to `IngestionMetadata` in `src/taisce_cuan/models.py`**

No other models change.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taisce_cuan/models.py tests/test_models.py
git commit -m "models: add optional canonical_source_sha pin field"
```

---

### Task 2: Git-ref resolver

**Files:**
- Create: `src/taisce_cuan/source/git_source.py`
- Create: `tests/conftest.py` (shared `make_local_git_repo` test helper, reused by Task 5 and referenced in Task 8)
- Create: `tests/test_git_source.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ClonedGitSource:
      package: str
      version: str
      upstream_url: str
      ref: str
      resolved_sha: str
      clone_dir: Path

  def clone_and_resolve_git_source(
      upstream_url: str, ref: str, workspace_dir: Path, *, package: str, version: str,
  ) -> ClonedGitSource: ...
  ```
  Raises `RuntimeError` naming `upstream_url` and `ref` when the clone or ref checkout fails. Consumed by Task 3, Task 4, and Task 5.

- [ ] **Step 1: Write the failing tests**

In `tests/conftest.py`:

```python
import subprocess
from pathlib import Path

def make_local_git_repo(base_dir: Path, package: str, version: str, tag: str | None = None) -> Path:
    """Create a local git repo with one commit and an annotated tag; return its path."""
    repo_dir = base_dir / f"upstream-{package}-{version}"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "--initial-branch=main", "--quiet"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "pyproject.toml").write_text(f"[project]\nname='{package}'\nversion='{version}'\n")
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial", "--quiet"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "tag", "-a", tag or version, "-m", "release"], cwd=repo_dir, check=True
    )
    return repo_dir
```

In `tests/test_git_source.py`:

```python
import subprocess
from taisce_cuan.source.git_source import clone_and_resolve_git_source

def test_resolves_annotated_tag_to_commit_sha(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "1.5.1^{commit}"], cwd=upstream, text=True
    ).strip()

    cloned = clone_and_resolve_git_source(
        str(upstream), "1.5.1", tmp_path / "work", package="griffe", version="1.5.1",
    )

    assert cloned.resolved_sha == expected_sha
    assert (cloned.clone_dir / "pyproject.toml").exists()

def test_raises_on_nonexistent_ref(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    try:
        clone_and_resolve_git_source(
            str(upstream), "does-not-exist", tmp_path / "work",
            package="griffe", version="1.5.1",
        )
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "does-not-exist" in str(exc)
        assert str(upstream) in str(exc)
```

Expose `make_local_git_repo` as a pytest fixture in `conftest.py` (wrap the function above in a fixture that returns it, or register it as a plain importable helper and adjust the test signatures accordingly — either is fine as long as both this task's and Task 5's tests can use it without duplicating the implementation).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_git_source.py -v`
Expected: FAIL (`ModuleNotFoundError: taisce_cuan.source.git_source`)

- [ ] **Step 3: Implement `clone_and_resolve_git_source` in `src/taisce_cuan/source/git_source.py`**

Full (non-shallow) `git clone <upstream_url> <clone_dir>`, then `git checkout <ref>`, then `git rev-parse HEAD^{commit}` for the resolved SHA. Wrap `subprocess.CalledProcessError` from either step in a `RuntimeError` whose message includes `upstream_url` and `ref`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_git_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taisce_cuan/source/git_source.py tests/conftest.py tests/test_git_source.py
git commit -m "source: add git ref-to-commit-SHA resolver"
```

---

### Task 3: Archive the resolved git source to a content hash + tree

**Files:**
- Modify: `src/taisce_cuan/source/git_source.py` (add `archive_git_source_to_dir`)
- Modify: `tests/test_git_source.py`

**Interfaces:**
- Consumes: `ClonedGitSource` (Task 2), `compute_sha256` from `src/taisce_cuan/sdist.py`.
- Produces:
  ```python
  def archive_git_source_to_dir(cloned: ClonedGitSource, dest_source_dir: Path) -> str:
      """Write cloned.resolved_sha's tracked tree into dest_source_dir and return
      the sha256 hex digest of the `git archive` tar bytes (a content hash,
      reused by publish_git_source's idempotency check — see Task 5)."""
  ```
  `archive_git_source_to_dir`'s return value plugs into Task 4's `build_git_ingestion_metadata(..., source_sha256=...)` and Task 5's reuse of `GitMirrorPublisher.check_existing_tag_content`.

- [ ] **Step 1: Write the failing test**

```python
def test_archive_git_source_to_dir_extracts_tree_and_returns_hash(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    cloned = clone_and_resolve_git_source(
        str(upstream), "1.5.1", tmp_path / "work", package="griffe", version="1.5.1",
    )
    dest = tmp_path / "source"

    digest = archive_git_source_to_dir(cloned, dest)

    assert (dest / "pyproject.toml").exists()
    assert not (dest / ".git").exists()
    assert len(digest) == 64  # hex sha256

def test_archive_git_source_to_dir_is_deterministic_for_same_commit(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    cloned = clone_and_resolve_git_source(
        str(upstream), "1.5.1", tmp_path / "work", package="griffe", version="1.5.1",
    )
    digest_a = archive_git_source_to_dir(cloned, tmp_path / "source-a")
    digest_b = archive_git_source_to_dir(cloned, tmp_path / "source-b")
    assert digest_a == digest_b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_git_source.py -v`
Expected: FAIL (`ImportError: cannot import name 'archive_git_source_to_dir'`)

- [ ] **Step 3: Implement `archive_git_source_to_dir`**

Run `git archive --format=tar <resolved_sha>` inside `cloned.clone_dir`, write its stdout to a temporary tar file, extract that tar into `dest_source_dir`, and return `compute_sha256()` of the tar file. The tar file itself is a transient hashing artifact — do not leave it behind in `dest_source_dir` or commit it anywhere.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_git_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taisce_cuan/source/git_source.py tests/test_git_source.py
git commit -m "source: archive resolved git commit to a source tree + content hash"
```

---

### Task 4: Git-source ingestion metadata builder

**Files:**
- Modify: `src/taisce_cuan/provenance/attest.py` (add `build_git_ingestion_metadata`)
- Modify: `tests/test_provenance_attest.py`

**Interfaces:**
- Consumes: `ClonedGitSource` (Task 2), `IngestionMetadata.canonical_source_sha` (Task 1), `canonicalize_name` from `sdist.py`.
- Produces:
  ```python
  def build_git_ingestion_metadata(
      cloned: ClonedGitSource,
      repo_name: str,
      source_sha256: str,
      *,
      timestamp: datetime.datetime | None = None,
  ) -> IngestionMetadata: ...
  ```
  Consumed directly by Task 5's `publish_git_source`.

- [ ] **Step 1: Write the failing test**

```python
from taisce_cuan.provenance.attest import build_git_ingestion_metadata
from taisce_cuan.source.git_source import ClonedGitSource

def test_build_git_ingestion_metadata_sets_canonical_source_sha(tmp_path):
    cloned = ClonedGitSource(
        package="griffe", version="1.5.1",
        upstream_url="https://github.com/mkdocstrings/griffe.git",
        ref="1.5.1", resolved_sha="f" * 40, clone_dir=tmp_path,
    )

    metadata = build_git_ingestion_metadata(cloned, "pypi.org-griffe", "a" * 64)

    assert metadata.canonical_source_sha == "f" * 40
    assert metadata.predicate.buildDefinition.externalParameters.upstream_registry == "git"
    assert metadata.subject[0].digest["sha256"] == "a" * 64
    assert metadata.predicate.runDetails.metadata.lightwell_builds.source_registry_used == "git"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provenance_attest.py -v`
Expected: FAIL (`ImportError: cannot import name 'build_git_ingestion_metadata'`)

- [ ] **Step 3: Implement `build_git_ingestion_metadata` in `src/taisce_cuan/provenance/attest.py`**

Mirror `build_ingestion_metadata`'s shape: `canonical = canonicalize_name(cloned.package)`; `subject=[Subject(name=f"{canonical}-{cloned.version}.tar", digest={"sha256": source_sha256})]`; `canonical_source_sha=cloned.resolved_sha` on the returned `IngestionMetadata`; `ExternalParameters(..., upstream_registry="git")`; one `ResolvedDependency` binding `cloned.upstream_url` with `digest={"gitCommit": cloned.resolved_sha}` and `annotations={"role": "upstream-git-source", "ref": cloned.ref}`; `LightwellBuildsInfo(repo=repo_name, source_registry_used="git")`; `attestation_level="unsigned-inventory"` and a note mentioning this is a git-source ingestion (signing is out of scope for this plan — see Global Constraints).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provenance_attest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taisce_cuan/provenance/attest.py tests/test_provenance_attest.py
git commit -m "provenance: build ingestion metadata for git-sourced packages"
```

---

### Task 5: `GitMirrorPublisher.publish_git_source()`

**Files:**
- Modify: `src/taisce_cuan/provenance/attest.py` (add `PublishGitSourceRequest` dataclass, alongside `PublishSourceRequest`)
- Modify: `src/taisce_cuan/source/mirror.py` (add `publish_git_source` method)
- Modify: `tests/test_git_mirror.py`

**Interfaces:**
- Consumes: `clone_and_resolve_git_source`, `archive_git_source_to_dir` (Task 2/3), `build_git_ingestion_metadata` (Task 4), existing `GitMirrorPublisher.get_existing_tags`, `check_existing_tag_content`, `ensure_remote_project` (unchanged, already in `mirror.py`).
- Produces:
  ```python
  @dataclass(frozen=True)
  class PublishGitSourceRequest:
      upstream_url: str
      ref: str
      package: str
      version: str
      workspace_dir: Path
      dry_run: bool = False

  # on GitMirrorPublisher:
  def publish_git_source(self, request: PublishGitSourceRequest) -> PublishSourceResult: ...
  ```
  Consumed by Task 6's CLI subcommand and exercised directly in Task 8's pilot.

- [ ] **Step 1: Write the failing tests**

```python
import json
from taisce_cuan.provenance.attest import PublishGitSourceRequest
from taisce_cuan.source import GitMirrorPublisher

def test_publish_git_source_dry_run_writes_pin(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup")

    result = publisher.publish_git_source(
        PublishGitSourceRequest(
            upstream_url=str(upstream), ref="1.5.1", package="griffe", version="1.5.1",
            workspace_dir=workspace, dry_run=True,
        )
    )

    assert result.tag_name == "griffe/1.5.1"
    repo_dir = workspace / "pypi.org-griffe"
    meta = json.loads((repo_dir / ".lightwell" / "metadata.json").read_text())
    import subprocess
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "1.5.1^{commit}"], cwd=upstream, text=True
    ).strip()
    assert meta["canonical_source_sha"] == expected_sha

    canonical_commit = subprocess.check_output(
        ["git", "rev-parse", "griffe/1.5.1^{commit}"], cwd=repo_dir, text=True
    ).strip()
    baseline_commit = subprocess.check_output(
        ["git", "rev-parse", "baseline/1.5.1^{commit}"], cwd=repo_dir, text=True
    ).strip()
    assert canonical_commit == baseline_commit

def test_publish_git_source_is_idempotent(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup")
    request = PublishGitSourceRequest(
        upstream_url=str(upstream), ref="1.5.1", package="griffe", version="1.5.1",
        workspace_dir=workspace, dry_run=True,
    )

    publisher.publish_git_source(request)
    result = publisher.publish_git_source(request)

    assert result.disposition == "already-present"

def test_publish_git_source_refuses_to_overwrite_moved_ref(tmp_path, make_local_git_repo):
    upstream = make_local_git_repo(tmp_path, "griffe", "1.5.1")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup")
    request = PublishGitSourceRequest(
        upstream_url=str(upstream), ref="1.5.1", package="griffe", version="1.5.1",
        workspace_dir=workspace, dry_run=True,
    )
    publisher.publish_git_source(request)

    # Simulate the upstream tag moving to a new commit
    import subprocess
    (upstream / "pyproject.toml").write_text("[project]\nname='griffe'\nversion='1.5.1'\n# moved\n")
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "commit", "-m", "moved", "--quiet"], cwd=upstream, check=True)
    subprocess.run(["git", "tag", "-f", "-a", "1.5.1", "-m", "moved"], cwd=upstream, check=True)

    try:
        publisher.publish_git_source(request)
        assert False, "expected ValueError"
    except ValueError:
        pass

def test_publish_git_source_canonicalizes_package_name_for_repo_and_tag(tmp_path, make_local_git_repo):
    """A package name with non-canonical casing/separators (e.g. Flask-WTF) must
    produce the same canonicalized repo name and tag that canonicalize_name()
    gives sdist packages, since that is what balor-fianna resolves against."""
    upstream = make_local_git_repo(tmp_path, "Flask_WTF", "1.3.0", tag="1.3.0")
    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(forge_url="https://forge.example.com", group="testgroup")

    result = publisher.publish_git_source(
        PublishGitSourceRequest(
            upstream_url=str(upstream), ref="1.3.0", package="Flask_WTF", version="1.3.0",
            workspace_dir=workspace, dry_run=True,
        )
    )

    from taisce_cuan.sdist import canonicalize_name
    canonical = canonicalize_name("Flask_WTF")
    assert result.tag_name == f"{canonical}/1.3.0"
    assert result.repo_name == f"pypi.org-{canonical}"
    assert (workspace / f"pypi.org-{canonical}").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_git_mirror.py -v`
Expected: FAIL (`AttributeError: 'GitMirrorPublisher' object has no attribute 'publish_git_source'`)

- [ ] **Step 3: Implement `publish_git_source` on `GitMirrorPublisher` in `src/taisce_cuan/source/mirror.py`**

Follow the same structure as `publish()` (git init, forge-credential config, `ensure_remote_project` + fetch, existing-tag idempotency check via `check_existing_tag_content` against `archive_git_source_to_dir`'s returned hash, `stream/{version}` orphan branch checkout, writing `.lightwell/metadata.json` from `build_git_ingestion_metadata`, commit, `<canonical>/<version>` + `baseline/<version>` tagging, atomic push) but source the tree from `clone_and_resolve_git_source` + `archive_git_source_to_dir` instead of `verify_normalized_source_artifact` + `extract_sdist_to_source`. Raise `ValueError` (not a generic exception) on the overwrite-refused case, matching `publish()`'s existing convention.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_git_mirror.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taisce_cuan/provenance/attest.py src/taisce_cuan/source/mirror.py tests/test_git_mirror.py
git commit -m "mirror: add publish_git_source for canonical_source_sha ingestion"
```

---

### Task 6: `push-git-source` CLI subcommand

**Files:**
- Modify: `src/taisce_cuan/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `GitMirrorPublisher.publish_git_source`, `PublishGitSourceRequest` (Task 5).
- Produces: `taisce-cuan push-git-source --upstream-url <url> --ref <ref> --package <name> --version <ver> [--workspace-dir <dir>] --forge-url <url> --group <group> [--remote-url] [--auth-token] --committer-name <name> --committer-email <email> [--dry-run]` — reuses the same forge/auth/committer flag names already defined on `push`.

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import MagicMock, patch
from taisce_cuan.cli import create_parser, handle_push_git_source

def test_push_git_source_parses_required_args():
    parser = create_parser()
    args = parser.parse_args([
        "push-git-source",
        "--upstream-url", "https://github.com/mkdocstrings/griffe.git",
        "--ref", "2.3.2",
        "--package", "griffe",
        "--version", "2.3.2",
        "--forge-url", "https://forge.example.com",
        "--group", "testgroup",
        "--committer-name", "bot",
        "--committer-email", "bot@example.com",
    ])
    assert args.command == "push-git-source"
    assert args.upstream_url == "https://github.com/mkdocstrings/griffe.git"
    assert args.ref == "2.3.2"

@patch("taisce_cuan.cli.GitMirrorPublisher")
def test_handle_push_git_source_calls_publisher(mock_publisher_cls):
    parser = create_parser()
    args = parser.parse_args([
        "push-git-source",
        "--upstream-url", "https://github.com/mkdocstrings/griffe.git",
        "--ref", "2.3.2",
        "--package", "griffe",
        "--version", "2.3.2",
        "--workspace-dir", "/tmp/work",
        "--forge-url", "https://forge.example.com",
        "--group", "testgroup",
        "--committer-name", "bot",
        "--committer-email", "bot@example.com",
    ])
    mock_publisher = mock_publisher_cls.return_value
    mock_publisher.publish_git_source.return_value = MagicMock(tag_name="griffe/2.3.2")

    exit_code = handle_push_git_source(args)

    assert exit_code == 0
    request = mock_publisher.publish_git_source.call_args[0][0]
    assert request.upstream_url == "https://github.com/mkdocstrings/griffe.git"
    assert request.ref == "2.3.2"
    assert request.package == "griffe"
    assert request.version == "2.3.2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL (`error: argument command: invalid choice: 'push-git-source'`)

- [ ] **Step 3: Add the `push-git-source` subparser and `handle_push_git_source` in `src/taisce_cuan/cli.py`**

Add a new subparser with `--upstream-url`, `--ref`, `--package`, `--version` (all required) plus the same `--workspace-dir`, `--forge-url`, `--group`, `--remote-url`, `--auth-token`, `--committer-name`, `--committer-email`, `--dry-run` flags `push` already defines (factor a shared `_add_forge_args(parser)` helper if that reads more cleanly — optional). `handle_push_git_source(args) -> int` builds a `PublishGitSourceRequest` from `args` and calls `publisher.publish_git_source(request)`, logging and returning 1 on exception like `handle_push` does. Wire `"push-git-source"` into `main()`'s dispatch.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taisce_cuan/cli.py tests/test_cli.py
git commit -m "cli: add push-git-source subcommand"
```

---

### Task 7: Document the pin contract

**Files:**
- Create: `docs/canonical-source-sha-pin-contract.md`

- [ ] **Step 1: Write the contract doc**

```markdown
# `canonical_source_sha` ingestion pin contract

For a Python package whose `canonical_source.type` is `"git"` ([LWLP-2111](https://redhat.atlassian.net/browse/LWLP-2111)),
`GitMirrorPublisher.publish_git_source` ([LWLP-3402](https://redhat.atlassian.net/browse/LWLP-3402))
resolves the configured `ref` to its exact upstream commit SHA at ingestion time
and writes it as a top-level `canonical_source_sha` string key in
`.lightwell/metadata.json`, committed at the same `<canonical_name>/<version>`
tag (and its `baseline/<version>` alias) that carries the mirrored source tree.
The pin is computed once, at first ingestion of a given package/version;
re-ingesting an already-tagged version never recomputes or overwrites it — a
moved upstream ref on a re-run fails closed instead. Balor-fianna's
`fetch_target_tree` stage ([balor-fianna!2098](https://gitlab.cee.redhat.com/lightwell/balor-fianna/-/merge_requests/2098))
reads this same key and compares it against the ref's live-resolved SHA at
build time, failing the build on a mismatch and passing permissively only when
no pin has been recorded yet (see [LWLP-2112](https://redhat.atlassian.net/browse/LWLP-2112)
for the sdist-only sync path this complements).
```

- [ ] **Step 2: Commit**

```bash
git add docs/canonical-source-sha-pin-contract.md
git commit -m "docs: document the canonical_source_sha ingestion pin contract"
```

---

### Task 8: Pilot end-to-end against griffe

This task is operational verification, not new code — there is no unit-test cycle; each step has a concrete, checkable outcome instead.

- [ ] **Step 1:** Confirm a scratch GitLab destination with the user for this pilot's real push (not `lightwell/lightwell-builds`, not `lightwell/lightwell-builds-midstream` — both reserved for other work). This is a real write to shared infrastructure; get explicit confirmation of the destination before Step 2.

- [ ] **Step 2:** Run, against the real forge, no `--dry-run`:
  ```bash
  taisce-cuan push-git-source \
    --upstream-url https://github.com/mkdocstrings/griffe.git \
    --ref 2.3.2 --package griffe --version 2.3.2 \
    --forge-url https://gitlab.cee.redhat.com --group <confirmed-scratch-group> \
    --committer-name "taisce-cuan pilot" --committer-email lightwell@redhat.com
  ```

- [ ] **Step 3:** Independently verify the pin: `git clone` the resulting `pypi.org-griffe` repo at tag `griffe/2.3.2`, read `.lightwell/metadata.json`'s `canonical_source_sha`, and confirm it equals `git ls-remote https://github.com/mkdocstrings/griffe.git 2.3.2` resolved to a commit (cross-checked independently of this tool's own output).

- [ ] **Step 4:** Point balor-fianna's `fetch_target_tree` stage at the real pushed mirror (set `LIGHTWELL_GITLAB_TOKEN`, configure the build-config's mirror project to the scratch repo, `canonical_source={"type": "git", "upstream_url": "https://github.com/mkdocstrings/griffe.git", "ref": "2.3.2"}`) and confirm the stage succeeds with `result.data["upstream_resolved_sha"]` equal to the pin from Step 3.

- [ ] **Step 5:** Induce a mismatch: push a new commit to the scratch mirror's `griffe/2.3.2` setup with `.lightwell/metadata.json`'s `canonical_source_sha` edited to `"0" * 40` (simulating drift, bypassing the tool). Re-run the same stage invocation from Step 4 and confirm it now fails closed, with the error naming both the real pin and the induced one.

- [ ] **Step 6:** Record the pilot transcript (commands + pass/fail for Steps 2-5) as a comment on [LWLP-3402](https://redhat.atlassian.net/browse/LWLP-3402), and link `docs/canonical-source-sha-pin-contract.md` from LWLP-2111, LWLP-2112, and LWLP-3402 per the ticket's acceptance criteria.
