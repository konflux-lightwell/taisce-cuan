from pathlib import Path

from taisce_cuan.tekton.fetch_git_source import main


def test_fetch_git_source_writes_resolved_sha_result(tmp_path, monkeypatch):
    from taisce_cuan.source import git_source as git_source_mod

    def fake_clone(upstream_url, ref, workspace_dir, *, package, version):
        return git_source_mod.ClonedGitSource(
            package=package,
            version=version,
            upstream_url=upstream_url,
            ref=ref,
            resolved_sha="a" * 40,
            clone_dir=workspace_dir / "clone",
        )

    monkeypatch.setattr(git_source_mod, "clone_and_resolve_git_source", fake_clone)
    monkeypatch.setattr(
        git_source_mod, "archive_git_source_to_dir", lambda cloned, dest: "deadbeef"
    )

    result_path = tmp_path / "resolvedSha"
    output_dir = tmp_path / "source-repo"
    rc = main(
        [
            "griffe",
            "2.3.2",
            "--upstream-url=https://github.com/mkdocstrings/griffe",
            "--ref=2.3.2",
            f"--output-dir={output_dir}",
            f"--result-path={result_path}",
        ]
    )

    assert rc == 0
    assert result_path.read_text() == "a" * 40


def test_fetch_git_source_returns_1_on_clone_failure(tmp_path, monkeypatch):
    from taisce_cuan.source import git_source as git_source_mod

    def fake_clone(upstream_url, ref, workspace_dir, *, package, version):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(git_source_mod, "clone_and_resolve_git_source", fake_clone)

    rc = main(
        [
            "griffe",
            "2.3.2",
            "--upstream-url=https://github.com/mkdocstrings/griffe",
            "--ref=2.3.2",
            f"--output-dir={tmp_path / 'source-repo'}",
        ]
    )

    assert rc == 1


def _param_block(task: str, param_name: str) -> str:
    """Return the text of a single `- name: <param_name>` param entry, from
    just after its `- name:` line up to (but excluding) the next `- name:`
    param entry or the `results:` section -- whichever comes first. Scanning
    the whole span (not just the line immediately after `description:`)
    means this catches a `default:` line no matter where within the entry
    it's added."""
    marker = f"- name: {param_name}\n"
    start = task.index(marker)
    rest = task[start + len(marker) :]
    next_param_idx = rest.find("- name:")
    next_results_idx = rest.find("results:")
    boundaries = [i for i in (next_param_idx, next_results_idx) if i != -1]
    end = min(boundaries) if boundaries else len(rest)
    return rest[:end]


def test_fetch_git_source_task_yaml():
    task_path = (
        Path(__file__).parents[1]
        / "tekton/tasks/fetch-git-source/0.1/fetch-git-source.yaml"
    )
    task = task_path.read_text()

    # upstreamUrl and ref must be required params -- no `default:` line
    # anywhere within their param entry.
    for param_name in ("upstreamUrl", "ref"):
        block = _param_block(task, param_name)
        assert "default:" not in block, (
            f"{param_name} should not have a default anywhere in its param "
            f"entry (expected a required param)"
        )

    assert "taisce-cuan-fetch-git-source" in task

    source_yaml = (
        Path(__file__).parents[1] / "tekton/tasks/fetch-source/0.1/fetch-source.yaml"
    ).read_text()
    artifact_image_line = next(
        line
        for line in source_yaml.splitlines()
        if "build-trusted-artifacts" in line and "image:" in line
    )
    assert artifact_image_line.strip() in task
