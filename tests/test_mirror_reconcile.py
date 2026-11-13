"""Exercise fleet reconciliation through a stateful GitLab HTTP fake."""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import pytest

FORGE = "https://gitlab.example.com"
GROUP = "mirrors/python"
TOKEN = "offline-secret-token"
BOT_ID = 7001
RELEASE_ID = 7002
RELEASE_CREATORS = [BOT_ID, RELEASE_ID]


def project(project_id, name, *, group=GROUP, archived=False):
    """Build a direct or shared project in GitLab's full project layout."""
    path = f"{group}/{name}"
    return {
        "id": project_id,
        "path": name,
        "path_with_namespace": path,
        "namespace": {"full_path": group},
        "http_url_to_repo": f"{FORGE}/{path}.git",
        "archived": archived,
    }


def ready_rules():
    """Build the expected GitLab protected-tag readback."""

    def users(ids):
        """Represent user-specific creator grants."""
        return [{"user_id": uid, "access_level": 40} for uid in ids]

    return {
        "baseline/*": users(RELEASE_CREATORS),
        "novel-head/*": users(RELEASE_CREATORS),
        **{
            name: users([BOT_ID])
            for name in (
                "pnc/*",
                "alignment/*",
                "builddep/*",
                "reproducer-pin/*",
                "constituent-pin/*",
            )
        },
        "sme-verified/*": [{"access_level": 30}],
    }


@dataclass
class GitLab:
    """Serve inventory pages and persist protected-tag changes in memory."""

    pages: list = field(default_factory=lambda: [[]])
    rules: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    fail_page: int | None = None
    fail_rule: str | None = None
    fail_project: int | None = None
    report_path: object = None
    snapshots: list = field(default_factory=list)
    live_projects: dict = field(default_factory=dict)
    live_responses: list = field(default_factory=list)
    fail_status: int = 503

    def respond(self, request):
        """Answer real HTTP requests without allowing external access."""
        self.requests.append(request)
        if request.url.path.endswith("/projects"):
            page = int(request.url.params["page"])
            if page == self.fail_page:
                return httpx.Response(429, text=TOKEN)
            next_page = str(page + 1) if page < len(self.pages) else ""
            rows = self.pages[page - 1]
            if request.url.params.get("simple") == "true":
                # Real GitLab omits "archived" from the simple project entity.
                rows = [
                    {k: v for k, v in row.items() if k != "archived"} for row in rows
                ]
            return httpx.Response(200, json=rows, headers={"X-Next-Page": next_page})
        if request.method == "GET" and request.url.path.rsplit("/", 1)[-1].isdigit():
            project_id = int(request.url.path.rsplit("/", 1)[-1])
            if self.live_responses:
                return self.live_responses.pop(0)
            rows = [row for page in self.pages if page for row in page]
            row = self.live_projects.get(
                project_id, next((row for row in rows if row["id"] == project_id), {})
            )
            return httpx.Response(200, json=row)
        project_id = int(request.url.path.split("/")[-2])
        if project_id == self.fail_project:
            return httpx.Response(429, text=TOKEN)
        rules = self.rules.setdefault(project_id, {})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"name": name, "create_access_levels": grants}
                    for name, grants in rules.items()
                ],
                headers={"X-Next-Page": ""},
            )
        self.snapshots.append(json.loads(self.report_path.read_text()))
        payload = json.loads(request.content)
        if payload["name"] == self.fail_rule:
            return httpx.Response(self.fail_status, text=TOKEN)
        rules[payload["name"]] = (
            [
                {"user_id": item["user_id"], "access_level": 40}
                for item in payload["allowed_to_create"]
            ]
            if "allowed_to_create" in payload
            else [{"access_level": payload["create_access_level"]}]
        )
        return httpx.Response(201)


@pytest.fixture
def gitlab(monkeypatch, tmp_path):
    """Route the actual HTTP client to the fake server."""
    server = GitLab(report_path=tmp_path / "audit.json")
    client = httpx.Client

    def factory(*args, **kwargs):
        """Keep the real client behavior while avoiding real network access."""
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    return server


def run(server, **kwargs):
    """Run a reconciliation against the fake with standard configuration."""
    from taisce_cuan.source.reconcile import reconcile_mirrors

    options = {
        "forge_url": FORGE,
        "group": GROUP,
        "auth_token": TOKEN,
        "release_user_ids": RELEASE_CREATORS,
        "bot_user_id": BOT_ID,
        "report_path": server.report_path,
        "mirror_policy": "python-mirror",
    }
    return reconcile_mirrors(**{**options, **kwargs})


def audit(server):
    """Read the JSON audit written by reconciliation."""
    return json.loads(server.report_path.read_text())


def test_inventory_completes_before_apply_and_excludes_other_projects(gitlab):
    """Only direct active Python mirrors are eligible, across all pages."""
    # Arrange
    gitlab.pages = [
        [project(20, "pypi.org-joserfc"), project(7, "maven.org-foo")],
        [
            project(10, "pypi.org-mistune"),
            project(30, "pypi.org-retired", archived=True),
            project(40, "pypi.org-shared", group="other/group"),
            project(41, "pypi.org-subgroup", group=GROUP + "/child"),
        ],
    ]
    # Act
    result = run(gitlab, apply=True, max_projects=25)
    report = audit(gitlab)
    # Assert
    assert result == 1  # A bounded batch is never a full fleet audit.
    assert [p["id"] for p in report["projects"]] == [10, 20]
    assert {p["id"] for p in report["excluded"]} == {7, 40, 41}
    assert [p["id"] for p in report["archived"]] == [30]
    assert report["total_inventory"] == 6
    assert report["inventory_complete"] and not report["fleet_ready"]
    methods = [(r.method, r.url.path) for r in gitlab.requests]
    assert all(method == "GET" for method, _ in methods[:2])
    assert all(path.endswith("/projects") for _, path in methods[:2])
    assert gitlab.snapshots[0]["projects"][0]["before"]["rules"]
    assert gitlab.snapshots[0]["projects"][0]["intent"] == "apply"
    assert gitlab.snapshots[0]["projects"][0]["validated_identity"] == {
        "id": 10,
        "path": f"{GROUP}/pypi.org-mistune",
        "http_url_to_repo": f"{FORGE}/{GROUP}/pypi.org-mistune.git",
        "archived": False,
    }
    assert TOKEN not in json.dumps(report)


def test_later_inventory_page_failure_never_mutates(gitlab):
    """An incomplete listing stops before any policy request or POST."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")], []]
    gitlab.fail_page = 2
    # Act
    result = run(gitlab, apply=True, max_projects=1)
    # Assert
    report = audit(gitlab)
    assert result == 1 and not report["inventory_complete"]
    assert report["total_inventory"] is None and not report["fleet_ready"]
    assert not any(r.method == "POST" for r in gitlab.requests)


@pytest.mark.parametrize(
    "changes",
    [
        {"path_with_namespace": "another/group/pypi.org-one"},
        {"id": 99},
        {"http_url_to_repo": "https://elsewhere/repo.git"},
        {"archived": True},
        {"archived": None},
    ],
)
def test_live_identity_change_blocks_apply(gitlab, changes):
    """Reject a transferred, changed, or archived project after inventory."""
    # Arrange
    row = project(1, "pypi.org-one")
    gitlab.pages = [[row], []]
    gitlab.live_projects[1] = {**row, **changes}

    # Act
    result = run(gitlab, apply=True, max_projects=1)

    # Assert
    report = audit(gitlab)
    assert result == 1 and report["inventory_complete"]
    assert not report["fleet_ready"] and report["remaining_ids"] == [1]
    assert report["errors"] and not any(r.method == "POST" for r in gitlab.requests)
    assert len([r for r in gitlab.requests if r.url.path.endswith("/projects")]) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(206, json={}),
        httpx.Response(302, headers={"Location": "https://elsewhere.example"}),
        httpx.Response(200, text="{"),
    ],
)
def test_invalid_live_identity_response_blocks_apply(gitlab, response):
    """Require a complete, valid, nonredirected identity response before writes."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    gitlab.live_responses = [response]

    # Act
    result = run(gitlab, apply=True, max_projects=1)

    # Assert
    assert result == 1 and audit(gitlab)["errors"]
    assert not any(r.method == "POST" for r in gitlab.requests)
    assert TOKEN not in json.dumps(audit(gitlab))


@pytest.mark.parametrize("recover", [True, False])
def test_preapply_identity_rate_limit_is_retried(gitlab, recover):
    """Retry a rate-limited live identity and stop when attempts are exhausted."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    gitlab.live_responses = [httpx.Response(429)] * (1 if recover else 3)

    # Act
    result = run(gitlab, apply=True, max_projects=1)

    # Assert
    requests = [r for r in gitlab.requests if r.url.path.endswith("/projects/1")]
    assert result == 1 and len(requests) == (2 if recover else 3)
    assert any(r.method == "POST" for r in gitlab.requests) is recover
    if recover:
        assert audit(gitlab)["projects"][0]["after"]["ready"]
    else:
        assert audit(gitlab)["errors"]


def test_rate_limited_post_is_not_retried(gitlab):
    """A write rate limit stops the batch rather than retrying the POST."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    gitlab.fail_rule = "baseline/*"
    gitlab.fail_status = 429

    # Act
    result = run(gitlab, apply=True, max_projects=1)

    # Assert
    assert result == 1 and not audit(gitlab)["projects"][0]["after"]["ready"]
    assert len([r for r in gitlab.requests if r.method == "POST"]) == 1


@pytest.mark.parametrize("active", [False, True])
def test_archived_mirrors_prevent_fleet_readiness(gitlab, active):
    """Archived-only and mixed inventories require archive review."""
    # Arrange
    rows = [project(3, "pypi.org-retired", archived=True)]
    if active:
        rows.append(project(1, "pypi.org-one"))
        gitlab.rules = {1: ready_rules()}
    gitlab.pages = [rows]

    # Act
    result = run(gitlab)

    # Assert
    report = audit(gitlab)
    assert result == 1 and report["inspection_complete"]
    assert not report["fleet_ready"] and report["archived"][0]["id"] == 3
    assert not any("/projects/3/" in r.url.path for r in gitlab.requests)


@pytest.mark.parametrize("boundary", ["replace", "dump"])
def test_atomic_snapshot_failure_keeps_previous_audit_and_stops_batch(
    gitlab, monkeypatch, boundary
):
    """A failed replacement after remote writes preserves the prior JSON snapshot."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(2, "pypi.org-two")]]
    real_replace = os.replace
    real_dump = json.dump
    snapshots = []

    def replace(source, destination):
        """Fail the real replacement boundary after the first remote action."""
        if any(request.method == "POST" for request in gitlab.requests):
            snapshots.append(gitlab.report_path.read_bytes())
            raise OSError("disk failure")
        return real_replace(source, destination)

    def dump(data, stream, **kwargs):
        """Fail JSON serialization after the first remote action."""
        if any(request.method == "POST" for request in gitlab.requests):
            snapshots.append(gitlab.report_path.read_bytes())
            stream.write("{partial")
            raise TypeError("serialization failure")
        return real_dump(data, stream, **kwargs)

    if boundary == "replace":
        monkeypatch.setattr(os, "replace", replace)
    else:
        monkeypatch.setattr(json, "dump", dump)

    # Act
    result = run(gitlab, apply=True, max_projects=2)

    # Assert
    assert result == 1 and snapshots
    assert gitlab.report_path.read_bytes() == snapshots[0]
    report = json.loads(snapshots[0])
    assert not report["fleet_ready"] and report["remaining_ids"] == [1, 2]
    assert report["projects"][0]["before"] and report["projects"][0]["after"] is None
    assert gitlab.report_path.stat().st_mode & 0o777 == 0o600
    assert not any("/projects/2/" in r.url.path for r in gitlab.requests)
    assert list(gitlab.report_path.parent.iterdir()) == [gitlab.report_path]


def test_report_records_validated_target_and_utc_start(gitlab):
    """Record the verified target and a timezone-aware UTC audit timestamp."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    gitlab.rules = {1: ready_rules()}
    # Act
    result = run(gitlab)

    # Assert
    report = audit(gitlab)
    assert result == 0 and report["forge_url"] == FORGE and report["group"] == GROUP
    assert datetime.fromisoformat(report["started_at_utc"]).utcoffset() == timedelta(0)


def test_invalid_target_is_not_recorded_in_audit(gitlab):
    """Never write an unvalidated target or credentials into an error report."""
    # Act
    result = run(gitlab, forge_url=f"https://{TOKEN}@gitlab.example.com")

    # Assert
    report = audit(gitlab)
    assert result == 1 and not gitlab.requests
    assert report.get("forge_url") is None and report.get("group") is None
    assert TOKEN not in json.dumps(report)


@pytest.mark.parametrize(
    "options", [{"max_projects": 1}, {"after_project_id": 1}, {"after_project_id": 0}]
)
def test_bounded_or_cursor_audit_never_claims_fleet_ready(gitlab, options):
    """A cap or cursor cannot prove readiness for the whole fresh inventory."""
    # Arrange
    gitlab.pages = [[project(2, "pypi.org-two"), project(1, "pypi.org-one")]]
    gitlab.rules = {1: ready_rules(), 2: ready_rules()}
    # Act
    result = run(gitlab, **options)
    # Assert
    report = audit(gitlab)
    assert result == 1 and not report["fleet_ready"]
    assert not report["inspection_complete"]
    if options.get("after_project_id") == 1:
        assert report["remaining_ids"] == [1]
    if options.get("max_projects") == 1:
        assert report["remaining_ids"] == [2]


@pytest.mark.parametrize("payload", [[], None])
def test_empty_or_malformed_inventory_never_proves_readiness(gitlab, payload):
    """An empty listing proves nothing, so it cannot report a ready fleet."""
    # Arrange
    gitlab.pages = [payload]
    # Act
    result = run(gitlab)
    # Assert
    report = audit(gitlab)
    assert result == 1 and report["fleet_ready"] is False
    assert report["inventory_complete"] is (payload is not None)
    assert report["total_inventory"] == (0 if payload is not None else None)


def test_inventory_query_uses_full_project_entities(gitlab):
    """Ask for full rows, which carry archive state, from the direct group only."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    gitlab.rules = {1: ready_rules()}
    # Act
    result = run(gitlab)
    # Assert
    query = dict(gitlab.requests[0].url.params)
    assert result == 0
    assert query == {
        "include_subgroups": "false",
        "with_shared": "false",
        "order_by": "id",
        "sort": "asc",
        "per_page": "100",
        "page": "1",
    }


def test_unreviewed_python_path_is_excluded_and_blocks_readiness(gitlab):
    """One odd mirror path is recorded, and the other mirrors are still audited."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(2, "pypi.org-Odd_Name")]]
    gitlab.rules = {1: ready_rules()}
    # Act
    result = run(gitlab)
    # Assert
    report = audit(gitlab)
    assert result == 1 and report["inspection_complete"]
    assert report["excluded"] == [{"id": 2, "reason": "unreviewed-python-path"}]
    assert [p["id"] for p in report["projects"]] == [1]
    assert not report["fleet_ready"]


def test_full_ready_audit_is_read_only(gitlab):
    """A fresh inspection of every active project can prove fleet readiness."""
    # Arrange
    gitlab.pages = [[project(2, "pypi.org-two"), project(1, "pypi.org-one")]]
    gitlab.rules = {1: ready_rules(), 2: ready_rules()}
    # Act
    result = run(gitlab)
    # Assert
    report = audit(gitlab)
    assert result == 0 and report["fleet_ready"] and report["inspection_complete"]
    assert [p["id"] for p in report["projects"]] == [1, 2]
    assert not report["remaining_ids"]
    assert not any(r.method == "POST" for r in gitlab.requests)


def test_project_api_failure_stops_remaining_batch(gitlab):
    """Stop after a rate-limited project and keep the untouched IDs visible."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(2, "pypi.org-two")]]
    gitlab.fail_project = 1
    # Act
    result = run(gitlab, apply=True, max_projects=2)
    # Assert
    report = audit(gitlab)
    assert result == 1 and report["remaining_ids"] == [1, 2]
    assert report["inventory_complete"] and not report["fleet_ready"]
    assert not any("/projects/2/protected_tags" in r.url.path for r in gitlab.requests)
    assert TOKEN not in json.dumps(report)


@pytest.mark.parametrize("limit", [None, 0, -1, 26, True])
def test_apply_requires_valid_bounded_batch_before_http(gitlab, limit):
    """Reject absent, nonpositive, non-integer, or excessive apply limits."""
    # Act
    result = run(gitlab, apply=True, max_projects=limit)
    # Assert
    assert result == 1 and not gitlab.requests
    assert not audit(gitlab)["fleet_ready"]


@pytest.mark.parametrize(
    "options",
    [
        {"auth_token": None},
        {"auth_token": "  "},
        {"forge_url": "http://gitlab.example.com"},
        {"group": GROUP + "/"},
        {"release_user_ids": []},
        {"bot_user_id": None},
        {"after_project_id": -1},
        {"max_projects": 26},
    ],
)
def test_invalid_configuration_rejected_before_http(gitlab, options):
    """Reject bad profile configuration, cursor, and read limits without HTTP."""
    # Act
    result = run(gitlab, **options)
    # Assert
    assert result == 1 and not gitlab.requests
    assert audit(gitlab)["errors"] and not audit(gitlab)["fleet_ready"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 0),
        ("http_url_to_repo", "https://elsewhere/repo.git"),
        ("namespace", {"full_path": "elsewhere"}),
    ],
)
def test_bad_selected_identity_blocks_entire_batch(gitlab, field, value):
    """Reject invalid direct Python project identities before mutation."""
    # Arrange
    row = project(1, "pypi.org-one")
    row[field] = value
    gitlab.pages = [[project(2, "pypi.org-two"), row]]
    # Act
    result = run(gitlab, apply=True, max_projects=2)
    # Assert
    assert result == 1 and not audit(gitlab)["fleet_ready"]
    assert not any(r.method == "POST" for r in gitlab.requests)


def test_duplicate_project_ids_block_batch(gitlab):
    """Reject ambiguous IDs across the complete inventory before any POST."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(1, "pypi.org-two")]]
    # Act
    result = run(gitlab, apply=True, max_projects=2)
    # Assert
    assert result == 1 and not audit(gitlab)["inventory_complete"]
    assert not any(r.method == "POST" for r in gitlab.requests)


def test_divergent_grants_are_not_rewritten(gitlab):
    """Leave conflicting rules untouched and report the project as not ready."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    gitlab.rules = {1: {"pnc/*": [{"access_level": 40}]}}
    # Act
    result = run(gitlab, apply=True, max_projects=1)
    # Assert
    assert result == 1 and not audit(gitlab)["projects"][0]["after"]["ready"]
    assert gitlab.rules[1]["pnc/*"] == [{"access_level": 40}]
    assert not any(r.method in ("POST", "DELETE", "PUT") for r in gitlab.requests)


def test_partial_failure_stops_batch_and_retry_reads_live_state(gitlab, tmp_path):
    """Record partial creations and resume from current state in a new report."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(2, "pypi.org-two")]]
    gitlab.fail_rule = "pnc/*"
    # Act
    first = run(gitlab, apply=True, max_projects=2)
    first_report = audit(gitlab)
    first_requests = gitlab.requests[:]
    gitlab.fail_rule = None
    gitlab.report_path = tmp_path / "retry.json"
    second = run(gitlab, apply=True, max_projects=2)
    # Assert
    assert first == second == 1
    assert first_report["projects"][0]["after"]["created"] == [
        "baseline/*",
        "novel-head/*",
    ]
    assert first_report["remaining_ids"] == [1, 2]
    assert not any("/projects/2/protected_tags" in r.url.path for r in first_requests)
    assert audit(gitlab)["projects"][0]["after"]["ready"]
    assert "baseline/*" not in audit(gitlab)["projects"][0]["after"]["created"]


def test_existing_report_is_never_overwritten(gitlab):
    """Protect existing files even if the requested report is source code."""
    # Arrange
    from pathlib import Path

    gitlab.report_path = Path(__file__)
    original = gitlab.report_path.read_bytes()
    # Act
    result = run(gitlab, apply=True, max_projects=1)
    # Assert
    assert result == 1 and gitlab.report_path.read_bytes() == original
    assert not gitlab.requests


def test_full_dry_run_reports_missing_without_mutation(gitlab):
    """A read-only audit with missing protections cannot report readiness."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one")]]
    # Act
    result = run(gitlab)
    # Assert
    report = audit(gitlab)
    assert result == 1 and report["inspection_complete"]
    assert not report["fleet_ready"] and not report["projects"][0]["before"]["ready"]
    assert not any(r.method == "POST" for r in gitlab.requests)


def test_divergent_project_is_skipped_and_batch_continues(gitlab):
    """A conflict with nothing written must not hold back the other mirrors."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(2, "pypi.org-two")]]
    gitlab.rules = {1: {"pnc/*": [{"access_level": 40}]}}
    # Act
    result = run(gitlab, apply=True, max_projects=2)
    # Assert
    report = audit(gitlab)
    assert result == 1 and not report["fleet_ready"]
    assert [p["id"] for p in report["projects"]] == [1, 2]
    assert not report["projects"][0]["after"]["ready"]
    assert report["projects"][1]["after"]["ready"]
    assert not any(
        "/projects/1/" in r.url.path and r.method == "POST" for r in gitlab.requests
    )
    assert gitlab.rules[1] == {"pnc/*": [{"access_level": 40}]}


def test_inspect_records_failed_project_and_audits_the_rest(gitlab):
    """A read-only audit lists every problem mirror instead of stopping at one."""
    # Arrange
    gitlab.pages = [[project(1, "pypi.org-one"), project(2, "pypi.org-two")]]
    gitlab.fail_project = 1
    gitlab.rules = {2: ready_rules()}
    # Act
    result = run(gitlab)
    # Assert
    report = audit(gitlab)
    assert result == 1 and not report["fleet_ready"]
    assert [p["id"] for p in report["projects"]] == [1, 2]
    assert report["projects"][0]["before"] is None
    assert report["projects"][1]["before"]["ready"]
    assert report["remaining_ids"] == [1] and not report["inspection_complete"]
    assert TOKEN not in json.dumps(report)
