"""
Copyright (C) 2026 Lightwell

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

         http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import annotations

import urllib.parse


def is_valid_url(url: str) -> bool:
    """Return True if ``url`` is a well-formed absolute URL.

    A URL is considered valid when it has both a scheme (e.g. ``https``)
    and a network location (host). This performs syntactic validation only;
    it does not check whether the URL is reachable.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return bool(parsed.scheme and parsed.netloc)


def is_https_url(url: str) -> bool:
    """Return True if ``url`` is a valid URL using the ``https`` scheme."""
    if not is_valid_url(url):
        return False
    return urllib.parse.urlparse(url).scheme == "https"
