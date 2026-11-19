"""Exercise Python mirror readiness through the external GitLab HTTP boundary."""

import json
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx
import pytest

from taisce_cuan.source import GitMirrorPublisher
from taisce_cuan.source.gitlab_api import MirrorPolicyError

FORGE = "https://gitlab.example.com"
GROUP = "mirrors/python"
BOT_ID = 7001
RELEASE_ID = 7002
RELEASE_CREATORS = [BOT_ID, RELEASE_ID]
BOT_PATTERNS = (
    "pnc/*",
    "alignment/*",
    "builddep/*",
    "reproducer-pin/*",
    "constituent-pin/*",
)


def user_grants(user_ids):
    """Build GitLab readback entries for user-specific creation permissions."""
    return [
        {"access_level": 40, "user_id": user_id, "group_id": None}
        for user_id in user_ids
    ]


BRANCH = "verification-metadata"
MAINTAINER_ACCESS = [
    {"access_level": 40, "user_id": None, "group_id": None, "deploy_key_id": None}
]
DEVELOPER_ACCESS = [
    {"access_level": 30, "user_id": None, "group_id": None, "deploy_key_id": None}
]
DEPLOY_KEY_ACCESS = [{"deploy_key_id": 9, "access_level": 40}]


def user_access(user_ids):
    """Build GitLab protected-branch entries for user-specific access."""
    return [
        {
            "access_level": 40,
            "user_id": user_id,
            "group_id": None,
            "deploy_key_id": None,
        }
        for user_id in user_ids
    ]


def branch_rule(name=BRANCH, *, push=None, merge=None, force=False, unprotect=None):
    """Build a GitLab protected-branch readback, defaulting to the required rule."""
    return {
        "name": name,
        "push_access_levels": user_access([BOT_ID]) if push is None else push,
        "merge_access_levels": user_access([BOT_ID]) if merge is None else merge,
        "allow_force_push": force,
        "unprotect_access_levels": MAINTAINER_ACCESS
        if unprotect is None
        else unprotect,
    }


def created_branch(payload):
    """Store a protected branch the way GitLab reads back a creation payload."""
    return branch_rule(
        payload["name"],
        push=user_access([entry["user_id"] for entry in payload["allowed_to_push"]]),
        merge=user_access([entry["user_id"] for entry in payload["allowed_to_merge"]]),
        force=payload["allow_force_push"],
    )


@dataclass
class MirrorServer:
    """Record the remote project and protection state of a small GitLab fake."""

    name: str = "pypi.org-joserfc"
    exists: bool = True
    rules: dict = field(
        default_factory=lambda: {
            "baseline/*": user_grants(RELEASE_CREATORS),
            "novel-head/*": user_grants(RELEASE_CREATORS),
        }
    )
    requests: list = field(default_factory=list)
    project_override: dict | None = None
    project_status: int | None = None
    group_status: int = 200
    create_status: int = 201
    create_override: dict | None = None
    post_fail_for: str | None = None
    post_timeout_for: str | None = None
    post_mismatch_for: str | None = None
    post_race_for: str | None = None
    post_also_creates: str | None = None
    branch_rules: dict = field(default_factory=lambda: {BRANCH: branch_rule()})
    branch_post_status: int | None = None
    branch_post_mismatch: bool = False
    branch_list_status: int | None = None
    branch_list_fails_after_post: bool = False

    @property
    def project(self):
        """Return the project identity and canonical push URL."""
        return {
            "id": 42,
            "path": self.name,
            "path_with_namespace": f"{GROUP}/{self.name}",
            "http_url_to_repo": f"{FORGE}/{GROUP}/{self.name}.git",
            "archived": False,
        }

    def respond(self, request):
        """Serve project provisioning and stateful protected-tag API requests."""
        self.requests.append(request)
        path = unquote(request.url.path)
        if path == f"/api/v4/projects/{GROUP}/{self.name}":
            return httpx.Response(
                self.project_status or (200 if self.exists else 404),
                json=(self.project_override or self.project)
                if self.exists
                else {"message": "not found"},
            )
        if path == f"/api/v4/groups/{GROUP}":
            return httpx.Response(self.group_status, json={"id": 7, "full_path": GROUP})
        if path == "/api/v4/projects" and request.method == "POST":
            self.exists = True
            return httpx.Response(
                self.create_status, json=self.create_override or self.project
            )
        if path == "/api/v4/projects/42/protected_branches":
            return self._branch_response(request)
        if path == "/api/v4/projects/42/protected_tags":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {"name": name, "create_access_levels": grants}
                        for name, grants in self.rules.items()
                    ],
                    headers={"X-Next-Page": ""},
                )
            payload = json.loads(request.content)
            name = payload["name"]
            if name == self.post_fail_for:
                return httpx.Response(503)
            if name == self.post_timeout_for:
                raise httpx.ReadTimeout("offline-test-token", request=request)
            if name in self.rules:
                return httpx.Response(
                    422, json={"message": ["Name has already been taken"]}
                )
            if "allowed_to_create" in payload:
                grants = user_grants(
                    [entry["user_id"] for entry in payload["allowed_to_create"]]
                )
            else:
                grants = [{"access_level": payload["create_access_level"]}]
            if name == self.post_mismatch_for:
                grants = user_grants(RELEASE_CREATORS)
            self.rules[name] = grants
            if name == "pnc/*" and self.post_also_creates:
                self.rules[self.post_also_creates] = user_grants([BOT_ID])
            if name == self.post_race_for:
                return httpx.Response(422)
            return httpx.Response(
                201, json={"name": name, "create_access_levels": grants}
            )
        return httpx.Response(404, json={"message": "unconfigured API request"})

    def _branch_response(self, request):
        """Serve protected-branch readback and one guarded creation per POST."""
        if request.method == "GET":
            posted = any(
                prior.method == "POST"
                and prior.url.path.endswith("/protected_branches")
                for prior in self.requests
            )
            if self.branch_list_status is not None:
                return httpx.Response(self.branch_list_status)
            if self.branch_list_fails_after_post and posted:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json=list(self.branch_rules.values()),
                headers={"X-Next-Page": ""},
            )
        payload = json.loads(request.content)
        if self.branch_post_status is not None:
            return httpx.Response(self.branch_post_status)
        stored = created_branch(payload)
        if self.branch_post_mismatch:
            stored["allow_force_push"] = True
        self.branch_rules[payload["name"]] = stored
        return httpx.Response(201, json=stored)


@pytest.fixture
def mirror_server(monkeypatch):
    """Connect the real publisher to a stateful fake at its HTTP boundary."""
    server = MirrorServer()
    client_class = httpx.Client

    def client_factory(*args, **kwargs):
        """Keep HTTP behavior real while routing requests to the fake server."""
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    return server


@pytest.mark.parametrize("exists", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize("name", ["pypi.org-joserfc", "pypi.org-mistune"])
def test_required_policy_prepares_python_mirrors(mirror_server, exists, name):
    """Prepare two real mirror layouts without replacing release-bot grants."""
    # Arrange
    mirror_server.exists = exists
    mirror_server.name = name
    if not exists:
        mirror_server.rules.clear()
        mirror_server.branch_rules.clear()
    release_before = {key: value[:] for key, value in mirror_server.rules.items()}
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act
    url = publisher.ensure_remote_project(name)

    # Assert
    assert url == f"{FORGE}/{GROUP}/{name}.git"
    assert set(mirror_server.rules) == {
        "baseline/*",
        "novel-head/*",
        "sme-verified/*",
        *BOT_PATTERNS,
    }
    for pattern in release_before:
        assert mirror_server.rules[pattern] == release_before[pattern]
    assert mirror_server.rules["baseline/*"] == user_grants(RELEASE_CREATORS)
    assert mirror_server.rules["novel-head/*"] == user_grants(RELEASE_CREATORS)
    for pattern in BOT_PATTERNS:
        assert mirror_server.rules[pattern] == user_grants([BOT_ID])
    assert mirror_server.rules["sme-verified/*"] == [{"access_level": 30}]


@pytest.mark.parametrize(
    "pattern",
    ["constituent-pin/*", "*", "constituent-pin/an-existing-commit"],
)
def test_required_policy_rejects_extra_creator_permissions(mirror_server, pattern):
    """Refuse uncertain protection without rewriting a divergent existing rule."""
    # Arrange
    original_grants = user_grants(RELEASE_CREATORS)
    mirror_server.rules[pattern] = original_grants[:]
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(
        RuntimeError, match="protection failed: .*(differs for|extra creators)"
    ):
        publisher.ensure_remote_project(mirror_server.name)
    assert mirror_server.rules[pattern] == original_grants
    assert not any(request.method == "DELETE" for request in mirror_server.requests)


@pytest.mark.parametrize(
    "overrides",
    [
        {"auth_token": None},
        {"remote_url": "https://gitlab.example.com/another/project.git"},
        {"tag_protection_user_ids": []},
        {"tag_protection_user_ids": None},
    ],
)
def test_required_policy_cannot_fall_back_to_an_unchecked_target(
    mirror_server, overrides
):
    """Reject an explicit remote, missing auth, or missing creators."""
    # Arrange
    kwargs = {
        "auth_token": "offline-test-token",
        "forge_url": FORGE,
        "group": GROUP,
        "tag_protection_user_ids": RELEASE_CREATORS,
        "mirror_policy": "python-mirror",
        "tag_protection_bot_user_id": BOT_ID,
        **overrides,
    }

    # Act / Assert
    with pytest.raises(RuntimeError):
        publisher = GitMirrorPublisher(**kwargs)
        publisher.ensure_remote_project(mirror_server.name)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_policy_project_api_errors_are_not_missing(mirror_server, status):
    """Reject API failures instead of creating or using an unchecked project."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    mirror_server.project_status = status
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        publisher.ensure_remote_project(mirror_server.name)
    assert not any(request.method == "POST" for request in mirror_server.requests)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 0),
        ("path_with_namespace", "elsewhere/repo"),
        ("http_url_to_repo", "https://other.example/repo.git"),
    ],
)
def test_policy_rejects_unexpected_project_identity(mirror_server, field, value):
    """Reject an API project that does not match the requested push target."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    mirror_server.project_override = {**mirror_server.project, field: value}
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        publisher.ensure_remote_project(mirror_server.name)
    assert not any(request.method == "POST" for request in mirror_server.requests)


@pytest.mark.parametrize("exists", [False, True], ids=["create", "lookup"])
@pytest.mark.parametrize("archived", [True, None, "", "missing"])
def test_publisher_rejects_archived_or_unknown_state(mirror_server, exists, archived):
    """Reject archived or malformed project responses before policy writes."""
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    # Arrange
    mirror_server.exists = exists
    identity = {**mirror_server.project, "archived": archived}
    if archived == "missing":
        del identity["archived"]
    if exists:
        mirror_server.project_override = identity
    else:
        mirror_server.create_override = identity
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        publisher.ensure_remote_project(mirror_server.name)
    assert not any("protected_tags" in r.url.path for r in mirror_server.requests)


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_project_archive_exception_requires_bool(value):
    """Reject non-boolean archive exceptions, including truthy values."""
    from taisce_cuan.source.mirror_policy import MirrorPolicyError, _validated_project

    # Arrange
    row = MirrorServer().project

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        _validated_project(
            row, FORGE, f"{GROUP}/pypi.org-joserfc", allow_archived=value
        )


@pytest.mark.parametrize("delay,expected", [(None, 1), ("0", 1), ("2", 2), ("60", 60)])
def test_policy_list_retries_rate_limited_get(monkeypatch, delay, expected):
    """Retry a 429 using a bounded delay while preserving the complete page."""
    from taisce_cuan.source.gitlab_api import gitlab_list

    # Arrange
    requests, sleeps = [], []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def respond(request):
        """Rate limit once, then return a complete page."""
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429, headers={} if delay is None else {"Retry-After": delay}
            )
        return httpx.Response(200, json=[{"id": 1}], headers={"X-Next-Page": ""})

    # Act
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = gitlab_list(client, f"{FORGE}/api/v4/projects", {})

    # Assert
    assert result == [{"id": 1}] and len(requests) == 2 and sleeps == [expected]


@pytest.mark.parametrize("delay", ["-1", "61", "bad", "1.5", ""])
def test_policy_list_rejects_invalid_retry_after(monkeypatch, delay):
    """Stop immediately on invalid or unbounded rate-limit delays."""
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    # Arrange
    requests, sleeps = [], []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def respond(request):
        """Return a malformed or excessive retry delay."""
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": delay})

    # Act / Assert
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MirrorPolicyError):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})
    assert len(requests) == 1 and not sleeps


def test_policy_list_rate_limit_exhaustion_is_not_empty(monkeypatch):
    """Stop after three GET attempts without treating 429 as an empty page."""
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    # Arrange
    requests, sleeps = [], []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def respond(request):
        """Keep the request rate limited."""
        requests.append(request)
        return httpx.Response(429)

    # Act / Assert
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MirrorPolicyError, match="429"):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})
    assert len(requests) == 3 and sleeps == [1, 1]


@pytest.mark.parametrize("header", ["1", "3", "https://other.example/?page=2", "-1"])
def test_policy_list_rejects_invalid_pagination(header):
    """Reject loops, skipped pages, URLs, and invalid next-page numbers."""
    # Arrange
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=[], headers={"X-Next-Page": header})
    )
    with httpx.Client(transport=transport) as client:
        # Act / Assert
        with pytest.raises(MirrorPolicyError):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})


@pytest.mark.parametrize("payload", [None, {}, [None], [{"id": 1}, "bad"]])
def test_policy_list_rejects_malformed_payloads(payload):
    """Reject malformed pages instead of treating them as empty inventory."""
    # Arrange
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        # Act / Assert
        with pytest.raises(MirrorPolicyError):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})


def test_policy_list_reads_full_page_without_pagination_header():
    """Continue after a full page when the server omits pagination headers."""
    # Arrange
    from taisce_cuan.source.gitlab_api import gitlab_list

    def respond(request):
        """Return a full first page and a final short page."""
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"page": page}] * (100 if page == 1 else 1))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        # Act
        result = gitlab_list(client, f"{FORGE}/api/v4/projects", {})

    # Assert
    assert len(result) == 101
    assert result[-1] == {"page": 2}


def test_policy_list_transport_error_does_not_expose_credentials():
    """Sanitize transport errors rather than exposing their credential text."""
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    # Arrange
    def respond(request):
        """Raise a transport error containing a fake credential."""
        raise httpx.ReadTimeout("secret-token", request=request)

    # Act / Assert
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MirrorPolicyError) as error:
            gitlab_list(
                client, f"{FORGE}/api/v4/projects", {"PRIVATE-TOKEN": "secret-token"}
            )
    assert "secret-token" not in str(error.value)


def test_policy_partial_creation_reports_progress_and_retries(mirror_server):
    """Keep partial progress visible and create only missing rules on retry."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    mirror_server.post_fail_for = "alignment/*"
    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        first = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)
        mirror_server.post_fail_for = None
        second = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)

    # Assert
    assert not first["ready"] and first["errors"]
    assert "pnc/*" in first["created"]
    assert second["ready"] and not second["errors"]
    assert "pnc/*" not in second["created"]


@pytest.mark.parametrize("race", [False, True])
def test_policy_readback_mismatch_is_not_ready(mirror_server, race):
    """Reject a POST or creation race when persisted grants differ from the rule."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    mirror_server.post_mismatch_for = "pnc/*"
    if race:
        mirror_server.post_race_for = "pnc/*"
    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)

    # Assert
    assert not report["ready"] and report["errors"]
    assert ("pnc/*" in report["created"]) is not race
    assert sum(request.method == "POST" for request in mirror_server.requests) == 1


@pytest.mark.parametrize("release_ids", [[], None, [True], [0], [-1], ["7001"]])
def test_policy_rejects_invalid_release_ids(release_ids):
    """Require configured positive integers, not missing values or booleans."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError, required_tag_rules

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        required_tag_rules("python-mirror", FORGE, GROUP, release_ids, BOT_ID)


@pytest.mark.parametrize("bot_id", [None, 0, -1, True, "7001"])
def test_policy_rejects_invalid_bot_id(bot_id):
    """Require a configured positive integer bot ID, not a missing or boolean value."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError, required_tag_rules

    # Act / Assert
    with pytest.raises(MirrorPolicyError, match="positive bot user ID"):
        required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, bot_id)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"mirror_policy": ""}, "Unsupported mirror policy profile"),
        ({"mirror_policy": "unknown"}, "Unsupported mirror policy profile"),
        ({"forge_url": FORGE + "/"}, "canonical forge and group"),
        ({"forge_url": "http://gitlab.example.com"}, "canonical forge and group"),
        ({"forge_url": FORGE + "/api"}, "canonical forge and group"),
        (
            {"forge_url": "https://token@gitlab.example.com"},
            "canonical forge and group",
        ),
        ({"group": GROUP + "/"}, "canonical forge and group"),
        ({"group": "/" + GROUP}, "canonical forge and group"),
        ({"group": ""}, "canonical forge and group"),
    ],
)
def test_policy_rejects_noncanonical_configuration(options, message):
    """Require the named profile, a bare HTTPS origin, and a bare namespace."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    kwargs = {
        "mirror_policy": "python-mirror",
        "forge_url": FORGE,
        "group": GROUP,
        "tag_protection_bot_user_id": BOT_ID,
        **options,
    }

    # Act / Assert
    with pytest.raises(MirrorPolicyError, match=message):
        GitMirrorPublisher(tag_protection_user_ids=RELEASE_CREATORS, **kwargs)


@pytest.mark.parametrize("name", ["other-repo", "pypi.org-../bad", "pypi.org-"])
def test_policy_rejects_non_python_project_names(mirror_server, name):
    """Reject non-Python and path-like repository names before any request."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        publisher.ensure_remote_project(name)
    assert not mirror_server.requests


@pytest.mark.parametrize("failure", ["group", "create", "identity"])
def test_policy_rejects_new_project_provisioning_errors(mirror_server, failure):
    """Do not return a constructed URL when project provisioning fails."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    mirror_server.exists = False
    if failure == "group":
        mirror_server.group_status = 403
    elif failure == "create":
        mirror_server.create_status = 500
    else:
        mirror_server.create_override = {**mirror_server.project, "id": True}
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError):
        publisher.ensure_remote_project(mirror_server.name)
    assert not any(
        "protected_tags" in request.url.path for request in mirror_server.requests
    )


@pytest.mark.parametrize(
    "pattern,grants",
    [
        ("pnc/*", [{"access_level": 40}]),
        ("pnc/*", [{"group_id": 77, "access_level": 40}]),
        ("sme-verified/*", [{"access_level": 20}]),
        ("pnc/*", [{"access_level": 40, "user_id": BOT_ID, "group_id": 77}]),
        ("pnc/*", [{"access_level": "unknown", "user_id": BOT_ID}]),
    ],
)
def test_policy_rejects_divergent_or_malformed_grants(mirror_server, pattern, grants):
    """Reject role, group, or malformed grants when exact creators are required."""
    # Arrange
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    mirror_server.rules[pattern] = grants
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError, match="differs for|grant is unrecognized"):
        publisher.ensure_remote_project(mirror_server.name)
    assert not any(request.method == "POST" for request in mirror_server.requests)


def test_policy_inventory_is_read_only_by_default(mirror_server):
    """Report missing rules without creating them during a read-only check."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(client, FORGE, 42, {}, rules)

    # Assert
    assert not report["ready"] and report["errors"] and not report["created"]
    assert not any(request.method == "POST" for request in mirror_server.requests)


def test_policy_allows_narrower_overlap_with_same_creator(mirror_server):
    """Accept a narrower rule when it grants no additional creator access."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    mirror_server.rules["pnc/existing-tag"] = user_grants([BOT_ID])
    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)

    # Assert
    assert report["ready"] and not report["errors"]
    assert mirror_server.rules["pnc/existing-tag"] == user_grants([BOT_ID])


def test_policy_concurrent_creation_requires_readback(mirror_server):
    """Accept a concurrent creator only when the complete readback matches."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    mirror_server.post_race_for = "pnc/*"
    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)

    # Assert
    assert report["ready"] and not report["errors"]
    assert "pnc/*" not in report["created"]


def test_policy_rechecks_missing_rules_after_concurrent_creation(mirror_server):
    """Skip a later rule that another process created during reconciliation."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    mirror_server.post_also_creates = "alignment/*"
    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)

    # Assert
    assert report["ready"] and not report["errors"]
    assert "alignment/*" not in report["created"]
    assert not any(
        request.method == "POST"
        and json.loads(request.content)["name"] == "alignment/*"
        for request in mirror_server.requests
    )


def test_policy_timeout_preserves_progress_without_exposing_credentials(mirror_server):
    """Report partial creation and read back after an uncertain POST timeout."""
    # Arrange
    from taisce_cuan.source.mirror_policy import (
        reconcile_tag_policy,
        required_tag_rules,
    )

    mirror_server.post_timeout_for = "alignment/*"
    rules = required_tag_rules("python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID)
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(client, FORGE, 42, {}, rules, apply=True)

    # Assert
    assert not report["ready"] and report["errors"]
    assert report["created"] == ["pnc/*"]
    assert "offline-test-token" not in str(report)
    assert mirror_server.requests[-1].method == "GET"


@pytest.mark.parametrize(
    "response", [httpx.Response(503), httpx.Response(200, text="{")]
)
def test_policy_list_rejects_http_errors_and_invalid_json(response):
    """Do not confuse an API error or invalid JSON with an empty inventory."""
    # Arrange
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        # Act / Assert
        with pytest.raises(MirrorPolicyError):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})


def test_policy_list_rejects_redirect_without_forwarding_token():
    """Keep private-token headers off cross-host pagination or redirects."""
    # Arrange
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    requests = []

    def respond(request):
        """Record requests and offer a cross-host redirect."""
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/page"})

    with httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        # Act / Assert
        with pytest.raises(MirrorPolicyError):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {"PRIVATE-TOKEN": "secret"})
    assert len(requests) == 1


def test_policy_list_rejects_page_limit():
    """Report an incomplete listing instead of stopping after the page bound."""
    # Arrange
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    def respond(request):
        """Supply a next-page header on every page."""
        next_page = int(request.url.params["page"]) + 1
        return httpx.Response(200, json=[], headers={"X-Next-Page": str(next_page)})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        # Act / Assert
        with pytest.raises(MirrorPolicyError, match="page limit"):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})


@pytest.mark.parametrize("route", ["source", "git"])
def test_policy_publish_routes_refuse_unverified_remote(
    mirror_server, tmp_path, make_local_git_repo, route
):
    """Reject publication on both routes before using an unverified remote."""
    # Arrange
    from test_git_mirror import create_sample_source

    from taisce_cuan.provenance.attest import PublishGitSourceRequest
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    mirror_server.name = "pypi.org-sample"
    mirror_server.project_status = 403
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )
    if route == "source":
        source = create_sample_source(tmp_path, "sample", "1.0.0")
    else:
        source = make_local_git_repo(tmp_path, "sample", "1.0.0")

    # Act / Assert
    with pytest.raises(MirrorPolicyError, match="HTTP 403"):
        if route == "source":
            publisher.publish_source(source, "sample", "1.0.0", tmp_path / "workspace")
        else:
            publisher.publish_git_source(
                PublishGitSourceRequest(
                    upstream_url=str(source),
                    ref="1.0.0",
                    package="sample",
                    version="1.0.0",
                    workspace_dir=tmp_path / "workspace",
                )
            )
    assert len(mirror_server.requests) == 1
    assert mirror_server.requests[0].method == "GET"


@pytest.mark.parametrize("route", ["source", "git"])
def test_policy_publish_dry_run_stays_offline(
    mirror_server, tmp_path, make_local_git_repo, monkeypatch, route
):
    """Keep policy-enabled dry runs offline on both publishing routes."""
    # Arrange
    from test_git_mirror import create_sample_source

    from taisce_cuan.provenance.attest import PublishGitSourceRequest

    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "tag.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )
    if route == "source":
        source = create_sample_source(tmp_path, "sample", "1.0.0")
    else:
        source = make_local_git_repo(tmp_path, "sample", "1.0.0")

    # Act
    if route == "source":
        publisher.publish_source(
            source, "sample", "1.0.0", tmp_path / "workspace", dry_run=True
        )
    else:
        publisher.publish_git_source(
            PublishGitSourceRequest(
                upstream_url=str(source),
                ref="1.0.0",
                package="sample",
                version="1.0.0",
                workspace_dir=tmp_path / "workspace",
                dry_run=True,
            )
        )

    # Assert
    assert not mirror_server.requests


def test_policy_rejects_partial_inventory():
    """Reject a partial HTTP response rather than treating it as full coverage."""
    from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_list

    # Arrange
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(206, json=[]))
    ) as client:
        # Act / Assert
        with pytest.raises(MirrorPolicyError, match="HTTP 206"):
            gitlab_list(client, f"{FORGE}/api/v4/projects", {})


@pytest.mark.parametrize(
    ("pattern", "grant"),
    [
        ("sme-verified/example", {"deploy_key_id": 9, "access_level": 40}),
        (
            "constituent-pin/example",
            {"user_id": BOT_ID, "deploy_key_id": 9, "access_level": 40},
        ),
    ],
)
def test_policy_rejects_unrecognized_deploy_key_grants(mirror_server, pattern, grant):
    """Do not mistake an unknown deploy-key principal for a user or role grant."""
    from taisce_cuan.source.mirror_policy import MirrorPolicyError

    # Arrange
    mirror_server.rules[pattern] = [grant]
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act / Assert
    with pytest.raises(MirrorPolicyError, match="unrecognized"):
        publisher.ensure_remote_project(mirror_server.name)


@pytest.mark.parametrize(
    ("pattern", "grant"),
    [
        (
            "constituent-pin/*",
            {
                "user_id": BOT_ID,
                "group_id": None,
                "deploy_key_id": None,
                "access_level": 40,
            },
        ),
        ("sme-verified/*", {"deploy_key_id": None, "access_level": 30}),
    ],
)
def test_policy_accepts_null_deploy_key_metadata(mirror_server, pattern, grant):
    """Accept normal API grants whose unused deploy-key field is explicitly null."""
    # Arrange
    mirror_server.rules[pattern] = [grant]
    publisher = GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )

    # Act
    url = publisher.ensure_remote_project(mirror_server.name)

    # Assert
    assert url == mirror_server.project["http_url_to_repo"]


def test_policy_audit_identifies_expected_and_overlapping_grants(mirror_server):
    """Expose the exact conflicting grants in the credential-free policy report."""
    from taisce_cuan.source.mirror_policy import (
        TagProtectionRule,
        reconcile_tag_policy,
    )

    # Arrange
    mirror_server.rules["constituent-pin/*"] = user_grants([BOT_ID])
    mirror_server.rules["constituent-pin/example"] = user_grants([RELEASE_ID])
    with httpx.Client() as client:
        # Act
        report = reconcile_tag_policy(
            client,
            FORGE,
            42,
            {},
            (TagProtectionRule("constituent-pin/*", (BOT_ID,)),),
        )

    # Assert
    rule = report["rules"][0]
    assert not report["ready"]
    assert rule["expected"] == [{"kind": "user", "value": BOT_ID}]
    assert rule["observed"] == [[{"kind": "user", "value": BOT_ID}]]
    assert rule["overlaps"] == [
        {
            "name": "constituent-pin/example",
            "grants": [{"kind": "user", "value": RELEASE_ID}],
            "compatible": False,
        }
    ]
    assert any("constituent-pin/example" in error for error in report["errors"])


CONFLICTING_BRANCH_RULES = [
    pytest.param(
        branch_rule(push=user_access([BOT_ID, RELEASE_ID])), id="extra-push-user"
    ),
    pytest.param(branch_rule(push=MAINTAINER_ACCESS), id="maintainer-push"),
    pytest.param(branch_rule(merge=user_access([RELEASE_ID])), id="merge-differs"),
    pytest.param(branch_rule(force=True), id="force-push-allowed"),
    pytest.param(branch_rule(unprotect=DEVELOPER_ACCESS), id="developer-unprotect"),
    pytest.param(
        {
            key: value
            for key, value in branch_rule().items()
            if key != "unprotect_access_levels"
        },
        id="unprotect-missing",
    ),
    pytest.param(branch_rule(push=DEPLOY_KEY_ACCESS), id="deploy-key-grant"),
]


def branch_requests(server, method):
    """Return the protected-branch requests of one HTTP method seen by the fake."""
    return [
        request
        for request in server.requests
        if request.method == method and request.url.path.endswith("/protected_branches")
    ]


def policy_publisher():
    """Build a publisher for the python-mirror policy with the fake's identities."""
    return GitMirrorPublisher(
        auth_token="offline-test-token",
        forge_url=FORGE,
        group=GROUP,
        tag_protection_user_ids=RELEASE_CREATORS,
        mirror_policy="python-mirror",
        tag_protection_bot_user_id=BOT_ID,
    )


def expect_error(action, error_type):
    """Return the expected error, or fail with AssertionError for a precise xfail."""
    try:
        action()
    except error_type as error:
        return error
    raise AssertionError(f"Expected {error_type.__name__}")


def publish_outcome(publisher, name):
    """Return the policy error text, or None when publication is allowed."""
    try:
        publisher.ensure_remote_project(name)
    except MirrorPolicyError as error:
        return str(error)
    return None


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="branch rule is not provisioned yet"
)
@pytest.mark.parametrize("exists", [False, True], ids=["new", "existing"])
def test_branch_rule_is_created_with_readback(mirror_server, exists):
    """Create a missing verification-metadata rule once and accept its readback."""
    # Arrange
    mirror_server.exists = exists
    mirror_server.branch_rules.clear()
    publisher = policy_publisher()

    # Act
    url = publisher.ensure_remote_project(mirror_server.name)

    # Assert
    assert url == mirror_server.project["http_url_to_repo"]
    posts = branch_requests(mirror_server, "POST")
    assert [json.loads(post.content) for post in posts] == [
        {
            "name": BRANCH,
            "allow_force_push": False,
            "allowed_to_push": [{"user_id": BOT_ID}],
            "allowed_to_merge": [{"user_id": BOT_ID}],
        }
    ]
    assert mirror_server.branch_rules[BRANCH] == branch_rule()


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="branch rule is not checked yet"
)
def test_correct_branch_rule_causes_no_branch_write(mirror_server):
    """Read the correct branch rule and leave it untouched."""
    # Arrange
    publisher = policy_publisher()

    # Act
    publisher.ensure_remote_project(mirror_server.name)

    # Assert
    assert branch_requests(mirror_server, "GET")
    assert not branch_requests(mirror_server, "POST")
    assert mirror_server.branch_rules == {BRANCH: branch_rule()}


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="branch rule is not checked yet"
)
@pytest.mark.parametrize("rule", CONFLICTING_BRANCH_RULES)
def test_branch_rule_conflicts_are_reported_not_rewritten(mirror_server, rule):
    """Refuse divergent branch protection and leave the stored rule unchanged."""
    # Arrange
    mirror_server.branch_rules[BRANCH] = rule
    publisher = policy_publisher()

    # Act
    error = expect_error(
        lambda: publisher.ensure_remote_project(mirror_server.name), MirrorPolicyError
    )

    # Assert
    assert "Protected branch policy differs" in str(error)
    assert not branch_requests(mirror_server, "POST")
    assert mirror_server.branch_rules[BRANCH] == rule


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="overlapping branch rules are not checked",
)
@pytest.mark.parametrize(
    ("rule", "blocked"),
    [
        pytest.param(branch_rule("*", force=True), True, id="wildcard-force-push"),
        pytest.param(
            branch_rule("verification-*", push=MAINTAINER_ACCESS),
            True,
            id="prefix-maintainer-push",
        ),
        pytest.param(
            branch_rule("main", push=DEPLOY_KEY_ACCESS),
            False,
            id="unrelated-deploy-key",
        ),
        pytest.param(branch_rule("*"), False, id="wildcard-bot-only"),
    ],
)
def test_overlapping_branch_rules(mirror_server, rule, blocked):
    """Block overlaps that grant extra access, and ignore or accept the rest."""
    # Arrange
    mirror_server.branch_rules[rule["name"]] = rule
    publisher = policy_publisher()

    # Act
    outcome = publish_outcome(publisher, mirror_server.name)

    # Assert
    assert branch_requests(mirror_server, "GET")
    assert not branch_requests(mirror_server, "POST")
    if blocked:
        assert outcome is not None and "Overlapping protected branch policy" in outcome
    else:
        assert outcome is None


@pytest.mark.xfail(
    strict=True, raises=ImportError, reason="branch policy module is missing"
)
@pytest.mark.parametrize(
    ("setting", "error", "created"),
    [
        pytest.param(
            ("branch_post_status", 500),
            "Protected branch policy creation failed for verification-metadata: "
            "HTTP 500",
            [],
            id="post-500",
        ),
        pytest.param(
            ("branch_post_mismatch", True),
            "Protected branch policy differs for verification-metadata",
            [BRANCH],
            id="readback-differs",
        ),
        pytest.param(
            ("branch_list_fails_after_post", True),
            "Protected branch policy readback failed",
            [BRANCH],
            id="readback-list-fails",
        ),
    ],
)
def test_branch_apply_failures_are_not_ready(mirror_server, setting, error, created):
    """Report a failed branch write or readback as not ready, keeping the audit."""
    from taisce_cuan.source.branch_policy import (
        reconcile_branch_policy,
        required_branch_rule,
    )

    # Arrange
    knob, value = setting
    setattr(mirror_server, knob, value)
    mirror_server.branch_rules.clear()
    rule = required_branch_rule(BOT_ID)

    # Act
    with httpx.Client() as client:
        report = reconcile_branch_policy(client, FORGE, 42, {}, rule, apply=True)

    # Assert
    assert not report["ready"]
    assert error in report["errors"]
    assert report["created"] == created
    assert len(branch_requests(mirror_server, "POST")) == 1


@pytest.mark.xfail(
    strict=True, raises=ImportError, reason="project policy module is missing"
)
def test_tag_conflict_does_not_block_branch_rule(mirror_server):
    """Create the branch rule while a stale tag conflict blocks tag writes."""
    from taisce_cuan.source.project_policy import (
        reconcile_project_policy,
        required_project_policy,
    )

    # Arrange
    mirror_server.rules["pnc/*"] = user_grants([RELEASE_ID])
    mirror_server.branch_rules.clear()
    tags_before = dict(mirror_server.rules)
    policy = required_project_policy(
        "python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID
    )

    # Act
    with httpx.Client() as client:
        report = reconcile_project_policy(client, FORGE, 42, {}, policy, apply=True)

    # Assert
    assert report["branch_created"] == [BRANCH]
    assert mirror_server.branch_rules[BRANCH] == branch_rule()
    assert mirror_server.rules == tags_before
    assert not report["ready"] and not report["apply_incomplete"]
    assert any("pnc/*" in error for error in report["errors"])


@pytest.mark.xfail(
    strict=True, raises=ImportError, reason="project policy module is missing"
)
def test_branch_conflict_does_not_block_tag_rules(mirror_server):
    """Create missing tag rules while a conflicting branch rule stays untouched."""
    from taisce_cuan.source.project_policy import (
        reconcile_project_policy,
        required_project_policy,
    )

    # Arrange
    mirror_server.rules.clear()
    conflict = branch_rule(force=True)
    mirror_server.branch_rules[BRANCH] = conflict
    policy = required_project_policy(
        "python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID
    )
    tag_names = {rule.name for rule in policy.tag_rules}

    # Act
    with httpx.Client() as client:
        report = reconcile_project_policy(client, FORGE, 42, {}, policy, apply=True)

    # Assert
    assert set(report["created"]) == tag_names
    assert set(mirror_server.rules) == tag_names
    assert mirror_server.branch_rules[BRANCH] == conflict
    assert (
        "Protected branch policy differs for verification-metadata" in report["errors"]
    )
    assert not report["branch_created"] and not report["ready"]
    assert not report["apply_incomplete"]


@pytest.mark.xfail(
    strict=True, raises=ImportError, reason="project policy module is missing"
)
def test_branch_list_failure_after_tag_writes_keeps_created_audit(mirror_server):
    """Keep the audit of tag rules created before a branch inventory failure."""
    from taisce_cuan.source.project_policy import (
        reconcile_project_policy,
        required_project_policy,
    )

    # Arrange
    mirror_server.rules.clear()
    mirror_server.branch_list_status = 500
    policy = required_project_policy(
        "python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID
    )

    # Act
    with httpx.Client() as client:
        report = reconcile_project_policy(client, FORGE, 42, {}, policy, apply=True)

    # Assert
    assert set(report["created"]) == {rule.name for rule in policy.tag_rules}
    assert "GitLab GET request failed: HTTP 500" in report["errors"]
    assert report["branch_rules"] == [] and not report["branch_created"]
    assert not report["ready"] and report["apply_incomplete"]


@pytest.mark.xfail(
    strict=True, raises=ImportError, reason="project policy module is missing"
)
def test_branch_list_failure_before_any_write_raises(mirror_server):
    """Raise without writing when the branch inventory fails before any tag write."""
    from taisce_cuan.source.project_policy import (
        reconcile_project_policy,
        required_project_policy,
    )

    # Arrange
    policy = required_project_policy(
        "python-mirror", FORGE, GROUP, RELEASE_CREATORS, BOT_ID
    )
    with httpx.Client() as client:
        reconcile_project_policy(client, FORGE, 42, {}, policy, apply=True)
    mirror_server.branch_list_status = 500
    mirror_server.requests.clear()

    # Act
    with httpx.Client() as client:
        error = expect_error(
            lambda: reconcile_project_policy(client, FORGE, 42, {}, policy, apply=True),
            MirrorPolicyError,
        )

    # Assert
    assert "HTTP 500" in str(error)
    assert not [
        request for request in mirror_server.requests if request.method == "POST"
    ]
