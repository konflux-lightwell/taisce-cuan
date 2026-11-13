"""Exercise policy selection and reconciliation CLI argument handling."""

import json

import httpx
import pytest

from taisce_cuan import cli
from taisce_cuan.tekton import push_source

BOT_ID = 7001
RELEASE_ID = 7002
CREATOR_IDS = f"{BOT_ID},{RELEASE_ID}"
FORGE = "https://gitlab.example.com"
GROUP = "mirrors/python"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Make configuration defaults independent of the developer environment."""
    for name in (
        "GITLAB_TOKEN",
        "GIT_AUTH_TOKEN",
        "GIT_REMOTE_URL",
        "TAG_PROTECTION_USER_IDS",
        "TAG_PROTECTION_BOT_USER_ID",
        "SIGN_KEY",
        "PUBLIC_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def general_args(route, source, forge_url=FORGE, group=GROUP):
    """Build valid arguments for both general publisher routes."""
    args = [
        route,
        "--package",
        "sample",
        "--version",
        "1.0",
        "--forge-url",
        forge_url,
        "--group",
        group,
        "--committer-name",
        "Bot",
        "--committer-email",
        "bot@example.com",
    ]
    if route == "push":
        args += ["--source", str(source)]
    else:
        args += ["--upstream-url", "https://upstream.example/repo", "--ref", "1.0"]
    return args


@pytest.mark.parametrize("route", ["push", "push-git-source"])
def test_general_routes_select_policy_and_reject_invalid_target(
    tmp_path, caplog, route
):
    """Forward the profile to the real publisher before either publication path."""
    # Arrange
    source = tmp_path / "source.tar.gz"
    source.touch()
    args = general_args(route, source, forge_url="http://gitlab.example.com") + [
        "--mirror-policy",
        "python-mirror",
        "--tag-protection-user-ids",
        CREATOR_IDS,
        "--tag-protection-bot-user-id",
        str(BOT_ID),
    ]
    # Act
    result = cli.main(args)
    # Assert
    assert result == 1
    assert "Mirror policy requires a canonical forge and group" in caplog.text


@pytest.mark.parametrize("route", ["push", "push-git-source"])
@pytest.mark.parametrize("creator_source", ["flag", "environment"])
def test_valid_profile_reaches_publisher_on_both_routes(
    tmp_path, monkeypatch, route, creator_source
):
    """Pass the selected profile and creators to the publisher that publishes."""
    # Arrange
    built = []

    class RecordingPublisher:
        """Capture construction and report a successful publication."""

        def __init__(self, **kwargs):
            """Record the selected publisher configuration."""
            built.append(kwargs)

        def publish_source(self, **kwargs):
            """Return a successful archive publication without doing I/O."""
            return "tag"

        def publish_git_source(self, request):
            """Return a successful Git publication without doing I/O."""
            return type("Result", (), {"tag_name": "tag"})()

    monkeypatch.setattr(cli, "GitMirrorPublisher", RecordingPublisher)
    source = tmp_path / "source.tar.gz"
    source.touch()
    args = general_args(route, source) + [
        "--mirror-policy",
        "python-mirror",
        "--tag-protection-bot-user-id",
        str(BOT_ID),
    ]
    if creator_source == "flag":
        args += ["--tag-protection-user-ids", CREATOR_IDS]
    else:
        monkeypatch.setenv("TAG_PROTECTION_USER_IDS", CREATOR_IDS)
    # Act
    result = cli.main(args)
    # Assert
    assert result == 0
    assert built[0]["mirror_policy"] == "python-mirror"
    assert built[0]["tag_protection_user_ids"] == [BOT_ID, RELEASE_ID]
    assert built[0]["tag_protection_bot_user_id"] == BOT_ID


def policy_route_args(route, tmp_path):
    """Return the entry point and valid base arguments for one policy route.

    The base arguments carry no policy flags, so each test adds only what it
    checks. Non-reconcile routes get --dry-run so an accidental success cannot
    publish.
    """
    source = tmp_path / "missing-source.tar.gz"
    if route == "reconcile-mirrors":
        report = tmp_path / "report.json"
        return cli.main, [
            route,
            "--forge-url",
            FORGE,
            "--group",
            GROUP,
            "--report",
            str(report),
        ]
    if route.startswith("tekton-"):
        args = [
            "--committer-name",
            "Bot",
            "--committer-email",
            "bot@example.com",
            "--source-path",
            str(source),
        ]
        if route == "tekton-git":
            args += [
                "--source-type",
                "git",
                "--package",
                "sample",
                "--version",
                "1.0",
                "--upstream-url",
                "https://upstream.example/repo",
                "--upstream-ref",
                "1.0",
            ]
        return push_source.main, args + ["--dry-run"]
    return cli.main, general_args(route, source) + ["--dry-run"]


@pytest.fixture
def no_publisher_work(monkeypatch):
    """Fail the test if policy validation lets publisher or Tekton setup start."""

    def unexpected_work(*args, **kwargs):
        """Reject publisher construction or Tekton setup before validation."""
        pytest.fail("Policy configuration must be checked before any work")

    monkeypatch.setattr(cli, "GitMirrorPublisher", unexpected_work)
    monkeypatch.setattr(push_source, "GitMirrorPublisher", unexpected_work)
    monkeypatch.setattr(push_source, "apply_ca_bundle", unexpected_work)


@pytest.mark.parametrize(
    "route,creator_args",
    [
        ("push", []),
        ("push-git-source", []),
        ("tekton-sdist", []),
        ("tekton-git", []),
        *[
            (route, [f"--tag-protection-user-ids={value}"])
            for route in ("push", "tekton-sdist", "reconcile-mirrors")
            for value in ("", " ", " , ")
        ],
    ],
)
def test_policy_requires_nonempty_creator_configuration(
    tmp_path, monkeypatch, capsys, no_publisher_work, route, creator_args
):
    """Name the missing flag before source, secret, report, or publisher work."""
    # Arrange
    report = tmp_path / "report.json"
    main, args = policy_route_args(route, tmp_path)
    # Tekton deliberately accepts creator IDs from its flag, not this variable.
    if route.startswith("tekton-"):
        monkeypatch.setenv("TAG_PROTECTION_USER_IDS", CREATOR_IDS)
    args += [
        "--mirror-policy",
        "python-mirror",
        "--tag-protection-bot-user-id",
        str(BOT_ID),
        *creator_args,
    ]

    # Act
    with pytest.raises(SystemExit) as error:
        main(args)

    # Assert
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "--mirror-policy requires --tag-protection-user-ids" in stderr
    assert not report.exists()


@pytest.mark.parametrize(
    "route", ["push", "push-git-source", "tekton-sdist", "reconcile-mirrors"]
)
def test_policy_requires_bot_id_before_any_work(
    tmp_path, capsys, no_publisher_work, route
):
    """Name the missing bot flag before source, secret, report, or publisher work."""
    # Arrange
    report = tmp_path / "report.json"
    main, args = policy_route_args(route, tmp_path)
    args += [
        "--mirror-policy",
        "python-mirror",
        "--tag-protection-user-ids",
        CREATOR_IDS,
    ]

    # Act
    with pytest.raises(SystemExit) as error:
        main(args)

    # Assert
    assert error.value.code == 2
    assert "--tag-protection-bot-user-id" in capsys.readouterr().err
    assert not report.exists()


@pytest.mark.parametrize("route", ["sdist", "git"])
def test_tekton_routes_select_policy_and_reject_invalid_target(tmp_path, caplog, route):
    """Forward the Tekton flag for both routes before publisher execution."""
    # Arrange
    source = tmp_path / "source.tar.gz"
    source.touch()
    args = [
        "--source-path",
        str(source),
        "--package",
        "sample",
        "--version",
        "1.0",
        "--forge-url",
        "http://gitlab.example.com",
        "--group",
        GROUP,
        "--committer-name",
        "Bot",
        "--committer-email",
        "bot@example.com",
        "--git-secret-dir",
        str(tmp_path / "no-secret"),
        "--signing-secret-dir",
        str(tmp_path / "no-signing"),
        "--ca-bundle",
        str(tmp_path / "no-ca"),
        "--mirror-policy",
        "python-mirror",
        "--tag-protection-user-ids",
        CREATOR_IDS,
        "--tag-protection-bot-user-id",
        str(BOT_ID),
    ]
    if route == "git":
        args += [
            "--source-type",
            "git",
            "--upstream-url",
            "https://upstream.example/repo",
            "--upstream-ref",
            "1.0",
        ]
    # Act
    result = push_source.main(args)
    # Assert
    assert result == 1
    assert "Mirror policy requires a canonical forge and group" in caplog.text


@pytest.mark.parametrize("route", ["push", "push-git-source", "tekton"])
def test_invalid_user_id_is_handled_as_cli_failure(tmp_path, route, caplog):
    """Malformed creator configuration returns a failure rather than a traceback."""
    # Arrange
    source = tmp_path / "source.tar.gz"
    source.touch()
    if route == "tekton":
        args = [
            "--source-path",
            str(source),
            "--package",
            "sample",
            "--version",
            "1.0",
            "--committer-name",
            "Bot",
            "--committer-email",
            "bot@example.com",
            "--ca-bundle",
            str(tmp_path / "none"),
            "--git-secret-dir",
            str(tmp_path / "none"),
            "--signing-secret-dir",
            str(tmp_path / "none"),
        ]
        main = push_source.main
    else:
        args = general_args(route, source)
        main = cli.main
    # Act
    result = main(args + ["--tag-protection-user-ids", "not-a-number"])
    # Assert
    assert result == 1 and "Failed" in caplog.text


def test_defaults_and_token_fallback_are_unchanged(tmp_path, monkeypatch):
    """Generic routes do not implicitly enable a profile or creator IDs."""
    # Arrange
    monkeypatch.setenv("GIT_AUTH_TOKEN", "fallback")
    source = tmp_path / "source.tar.gz"
    # Act
    args = cli.create_parser().parse_args(general_args("push", source))
    tekton = push_source.create_parser().parse_args(
        ["--committer-name", "Bot", "--committer-email", "bot@example.com"]
    )
    # Assert
    assert args.mirror_policy is None and tekton.mirror_policy is None
    assert args.auth_token == "fallback" and args.tag_protection_user_ids == ""
    assert tekton.tag_protection_user_ids == ""


def test_reconcile_requires_explicit_forge_url(tmp_path, capsys):
    """Refuse to guess the forge: omitting --forge-url is a usage error."""
    # Arrange
    argv = [
        "reconcile-mirrors",
        "--group",
        GROUP,
        "--mirror-policy",
        "python-mirror",
        "--tag-protection-user-ids",
        CREATOR_IDS,
        "--tag-protection-bot-user-id",
        str(BOT_ID),
        "--report",
        str(tmp_path / "report"),
    ]
    # Act / Assert
    with pytest.raises(SystemExit) as error:
        cli.create_parser().parse_args(argv)
    assert error.value.code == 2
    assert "--forge-url" in capsys.readouterr().err


def test_reconcile_cli_reads_environment_token_and_writes_report(tmp_path, monkeypatch):
    """Connect the reconciliation command to an offline, empty GitLab inventory.

    An empty inventory proves nothing, so the audit exits as not ready.
    """
    # Arrange
    requests = []
    monkeypatch.setenv("GITLAB_TOKEN", "credential")
    report = tmp_path / "report.json"
    client = httpx.Client

    def respond(request):
        """Record the request and return an empty inventory."""
        requests.append(request)
        return httpx.Response(200, json=[], headers={"X-Next-Page": ""})

    def factory(*args, **kwargs):
        """Send real client requests to the offline fake."""
        kwargs["transport"] = httpx.MockTransport(respond)
        return client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    # Act
    result = cli.main(
        [
            "reconcile-mirrors",
            "--forge-url",
            FORGE,
            "--group",
            GROUP,
            "--mirror-policy",
            "python-mirror",
            "--tag-protection-user-ids",
            CREATOR_IDS,
            "--tag-protection-bot-user-id",
            str(BOT_ID),
            "--report",
            str(report),
        ]
    )
    # Assert
    assert result == 1 and not json.loads(report.read_text())["fleet_ready"]
    assert requests[0].headers["PRIVATE-TOKEN"] == "credential"
    assert "credential" not in report.read_text()
    assert requests[0].url.params["include_subgroups"] == "false"
