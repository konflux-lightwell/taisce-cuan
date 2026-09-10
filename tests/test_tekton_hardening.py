from pathlib import Path


TEKTON_ROOT = Path(__file__).parents[1] / "tekton" / "tasks"
FETCH_TASK = TEKTON_ROOT / "fetch-source/0.1/fetch-source.yaml"
PUSH_TASK = TEKTON_ROOT / "push-source-to-lightwell-builds/0.1/push-source-to-lightwell-builds.yaml"


def _task_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_fetch_source_runs_every_step_as_non_root_and_disables_git_prompts():
    text = _task_text(FETCH_TASK)

    assert "  stepTemplate:\n    securityContext:\n      runAsUser: 1001" in text
    assert '      - name: GIT_TERMINAL_PROMPT\n        value: "0"' in text


def test_push_source_runs_every_step_as_non_root_and_disables_git_prompts():
    text = _task_text(PUSH_TASK)

    assert "  stepTemplate:\n    securityContext:\n      runAsUser: 1001" in text
    assert '      - name: GIT_TERMINAL_PROMPT\n        value: "0"' in text


def test_git_credentials_are_read_only_with_restricted_default_mode():
    text = _task_text(PUSH_TASK)

    assert "        defaultMode: 0400" in text
    assert "          mountPath: /var/run/secrets/gitlab\n          readOnly: true" in text
