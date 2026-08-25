import json
from pathlib import Path
import tarfile
from taisce_cuan.git_mirror import GitMirrorPublisher


def test_git_mirror_publisher_dry_run(tmp_path: Path):
    sdist_file = tmp_path / "sample-0.1.0.tar.gz"
    pkg_dir = tmp_path / "sample-0.1.0"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0.1.0'")
    with tarfile.open(sdist_file, "w:gz") as tar:
        tar.add(pkg_dir, arcname="sample-0.1.0")

    workspace = tmp_path / "workspace"
    publisher = GitMirrorPublisher(gitlab_url="https://gitlab.example.com", group="testgroup")

    tag = publisher.publish_sdist(
        sdist_path=sdist_file,
        package="sample",
        version="0.1.0",
        workspace_dir=workspace,
        dry_run=True,
    )

    assert tag == "sample/0.1.0"
    repo_dir = workspace / "pypi.org-sample"
    assert (repo_dir / "source" / "pyproject.toml").exists()
    assert (repo_dir / ".lightwell" / "metadata.json").exists()

    with open(repo_dir / ".lightwell" / "metadata.json") as f:
        data = json.load(f)
        assert data["predicate"]["buildDefinition"]["externalParameters"]["package"] == "sample"
        assert data["subject"][1]["name"] == "source/"
        assert data["subject"][1]["digest"]["gitTree"] != "pending"
