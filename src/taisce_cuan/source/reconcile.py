"""Inspect and provision the explicitly selected Python mirror policy."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from taisce_cuan.source.mirror_policy import (
    MirrorPolicyError,
    _validated_project,
    gitlab_get,
    gitlab_list,
    reconcile_tag_policy,
    required_tag_rules,
)

logger = logging.getLogger(__name__)
_MAX_PROJECTS = 25
_PYTHON_NAME = re.compile(r"pypi\.org-[a-z0-9][a-z0-9._-]*")
# Exclusion reason for a direct pypi.org-* project whose path needs human review.
_UNREVIEWED_PATH = "unreviewed-python-path"


def _save_report(report_path: Path, report: dict) -> None:
    """Atomically replace the owned report with a synced private snapshot."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=report_path.parent, delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, report_path)
        temporary_path = None
    except Exception:
        raise OSError("Cannot persist mirror reconciliation report") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.error("Cannot remove temporary mirror reconciliation report")


def _classify_inventory(
    rows: list[dict], forge_url: str, group: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """Validate identities and separate eligible, excluded, and archived rows."""
    active: list[dict] = []
    excluded: list[dict] = []
    archived: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        project_id, name = row.get("id"), row.get("path")
        if type(project_id) is not int or project_id <= 0 or project_id in seen:
            raise MirrorPolicyError(
                "Inventory contains an invalid or duplicate project ID"
            )
        seen.add(project_id)
        if not isinstance(name, str) or not name:
            raise MirrorPolicyError("Inventory contains a malformed project path")
        if not name.startswith("pypi.org-"):
            excluded.append({"id": project_id, "reason": "not-python"})
            continue
        path = row.get("path_with_namespace")
        namespace = row.get("namespace")
        if (
            not isinstance(path, str)
            or not isinstance(namespace, dict)
            or not isinstance(namespace.get("full_path"), str)
            or path != f"{namespace['full_path']}/{name}"
        ):
            raise MirrorPolicyError("Inventory contains a mismatched project identity")
        if namespace["full_path"] != group:
            excluded.append({"id": project_id, "reason": "outside-direct-namespace"})
            continue
        if not _PYTHON_NAME.fullmatch(name):
            # Keep the audit going, but never let an unreviewed mirror count as ready.
            excluded.append({"id": project_id, "reason": _UNREVIEWED_PATH})
            continue
        _validated_project(row, forge_url, f"{group}/{name}", allow_archived=True)
        identity = {"id": project_id, "path": path}
        (archived if row["archived"] else active).append(identity)
    return sorted(active, key=lambda row: row["id"]), excluded, archived


def _valid_options(
    auth_token: str | None,
    apply: bool,
    max_projects: int | None,
    after_project_id: int | None,
) -> None:
    """Reject missing authentication and unsafe batch bounds before HTTP requests."""
    if not isinstance(auth_token, str) or not auth_token.strip():
        raise MirrorPolicyError("Mirror reconciliation requires GitLab authentication")
    if apply and max_projects is None:
        raise MirrorPolicyError("Apply requires a project limit between 1 and 25")
    if max_projects is not None and (
        type(max_projects) is not int or not 1 <= max_projects <= _MAX_PROJECTS
    ):
        raise MirrorPolicyError("Project limit must be between 1 and 25")
    if after_project_id is not None and (
        type(after_project_id) is not int or after_project_id < 0
    ):
        raise MirrorPolicyError("Project cursor must be a nonnegative integer")


def _inspect_projects(
    client: httpx.Client,
    report_path: Path,
    report: dict,
    active: list[dict],
    forge_url: str,
    headers: dict,
    rules: tuple,
    apply: bool,
    max_projects: int | None,
    after_project_id: int | None,
) -> None:
    """Record each snapshot, then optionally apply and record its readback."""
    candidates = [
        p for p in active if after_project_id is None or p["id"] > after_project_id
    ]
    selected = candidates[:max_projects] if max_projects is not None else candidates
    report["selected_ids"] = [p["id"] for p in selected]
    report["remaining_ids"] = [p["id"] for p in active]
    _save_report(report_path, report)
    for project in selected:
        project_id = project["id"]
        outcome = {
            **project,
            "intent": "apply" if apply else "inspect",
            "before": None,
            "after": None,
        }
        report["projects"].append(outcome)
        _save_report(report_path, report)
        try:
            outcome["before"] = reconcile_tag_policy(
                client, forge_url, project_id, headers, rules
            )
            # Persist the before snapshot and intent before any POST.
            _save_report(report_path, report)
            if apply:
                response = gitlab_get(
                    client, f"{forge_url}/api/v4/projects/{project_id}", headers
                )
                try:
                    data = response.json()
                except ValueError:
                    raise MirrorPolicyError(
                        "GitLab project identity is malformed"
                    ) from None
                url, live_id = _validated_project(data, forge_url, project["path"])
                if live_id != project_id:
                    raise MirrorPolicyError(
                        "GitLab project ID does not match inventory"
                    )
                outcome["validated_identity"] = {
                    "id": live_id,
                    "path": project["path"],
                    "http_url_to_repo": url,
                    "archived": False,
                }
                _save_report(report_path, report)
                outcome["after"] = reconcile_tag_policy(
                    client, forge_url, project_id, headers, rules, apply=True
                )
                _save_report(report_path, report)
                if (
                    not outcome["after"]["ready"]
                    and outcome["after"]["writes_attempted"]
                ):
                    # A write may be partial, so stop and keep the rest untouched.
                    report["errors"].append("Project policy apply did not complete")
                    break
            report["remaining_ids"].remove(project_id)
            _save_report(report_path, report)
        except MirrorPolicyError as exc:
            report["errors"].append(
                f"Project policy inspection or readback failed: {exc}"
            )
            _save_report(report_path, report)
            if apply:
                break
    report["inspection_complete"] = (
        not report["remaining_ids"]
        and not report["errors"]
        and max_projects is None
        and after_project_id is None
    )
    report["fleet_ready"] = bool(
        report["inventory_complete"]
        and report["inspection_complete"]
        and not report["archived"]
        and not report["errors"]
        and report["projects"]
        and not any(item["reason"] == _UNREVIEWED_PATH for item in report["excluded"])
        and all(
            (project["after"] if apply else project["before"])["ready"]
            for project in report["projects"]
        )
    )
    _save_report(report_path, report)


def reconcile_mirrors(
    *,
    forge_url: str,
    group: str,
    auth_token: str | None,
    release_user_ids: list[int] | None,
    bot_user_id: int | None,
    report_path: str | Path,
    mirror_policy: str,
    apply: bool = False,
    max_projects: int | None = None,
    after_project_id: int | None = None,
) -> int:
    """Reconcile a fresh, complete inventory and write an exclusive JSON audit.

    Args:
        forge_url: Canonical GitLab origin to reconcile.
        group: Canonical GitLab namespace holding the direct mirrors.
        auth_token: GitLab API credential, never written to the report.
        release_user_ids: Expected release tag creator IDs.
        bot_user_id: Expected pipeline bot ID for the pin tag rules.
        report_path: New path for the audit report; existing files are refused.
        mirror_policy: Explicit policy profile.
        apply: Create missing rules after the inventory and snapshot are saved.
        max_projects: Optional batch limit, required for apply and capped at 25.
        after_project_id: Optional numeric cursor excluding the prefix.

    Returns:
        Zero only for a complete, ready audit with no archived mirrors.
    """
    report = {
        "intent": "apply" if apply else "inspect",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "mirror_policy": mirror_policy if mirror_policy == "python-mirror" else None,
        "max_projects": max_projects if type(max_projects) is int else None,
        "after_project_id": after_project_id if type(after_project_id) is int else None,
        "total_inventory": None,
        "inventory_complete": False,
        "inspection_complete": False,
        "fleet_ready": False,
        "excluded": [],
        "archived": [],
        "selected_ids": [],
        "projects": [],
        "remaining_ids": None,
        "errors": [],
    }
    report_path = Path(report_path)
    try:
        fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        logger.error("Cannot create a new mirror reconciliation report")
        return 1
    try:
        _save_report(report_path, report)
        _valid_options(auth_token, apply, max_projects, after_project_id)
        rules = required_tag_rules(
            mirror_policy, forge_url, group, release_user_ids, bot_user_id
        )
        report.update(forge_url=forge_url, group=group)
        headers = {"PRIVATE-TOKEN": auth_token}
        with httpx.Client(timeout=15.0, verify=True) as client:
            rows = gitlab_list(
                client,
                f"{forge_url}/api/v4/groups/{quote(group, safe='')}/projects",
                headers,
                # Do not send simple=true: only the full project entity has
                # "archived". Rows are filtered by path in _classify_inventory.
                params={
                    "include_subgroups": "false",
                    "with_shared": "false",
                    "order_by": "id",
                    "sort": "asc",
                },
            )
            active, excluded, archived = _classify_inventory(rows, forge_url, group)
            report.update(
                total_inventory=len(rows),
                inventory_complete=True,
                excluded=excluded,
                archived=archived,
            )
            _save_report(report_path, report)
            _inspect_projects(
                client,
                report_path,
                report,
                active,
                forge_url,
                headers,
                rules,
                apply,
                max_projects,
                after_project_id,
            )
    except MirrorPolicyError as exc:
        # These errors are generated locally and never include API response bodies.
        report["errors"].append(str(exc))
        logger.error("Mirror reconciliation failed: %s", exc)
        try:
            _save_report(report_path, report)
        except OSError:
            logger.error("Cannot persist mirror reconciliation report")
    except OSError:
        logger.error("Cannot persist mirror reconciliation report")
        return 1
    except Exception:
        # Exclude exception strings, response bodies, and credentials.
        report["fleet_ready"] = False
        report["inspection_complete"] = False
        report["errors"].append("Mirror reconciliation stopped unexpectedly")
        try:
            _save_report(report_path, report)
        except OSError:
            logger.error("Cannot persist mirror reconciliation report")
    if not report["fleet_ready"]:
        logger.error("Mirror reconciliation is incomplete or not ready; see the report")
        return 1
    return 0
