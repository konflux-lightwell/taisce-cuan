"""Verify and provision the explicit GitLab Python mirror tag policy."""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

_PROFILE = "python-mirror"
_ROLE_LEVELS = {0, 5, 10, 15, 20, 30, 40, 50, 60}


class MirrorPolicyError(RuntimeError):
    """The requested mirror policy cannot be verified or applied."""


@dataclass(frozen=True)
class TagProtectionRule:
    """Describe a tag pattern and its exact user or role creation grants.

    Attributes:
        name: GitLab protected tag pattern.
        user_ids: Users allowed to create tags matching the pattern.
        access_level: GitLab role allowed to create tags, if not user-specific.
    """

    name: str
    user_ids: tuple[int, ...] = ()
    access_level: int | None = None


def _valid_user_id(user_id: object) -> bool:
    """Return True for a positive integer GitLab user ID, rejecting booleans."""
    return type(user_id) is int and user_id > 0


def _canonical_target(forge_url: object, group: object) -> bool:
    """Return True for a credential-free HTTPS origin and a clean namespace.

    The target comes from configuration, so only its shape is checked here.
    """
    if not isinstance(forge_url, str) or not isinstance(group, str):
        return False
    try:
        parts = urlsplit(forge_url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and forge_url == f"https://{parts.netloc}"
        and bool(group)
        and group == group.strip("/")
    )


def required_tag_rules(
    profile: str,
    forge_url: str,
    group: str,
    release_user_ids: list[int] | None,
    bot_user_id: int | None,
) -> tuple[TagProtectionRule, ...]:
    """Resolve an explicit policy profile and validate its target and creators.

    Args:
        profile: Required policy profile name.
        forge_url: Canonical HTTPS origin of the GitLab forge, with no path.
        group: Canonical GitLab namespace for the mirrors, with no edge slashes.
        release_user_ids: Configured users allowed to create release tags.
        bot_user_id: Configured pipeline bot allowed to create pin tags. It is
            supplied at runtime so no identity is committed to this repository.

    Returns:
        The complete, ordered set of required tag protection rules.

    Raises:
        MirrorPolicyError: The profile, target, release users, or bot are invalid.
    """
    if profile != _PROFILE:
        raise MirrorPolicyError("Unsupported mirror policy profile")
    if not _canonical_target(forge_url, group):
        raise MirrorPolicyError("Mirror policy requires a canonical forge and group")
    if (
        not isinstance(release_user_ids, list)
        or not release_user_ids
        or not all(_valid_user_id(user_id) for user_id in release_user_ids)
    ):
        raise MirrorPolicyError("Mirror policy requires positive release user IDs")
    if not _valid_user_id(bot_user_id):
        raise MirrorPolicyError("Mirror policy requires a positive bot user ID")
    release_ids = tuple(sorted(set(release_user_ids)))
    # Pattern names must stay in sync with the downstream consumers' protected
    # tag pattern list and docs/mirror-policy.md (LWLP-5235). The configured
    # release and bot grants intentionally stay out of the repository.
    return (
        TagProtectionRule("baseline/*", release_ids),
        TagProtectionRule("novel-head/*", release_ids),
        *(
            TagProtectionRule(name, (bot_user_id,))
            for name in (
                "pnc/*",
                "alignment/*",
                "builddep/*",
                "reproducer-pin/*",
                "constituent-pin/*",
            )
        ),
        TagProtectionRule("sme-verified/*", access_level=30),
    )


def gitlab_get(
    client: httpx.Client, url: str, headers: dict, params: dict | None = None
) -> httpx.Response:
    """GET one GitLab resource, retrying only bounded rate-limit responses.

    Args:
        client: HTTP client for the GitLab API.
        url: GitLab endpoint to read.
        headers: API request headers.
        params: Optional request parameters.

    Returns:
        The successful HTTP 200 response.

    Raises:
        MirrorPolicyError: The request fails or its retry delay is invalid.
    """
    for attempt in range(3):
        try:
            response = client.get(
                url, headers=headers, params=params, follow_redirects=False
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            raise MirrorPolicyError("GitLab GET request failed") from None
        if response.status_code == 200:
            return response
        if response.status_code != 429:
            raise MirrorPolicyError(
                f"GitLab GET request failed: HTTP {response.status_code}"
            )
        delay = response.headers.get("Retry-After", "1")
        digits = delay.lstrip("0") or "0"
        if (
            not delay.isascii()
            or not delay.isdecimal()
            or len(digits) > 2
            or int(digits) > 60
        ):
            raise MirrorPolicyError("GitLab rate limit has an invalid retry delay")
        if attempt < 2:
            time.sleep(max(1, int(digits)))
    raise MirrorPolicyError("GitLab GET request failed: HTTP 429 after retries")


def gitlab_list(
    client: httpx.Client, url: str, headers: dict, params: dict | None = None
) -> list[dict]:
    """Read every page of a GitLab list, rejecting incomplete or malformed results.

    Args:
        client: HTTP client for the GitLab API.
        url: GitLab list endpoint, used for every page.
        headers: API request headers.
        params: Optional filters, with pagination set by this function.

    Returns:
        All objects from the complete list.

    Raises:
        MirrorPolicyError: A request, result, or pagination sequence is invalid.
    """
    results: list[dict] = []
    query = dict(params or {})
    query["per_page"] = 100
    for page in range(1, 1001):
        query["page"] = page
        response = gitlab_get(client, url, headers, query)
        try:
            data = response.json()
        except ValueError:
            raise MirrorPolicyError("GitLab list returned malformed JSON") from None
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            raise MirrorPolicyError("GitLab list returned malformed results")
        results.extend(data)
        next_page = response.headers.get("X-Next-Page")
        if next_page is None:
            if len(data) < 100:
                return results
        elif not next_page:
            return results
        elif next_page != str(page + 1):
            raise MirrorPolicyError("GitLab list returned invalid pagination")
    raise MirrorPolicyError("GitLab list exceeded the page limit")


def _validated_project(
    data: object, forge_url: str, path: str, *, allow_archived: bool = False
) -> tuple[str, int]:
    """Validate project identity, canonical URL, and explicit archive state.

    Args:
        data: GitLab project response.
        forge_url: Canonical GitLab origin.
        path: Expected full project path.
        allow_archived: Permit archived projects only for read-only classification.

    Returns:
        The validated repository URL and numeric project ID.

    Raises:
        MirrorPolicyError: The project or archive exception is invalid.
    """
    if type(allow_archived) is not bool:
        raise MirrorPolicyError("GitLab project archive exception is invalid")
    if not isinstance(data, dict):
        raise MirrorPolicyError("GitLab project identity is malformed")
    project_id = data.get("id")
    url = data.get("http_url_to_repo")
    if (
        type(project_id) is not int
        or project_id <= 0
        or data.get("path_with_namespace") != path
        or url != f"{forge_url}/{path}.git"
        or type(data.get("archived")) is not bool
        or (data["archived"] and not allow_archived)
    ):
        raise MirrorPolicyError("GitLab project identity does not match mirror policy")
    return url, project_id


def _grants(entries: object) -> set[tuple[str, int]]:
    """Parse GitLab creator grants without confusing user grants with roles."""
    if not isinstance(entries, list):
        raise MirrorPolicyError("Protected tag creator grants are malformed")
    grants: set[tuple[str, int]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MirrorPolicyError("Protected tag creator grant is malformed")
        if entry.get("deploy_key_id") is not None:
            raise MirrorPolicyError("Protected tag creator grant is unrecognized")
        user_id, group_id, level = (
            entry.get("user_id"),
            entry.get("group_id"),
            entry.get("access_level"),
        )
        if level is not None and (type(level) is not int or level not in _ROLE_LEVELS):
            raise MirrorPolicyError("Protected tag creator grant is unrecognized")
        if (
            user_id is not None
            and group_id is None
            and type(user_id) is int
            and user_id > 0
        ):
            grants.add(("user", user_id))
        elif (
            group_id is not None
            and user_id is None
            and type(group_id) is int
            and group_id > 0
        ):
            grants.add(("group", group_id))
        elif (
            user_id is None
            and group_id is None
            and type(level) is int
            and level in _ROLE_LEVELS
        ):
            grants.add(("role", level))
        else:
            raise MirrorPolicyError("Protected tag creator grant is unrecognized")
    return grants


def _desired_grants(rule: TagProtectionRule) -> set[tuple[str, int]]:
    """Validate a desired rule and return its exact creation grants."""
    if (
        not isinstance(rule.name, str)
        or not rule.name.endswith("/*")
        or "*" in rule.name[:-1]
    ):
        raise MirrorPolicyError("Mirror policy rule pattern is malformed")
    if rule.user_ids and rule.access_level is None:
        if any(type(user_id) is not int or user_id <= 0 for user_id in rule.user_ids):
            raise MirrorPolicyError("Mirror policy rule contains invalid user IDs")
        return {("user", user_id) for user_id in rule.user_ids}
    if (
        not rule.user_ids
        and type(rule.access_level) is int
        and rule.access_level in _ROLE_LEVELS
    ):
        return {("role", rule.access_level)}
    raise MirrorPolicyError("Mirror policy rule has invalid creator grants")


def _overlaps(name: str, required: str) -> bool:
    """Conservatively identify rules that could match a required tag namespace."""
    prefix = name.split("*", 1)[0]
    required_prefix = required[:-1]
    if "*" not in name:
        return name.startswith(required_prefix)
    return prefix.startswith(required_prefix) or required_prefix.startswith(prefix)


def _within(grants: set[tuple[str, int]], desired: set[tuple[str, int]]) -> bool:
    """Check whether an overlapping rule can grant additional creator access."""
    for kind, value in grants:
        if (kind, value) in desired or (kind == "role" and value == 0):
            continue
        if kind == "role" and any(
            wanted_kind == "role" and wanted_level > 0 and value >= wanted_level
            for wanted_kind, wanted_level in desired
        ):
            continue
        return False
    return True


def _grant_records(grants: set[tuple[str, int]]) -> list[dict]:
    """Describe normalized creator grants in a deterministic audit format."""
    return [{"kind": kind, "value": value} for kind, value in sorted(grants)]


def _evaluate(
    existing: list[dict], rules: tuple[TagProtectionRule, ...]
) -> tuple[list[dict], list[TagProtectionRule], list[str]]:
    """Compare all existing rules and overlaps with the requested policy."""
    parsed: list[tuple[str, set[tuple[str, int]]]] = []
    for entry in existing:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise MirrorPolicyError("Protected tag rule name is malformed")
        parsed.append((name, _grants(entry.get("create_access_levels"))))

    reports: list[dict] = []
    missing: list[TagProtectionRule] = []
    errors: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        desired = _desired_grants(rule)
        if rule.name in seen:
            raise MirrorPolicyError("Mirror policy contains duplicate rules")
        seen.add(rule.name)
        matches = [grants for name, grants in parsed if name == rule.name]
        if not matches:
            missing.append(rule)
            state = "missing"
        elif len(matches) != 1 or matches[0] != desired:
            state = "divergent"
            errors.append(f"Protected tag policy differs for {rule.name}")
        else:
            state = "ready"
        overlaps = []
        for name, grants in parsed:
            if name == rule.name or not _overlaps(name, rule.name):
                continue
            compatible = _within(grants, desired)
            overlaps.append(
                {
                    "name": name,
                    "grants": _grant_records(grants),
                    "compatible": compatible,
                }
            )
            if not compatible:
                errors.append(
                    f"Overlapping protected tag policy {name!r} grants extra creators "
                    f"for {rule.name}"
                )
        reports.append(
            {
                "name": rule.name,
                "state": state,
                "expected": _grant_records(desired),
                "observed": [_grant_records(grants) for grants in matches],
                "overlaps": sorted(overlaps, key=lambda overlap: overlap["name"]),
            }
        )
    return reports, missing, errors


def reconcile_tag_policy(
    client: httpx.Client,
    forge_url: str,
    project_id: int,
    headers: dict,
    rules: tuple[TagProtectionRule, ...],
    *,
    apply: bool = False,
) -> dict:
    """Inspect all tag rules and optionally create missing rules with readback.

    Args:
        client: HTTP client for the GitLab API.
        forge_url: GitLab origin.
        project_id: Numeric GitLab project ID.
        headers: API request headers.
        rules: Required tag protection rules.
        apply: Create missing rules when no existing permissions conflict.

    Returns:
        A report with project_id, ready, rules, created, errors, and
        writes_attempted. Created rules are recorded even if a later request or
        readback fails. writes_attempted is False when nothing was sent, such as
        when existing permissions conflict.

    Raises:
        MirrorPolicyError: The initial inventory or required rules are invalid.
    """
    if type(project_id) is not int or project_id <= 0 or not rules:
        raise MirrorPolicyError("Mirror policy requires a project ID and rules")
    url = f"{forge_url}/api/v4/projects/{project_id}/protected_tags"
    reports, missing, errors = _evaluate(gitlab_list(client, url, headers), rules)
    created: list[str] = []
    writes_attempted = False
    if apply and not errors:
        for rule in tuple(missing):
            if rule not in missing:
                continue
            payload: dict = {"name": rule.name}
            if rule.user_ids:
                payload["allowed_to_create"] = [
                    {"user_id": uid} for uid in rule.user_ids
                ]
            else:
                payload["create_access_level"] = rule.access_level
            writes_attempted = True
            # Never replay POST automatically, even after a rate limit. Earlier
            # creates may have succeeded; readback and a fresh run inspect live state.
            try:
                response = client.post(
                    url, headers=headers, json=payload, follow_redirects=False
                )
            except (httpx.HTTPError, httpx.InvalidURL):
                errors.append(f"Protected tag policy creation failed for {rule.name}")
                response = None
            if response is not None and response.status_code in (200, 201):
                created.append(rule.name)
            elif response is not None and response.status_code not in (409, 422):
                errors.append(
                    f"Protected tag policy creation failed for {rule.name}: "
                    f"HTTP {response.status_code}"
                )
            try:
                reports, remaining, readback_errors = _evaluate(
                    gitlab_list(client, url, headers), rules
                )
            except MirrorPolicyError:
                errors.append("Protected tag policy readback failed")
                break
            missing = remaining
            errors.extend(readback_errors)
            if any(item.name == rule.name for item in remaining):
                errors.append(f"Protected tag policy was not verified for {rule.name}")
            if errors:
                break
    errors.extend(f"Protected tag policy is missing {rule.name}" for rule in missing)
    return {
        "project_id": project_id,
        "ready": not errors,
        "rules": reports,
        "created": created,
        "errors": errors,
        "writes_attempted": writes_attempted,
    }
