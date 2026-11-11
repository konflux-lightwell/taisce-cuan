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

import subprocess
from pathlib import Path

from taisce_cuan.tekton.package_git_source_image import main

TASK = (
    Path(__file__).parents[2]
    / "tekton/tasks/package-git-source-image/0.1/package-git-source-image.yaml"
)


def _fake_subprocess_run(auth_json="{}", digest="sha256:" + "a" * 64, push_exc=None):
    """Build a subprocess.run stand-in for select-oci-auth/oras push/oras resolve."""

    def _run(args, **kwargs):
        if args[0] == "select-oci-auth":
            return subprocess.CompletedProcess(args, 0, stdout=auth_json)
        if args[:2] == ["retry", "oras"] and args[2] == "push":
            if push_exc:
                raise push_exc
            return subprocess.CompletedProcess(args, 0)
        if args[:2] == ["retry", "oras"] and args[2] == "resolve":
            return subprocess.CompletedProcess(args, 0, stdout=f"{digest}\n")
        raise AssertionError(f"unexpected subprocess invocation: {args}")

    return _run


def _source_dir(tmp_path: Path) -> Path:
    d = tmp_path / "source-repo"
    d.mkdir()
    (d / "setup.py").write_text("# fake package source")
    return d


def test_pushes_and_writes_all_results(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run())
    source_dir = _source_dir(tmp_path)
    paths = {
        name: tmp_path / name
        for name in ("url", "digest", "ref", "chains_url", "chains_commit")
    }

    rc = main(
        [
            "--image",
            "quay.io/example/git-source:griffe-2.3.2",
            "--source-dir",
            str(source_dir),
            "--package",
            "griffe",
            "--version",
            "2.3.2",
            "--git-url",
            "https://gitlab.cee.redhat.com/lightwell/konflux-definitions/python-remediated-exploration.git",
            "--revision",
            "abc123",
            "--image-url-result-path",
            str(paths["url"]),
            "--image-digest-result-path",
            str(paths["digest"]),
            "--image-ref-result-path",
            str(paths["ref"]),
            "--chains-git-url-result-path",
            str(paths["chains_url"]),
            "--chains-git-commit-result-path",
            str(paths["chains_commit"]),
        ]
    )

    assert rc == 0
    assert paths["url"].read_text() == "quay.io/example/git-source:griffe-2.3.2"
    assert paths["digest"].read_text() == "sha256:" + "a" * 64
    assert (
        paths["ref"].read_text()
        == "quay.io/example/git-source:griffe-2.3.2@sha256:" + "a" * 64
    )
    assert (
        paths["chains_url"].read_text()
        == "https://gitlab.cee.redhat.com/lightwell/konflux-definitions/python-remediated-exploration.git"
    )
    assert paths["chains_commit"].read_text() == "abc123"


def test_fails_on_empty_source_dir(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: calls.append(a) or _fake_subprocess_run()(*a, **k),
    )
    empty_dir = tmp_path / "source-repo"
    empty_dir.mkdir()

    rc = main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(empty_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert rc == 1
    assert calls == []  # never attempted auth/push for an empty source dir


def test_fails_when_push_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_subprocess_run(
            push_exc=subprocess.CalledProcessError(1, ["oras", "push"])
        ),
    )
    source_dir = _source_dir(tmp_path)

    rc = main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert rc == 1


def test_includes_expires_after_annotation_only_when_set(tmp_path, monkeypatch):
    seen_push_args = []

    def _run(args, **kwargs):
        if args[:3] == ["retry", "oras", "push"]:
            seen_push_args.append(args)
        return _fake_subprocess_run()(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    source_dir = _source_dir(tmp_path)

    main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
            "--image-expires-after",
            "3d",
        ]
    )

    assert "--annotation" in seen_push_args[0]
    assert "quay.expires-after=3d" in seen_push_args[0]


def test_omits_expires_after_annotation_when_unset(tmp_path, monkeypatch):
    seen_push_args = []

    def _run(args, **kwargs):
        if args[:3] == ["retry", "oras", "push"]:
            seen_push_args.append(args)
        return _fake_subprocess_run()(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    source_dir = _source_dir(tmp_path)

    main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert not any("quay.expires-after" in a for a in seen_push_args[0])


def test_push_runs_with_source_dir_as_cwd_and_passes_top_level_entries(
    tmp_path, monkeypatch
):
    """Mirrors the original inline script's `cd source-repo && FILES=( * )` behavior,
    including picking up dotfiles -- an improvement over the original bash `*` glob,
    which silently excluded them (a known, documented gap in the inline version)."""
    seen = {}

    def _run(args, **kwargs):
        if args[:3] == ["retry", "oras", "push"]:
            seen["cwd"] = kwargs.get("cwd")
            seen["args"] = args
        return _fake_subprocess_run()(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    source_dir = _source_dir(tmp_path)
    (source_dir / ".github").mkdir()
    (source_dir / ".github" / "workflows.yml").write_text("x")

    main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert str(seen["cwd"]) == str(source_dir)
    assert "setup.py" in seen["args"]
    assert ".github" in seen["args"]


def test_select_oci_auth_stdout_is_captured_but_stderr_is_not(tmp_path, monkeypatch):
    """select-oci-auth's auth JSON goes to stdout (captured); its informational
    'Using token for ...' lines go to stderr and must stream through uncaptured,
    matching the original inline script's `select-oci-auth "$IMAGE" > auth.json`."""
    seen_kwargs = {}

    def _run(args, **kwargs):
        if args[0] == "select-oci-auth":
            seen_kwargs.update(kwargs)
        return _fake_subprocess_run()(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    source_dir = _source_dir(tmp_path)

    main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert seen_kwargs.get("stdout") is not None
    assert "stderr" not in seen_kwargs


def test_oras_push_output_is_not_captured(tmp_path, monkeypatch):
    """oras push's own progress/retry output must stream directly to the Tekton
    step log, not be swallowed -- matching the original script, which never
    redirected this command's stdout/stderr."""
    seen_kwargs = {}

    def _run(args, **kwargs):
        if args[:3] == ["retry", "oras", "push"]:
            seen_kwargs.update(kwargs)
        return _fake_subprocess_run()(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    source_dir = _source_dir(tmp_path)

    main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert "stdout" not in seen_kwargs
    assert "stderr" not in seen_kwargs


def test_optional_result_paths_may_be_omitted(tmp_path, monkeypatch):
    """A caller only interested in a subset of results (or none, in a unit test)
    shouldn't be forced to supply every --*-result-path flag."""
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run())
    source_dir = _source_dir(tmp_path)

    rc = main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert rc == 0


def test_auth_json_written_under_source_dir_not_system_tmp(tmp_path, monkeypatch):
    """auth.json is a transient credential file for a single invocation -- write it
    under the task's own workspace (source_dir's parent), not wherever the
    process's cwd happens to be when the step runs."""
    seen_registry_configs = []

    def _run(args, **kwargs):
        if "--registry-config" in args:
            seen_registry_configs.append(args[args.index("--registry-config") + 1])
        return _fake_subprocess_run()(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    source_dir = _source_dir(tmp_path)

    main(
        [
            "--image",
            "quay.io/example/git-source:x",
            "--source-dir",
            str(source_dir),
            "--package",
            "x",
            "--version",
            "1.0.0",
            "--git-url",
            "https://example.test/repo.git",
            "--revision",
            "main",
        ]
    )

    assert len(seen_registry_configs) == 2  # push and resolve both authenticate
    for registry_config in seen_registry_configs:
        assert Path(registry_config).parent == source_dir.parent


class TestPackageGitSourceImageTaskYaml:
    def test_task_yaml_declares_expected_params_and_results(self):
        task = TASK.read_text()

        for param in (
            "SOURCE_ARTIFACT",
            "PACKAGE",
            "VERSION",
            "IMAGE",
            "IMAGE_EXPIRES_AFTER",
            "caTrustConfigMapName",
            "caTrustConfigMapKey",
            "git-url",
            "revision",
        ):
            assert f"name: {param}" in task
        for result in (
            "IMAGE_URL",
            "IMAGE_DIGEST",
            "IMAGE_REF",
            "CHAINS-GIT_URL",
            "CHAINS-GIT_COMMIT",
        ):
            assert f"name: {result}" in task

    def test_create_oci_artifact_step_invokes_the_new_cli(self):
        task = TASK.read_text()

        assert "taisce-cuan-package-git-source-image" in task
        # Every result must be wired to a --*-result-path arg, not just declared.
        for flag in (
            "--image-url-result-path=$(results.IMAGE_URL.path)",
            "--image-digest-result-path=$(results.IMAGE_DIGEST.path)",
            "--image-ref-result-path=$(results.IMAGE_REF.path)",
            "--chains-git-url-result-path=$(results.CHAINS-GIT_URL.path)",
            "--chains-git-commit-result-path=$(results.CHAINS-GIT_COMMIT.path)",
        ):
            assert flag in task
