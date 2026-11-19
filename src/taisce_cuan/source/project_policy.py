"""Combine the tag and branch protection rules required for a mirror project."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from taisce_cuan.source.branch_policy import (
    BranchProtectionRule,
    reconcile_branch_policy,
    required_branch_rule,
)
from taisce_cuan.source.gitlab_api import MirrorPolicyError
from taisce_cuan.source.mirror_policy import (
    TagProtectionRule,
    reconcile_tag_policy,
    required_tag_rules,
)


@dataclass(frozen=True)
class ProjectPolicy:
    """Hold every protection rule that one mirror project must satisfy.

    Attributes:
        tag_rules: Required protected tag rules.
        branch_rule: Required protected branch rule.
    """

    tag_rules: tuple[TagProtectionRule, ...]
    branch_rule: BranchProtectionRule


def required_project_policy(
    profile: str,
    forge_url: str,
    group: str,
    release_user_ids: list[int] | None,
    bot_user_id: int | None,
) -> ProjectPolicy:
    """Resolve the tag and branch rules for an explicit policy profile.

    Args:
        profile: Required policy profile name.
        forge_url: Canonical HTTPS origin of the GitLab forge, with no path.
        group: Canonical GitLab namespace for the mirrors, with no edge slashes.
        release_user_ids: Configured users allowed to create release tags.
        bot_user_id: Configured pipeline bot for the pin tags and the branch rule.

    Returns:
        The complete tag and branch policy.

    Raises:
        MirrorPolicyError: The profile, target, release users, or bot are invalid.
    """
    return ProjectPolicy(
        required_tag_rules(profile, forge_url, group, release_user_ids, bot_user_id),
        required_branch_rule(bot_user_id),
    )


def _apply_incomplete(tags: dict, branch: dict, branch_failed: bool) -> bool:
    """Report whether a write may have left the project partly protected.

    A branch inventory failure is reported only after tag writes, so it always
    counts as incomplete. A tag or branch write that did not reach ready also counts.
    """
    return (
        branch_failed
        or (tags["writes_attempted"] and not tags["ready"])
        or (branch["writes_attempted"] and not branch["ready"])
    )


def reconcile_project_policy(
    client: httpx.Client,
    forge_url: str,
    project_id: int,
    headers: dict,
    policy: ProjectPolicy,
    *,
    apply: bool = False,
) -> dict:
    """Inspect or apply the tag and branch rules without letting either block the other.

    Tag rules run first. A tag conflict never blocks the branch rule, and a branch
    conflict never blocks tag rules. A failed tag write also does not skip the branch
    step; the branch rule is created or checked, and apply_incomplete then stops the
    batch. If the branch inventory fails before any branch
    write, the error is raised only when no tag write happened. After a tag write,
    the error is reported so the created audit trail survives.

    Args:
        client: HTTP client for the GitLab API.
        forge_url: GitLab origin.
        project_id: Numeric GitLab project ID.
        headers: API request headers.
        policy: The required tag and branch rules.
        apply: Create missing rules when no existing permissions conflict.

    Returns:
        The tag report keys, with errors and ready combined from both parts, plus
        branch_rules, branch_created, and apply_incomplete.

    Raises:
        MirrorPolicyError: The tag inventory fails, or the branch inventory fails
            before any write.
    """
    tags = reconcile_tag_policy(
        client, forge_url, project_id, headers, policy.tag_rules, apply=apply
    )
    branch_failed = False
    try:
        branch = reconcile_branch_policy(
            client, forge_url, project_id, headers, policy.branch_rule, apply=apply
        )
    except MirrorPolicyError as exc:
        if not tags["writes_attempted"]:
            raise
        branch_failed = True
        branch = {
            "project_id": project_id,
            "ready": False,
            "rules": [],
            "created": [],
            "errors": [str(exc)],
            "writes_attempted": False,
        }
    return {
        "project_id": project_id,
        "ready": tags["ready"] and branch["ready"],
        "rules": tags["rules"],
        "created": tags["created"],
        "errors": tags["errors"] + branch["errors"],
        "writes_attempted": tags["writes_attempted"] or branch["writes_attempted"],
        "branch_rules": branch["rules"],
        "branch_created": branch["created"],
        "apply_incomplete": _apply_incomplete(tags, branch, branch_failed),
    }
