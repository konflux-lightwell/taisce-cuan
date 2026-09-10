from pathlib import Path


TASK = Path(__file__).parents[1] / "tekton/tasks/archive-sources/0.1/archive-sources.yaml"


def test_archive_sources_uses_sign_key_and_preserves_legacy_key_files():
    task = TASK.read_text()

    # Secret keys are exported as environment variables, allowing the mounted
    # production secret's SIGN_KEY and AWS KMS credentials to reach cosign.
    assert 'default: konflux-cosign-signing-production' in task
    assert 'export "${secret_key}=$(cat "${secret_file}")"' in task
    assert 'ARGS+=("--sign-key=${SIGN_KEY}")' in task

    # Existing callers using file-backed cosign keys remain supported.
    assert 'ARGS+=("--sign-key=/etc/signing-secret/key.pem")' in task
    assert 'ARGS+=("--sign-key=/etc/signing-secret/cosign.key")' in task


def test_archive_sources_mounts_signing_secret_read_only_and_optional():
    task = TASK.read_text()
    volume = task[task.index("    - name: signing-secret"):task.index("  stepTemplate:")]
    mounts = task[task.index("      - name: signing-secret", task.index("  stepTemplate:")):task.index("  steps:")]

    assert "secretName: $(params.signingSecretName)" in volume
    assert "optional: true" in volume
    assert 'mountPath: "/etc/signing-secret"' in mounts
    assert "readOnly: true" in mounts
