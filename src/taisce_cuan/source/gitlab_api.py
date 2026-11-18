"""Call GitLab REST endpoints with bounded retries and strict list pagination."""

from __future__ import annotations

import time

import httpx


class MirrorPolicyError(RuntimeError):
    """The requested mirror policy cannot be verified or applied."""


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
