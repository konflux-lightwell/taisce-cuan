"""Verify and provision the protected branch rule for verification metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from taisce_cuan.source.gitlab_api import MirrorPolicyError, gitlab_create, gitlab_list
from taisce_cuan.source.mirror_policy import (
    _grant_records,
    _grants,
    _valid_user_id,
    _within,
)

VERIFICATION_METADATA_BRANCH = "verification-metadata"
# 🏷️ Label used in GitLab error text for push and merge access grants.
_LABEL = "Protected branch access"
_MAINTAINER_LEVEL = 40

_Grants = set[tuple[str, int]]
# 🧾 Parsed push, merge, and unprotect grants, plus the force-push flag.
_Parsed = tuple[bool, _Grants, _Grants, _Grants]


@dataclass(frozen=True)
class BranchProtectionRule:
    """Describe a protected branch and its exact push and merge user grants.

    Attributes:
        name: GitLab protected branch name.
        user_ids: Users allowed to push to and merge into the branch.
    """

    name: str
    user_ids: tuple[int, ...]


def required_branch_rule(bot_user_id: int | None) -> BranchProtectionRule:
    """Build the verification metadata rule for the configured pipeline bot.

    Args:
        bot_user_id: Configured pipeline bot, the only user allowed to push or merge.
            It is supplied at runtime so no identity is committed to this repository.

    Returns:
        The required protected branch rule.

    Raises:
        MirrorPolicyError: The bot ID is not a positive integer.
    """
    if not _valid_user_id(bot_user_id):
        raise MirrorPolicyError("Mirror policy requires a positive bot user ID")
    return BranchProtectionRule(VERIFICATION_METADATA_BRANCH, (bot_user_id,))


def _matches(pattern: str, branch: str) -> bool:
    """Match a GitLab branch pattern, where only * is a wildcard and case matters."""
    # 🔍 Escape every literal part so dots and dashes never act as regex syntax.
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, branch) is not None


def _parse_rule(entry: dict) -> _Parsed | None:
    """Parse one matching rule, returning None when any part cannot be verified.

    Args:
        entry: One GitLab protected branch readback.

    Returns:
        The parsed grants, or None so the caller reports the rule as divergent.
    """
    force = entry.get("allow_force_push")
    if type(force) is not bool:
        return None
    try:
        return (
            force,
            _grants(entry.get("push_access_levels"), _LABEL),
            _grants(entry.get("merge_access_levels"), _LABEL),
            _grants(entry.get("unprotect_access_levels"), _LABEL),
        )
    except MirrorPolicyError:
        return None


def _exact_ready(parsed: _Parsed | None, desired: _Grants) -> bool:
    """Return True only when an exact-name rule matches the required policy.

    Unprotect access must be empty or limited to roles at Maintainer level or above.
    """
    if parsed is None:
        return False
    force, push, merge, unprotect = parsed
    return (
        not force
        and push == desired
        and merge == desired
        and all(
            kind == "role" and level >= _MAINTAINER_LEVEL for kind, level in unprotect
        )
    )


def _branch_record(parsed: _Parsed | None) -> dict | None:
    """Describe one exact rule's parsed grants for the audit, or None if unparsed."""
    if parsed is None:
        return None
    force, push, merge, unprotect = parsed
    return {
        "allow_force_push": force,
        "push": _grant_records(push),
        "merge": _grant_records(merge),
        "unprotect": _grant_records(unprotect),
    }


def _overlap(entry: dict, desired: _Grants) -> dict:
    """Describe a matching wildcard rule and whether it stays within the policy.

    A wildcard is compatible only when it forbids force pushes and grants no push or
    merge access beyond the desired users. An unparsed rule is never compatible.
    """
    parsed = _parse_rule(entry)
    if parsed is None:
        return {"name": entry["name"], "grants": [], "compatible": False}
    force, push, merge, _ = parsed
    return {
        "name": entry["name"],
        "grants": _grant_records(push | merge),
        "compatible": not force and _within(push, desired) and _within(merge, desired),
    }


def _evaluate_branch(
    existing: list[dict], rule: BranchProtectionRule
) -> tuple[dict, list[str]]:
    """Compare the required branch rule with every existing rule that matches it.

    Args:
        existing: Complete GitLab protected branch readback.
        rule: The required protected branch rule.

    Returns:
        A per-rule report and the list of errors that block readiness.

    Raises:
        MirrorPolicyError: A listed rule name is missing or malformed.
    """
    if any(
        not isinstance(entry.get("name"), str) or not entry["name"]
        for entry in existing
    ):
        raise MirrorPolicyError("Protected branch rule name is malformed")
    desired: _Grants = {("user", user_id) for user_id in rule.user_ids}
    matching = [entry for entry in existing if _matches(entry["name"], rule.name)]
    exact = [entry for entry in matching if entry["name"] == rule.name]
    overlaps = [
        _overlap(entry, desired) for entry in matching if entry["name"] != rule.name
    ]
    errors = [
        f"Overlapping protected branch policy {overlap['name']!r} grants extra access "
        f"for {rule.name}"
        for overlap in overlaps
        if not overlap["compatible"]
    ]
    parsed_exact = [_parse_rule(entry) for entry in exact]
    if not exact:
        state = "missing"
    elif len(exact) == 1 and _exact_ready(parsed_exact[0], desired):
        state = "ready"
    else:
        state = "divergent"
        errors.append(f"Protected branch policy differs for {rule.name}")
    report = {
        "name": rule.name,
        "state": state,
        "expected": _grant_records(desired),
        "observed": [_branch_record(parsed) for parsed in parsed_exact],
        "overlaps": sorted(overlaps, key=lambda overlap: overlap["name"]),
    }
    return report, errors


def reconcile_branch_policy(
    client: httpx.Client,
    forge_url: str,
    project_id: int,
    headers: dict,
    rule: BranchProtectionRule,
    *,
    apply: bool = False,
) -> dict:
    """Inspect the branch rule and optionally create it when it is missing.

    Args:
        client: HTTP client for the GitLab API.
        forge_url: GitLab origin.
        project_id: Numeric GitLab project ID.
        headers: API request headers.
        rule: The required protected branch rule.
        apply: Create the rule only when it is missing and nothing conflicts.

    Returns:
        A report with project_id, ready, rules, created, errors, and
        writes_attempted, in the same shape as the tag policy report. A created
        rule is recorded even if readback fails.

    Raises:
        MirrorPolicyError: The project ID is invalid, or the initial inventory fails
            before any write.
    """
    if type(project_id) is not int or project_id <= 0:
        raise MirrorPolicyError("Mirror policy requires a project ID and a branch rule")
    url = f"{forge_url}/api/v4/projects/{project_id}/protected_branches"
    report, errors = _evaluate_branch(gitlab_list(client, url, headers), rule)
    created: list[str] = []
    writes_attempted = False
    if apply and not errors and report["state"] == "missing":
        writes_attempted = True
        payload = {
            "name": rule.name,
            "allow_force_push": False,
            "allowed_to_push": [{"user_id": uid} for uid in rule.user_ids],
            "allowed_to_merge": [{"user_id": uid} for uid in rule.user_ids],
        }
        was_created, error = gitlab_create(
            client,
            url,
            headers,
            payload,
            f"Protected branch policy creation failed for {rule.name}",
        )
        if was_created:
            created.append(rule.name)
        if error:
            errors.append(error)
        try:
            report, readback_errors = _evaluate_branch(
                gitlab_list(client, url, headers), rule
            )
        except MirrorPolicyError:
            errors.append("Protected branch policy readback failed")
        else:
            errors.extend(readback_errors)
            if report["state"] == "missing":
                errors.append(
                    f"Protected branch policy was not verified for {rule.name}"
                )
    if report["state"] == "missing":
        errors.append(f"Protected branch policy is missing {rule.name}")
    return {
        "project_id": project_id,
        "ready": not errors,
        "rules": [report],
        "created": created,
        "errors": errors,
        "writes_attempted": writes_attempted,
    }
