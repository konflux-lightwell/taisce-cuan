from pathlib import Path

TASK = (
    Path(__file__).parents[1] / "tekton/tasks/archive-sources/0.1/archive-sources.yaml"
)


def test_archive_sources_push_source_step_uses_entrypoint():
    task = TASK.read_text()

    # The push-source step calls the Python entrypoint directly, not a bash script.
    # Sign-key and env-var handling are tested in tests/tekton/test_push_source.py.
    assert "default: konflux-cosign-signing-production" in task
    assert "taisce-cuan-push-source" in task


def test_archive_sources_accepts_git_source_params():
    task = TASK.read_text()

    assert "name: sourceType" in task
    assert "name: upstreamUrl" in task
    assert "name: upstreamRef" in task
    assert "--source-type=$(params.sourceType)" in task
    assert "--upstream-url=$(params.upstreamUrl)" in task
    assert "--upstream-ref=$(params.upstreamRef)" in task


def test_archive_sources_mounts_signing_secret_read_only_and_optional():
    task = TASK.read_text()
    volume = task[
        task.index("    - name: signing-secret") : task.index("  stepTemplate:")
    ]
    mounts = task[
        task.index(
            "      - name: signing-secret", task.index("  stepTemplate:")
        ) : task.index("  steps:")
    ]

    assert "secretName: $(params.signingSecretName)" in volume
    assert "optional: true" in volume
    assert 'mountPath: "/etc/signing-secret"' in mounts
    assert "readOnly: true" in mounts
