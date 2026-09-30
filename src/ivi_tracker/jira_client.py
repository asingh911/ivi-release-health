"""Thin Jira Cloud REST client for the public AGL instance.

Uses `requests` directly rather than a Jira library: the old /rest/api/3/search
endpoint was removed from Jira Cloud, and many libraries still call it. Search
goes through /rest/api/3/search/jql, which paginates with nextPageToken.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

DEFAULT_BASE_URL = "https://lf-automotivelinux.atlassian.net"
USER_AGENT = "ivi-release-health (+https://github.com/asingh911/ivi-release-health)"

# Fields requested for every issue. `description` is skipped: in API v3 it is Atlassian Document Format JSON.
ISSUE_FIELDS = [
    "summary", "issuetype", "status", "priority", "components", "fixVersions",
    "labels", "created", "updated", "resolutiondate", "assignee",
]

PAGE_SIZE = 100
PAGE_PAUSE_S = 0.3
MAX_RETRIES = 5


class JiraClient:
    """Sequential, polite client. Tracks `api_calls` for the run log."""

    def __init__(self, base_url: str | None = None, email: str | None = None,
                 api_token: str | None = None, session: requests.Session | None = None):
        self.base_url = (base_url or os.getenv("JIRA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        email = email or os.getenv("JIRA_EMAIL")
        api_token = api_token or os.getenv("JIRA_API_TOKEN")
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        # Anonymous by default; Basic auth only when both email and token are set.
        if email and api_token:
            self.session.auth = (email, api_token)
        self.api_calls = 0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(MAX_RETRIES):
            self.api_calls += 1
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                # Honor Retry-After on rate limits; back off exponentially on server errors.
                wait = int(r.headers.get("Retry-After", 2 ** attempt * 5))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts (last status {r.status_code})")

    def search_all(self, jql: str, fields: list[str] = ISSUE_FIELDS,
                   page_size: int = PAGE_SIZE) -> list[dict]:
        """Return every issue matching `jql`, following nextPageToken until the last page."""
        issues: list[dict] = []
        token: str | None = None
        while True:
            params = {"jql": jql, "fields": ",".join(fields), "maxResults": page_size}
            if token:
                params["nextPageToken"] = token
            data = self._get("/rest/api/3/search/jql", params)
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            # Pages can be shorter than maxResults, so only the token/isLast signal the end.
            if not token or data.get("isLast"):
                return issues
            time.sleep(PAGE_PAUSE_S)

    def get_versions(self, project_key: str) -> list[dict]:
        return self._get(f"/rest/api/3/project/{project_key}/versions")

    def get_components(self, project_key: str) -> list[dict]:
        return self._get(f"/rest/api/3/project/{project_key}/components")

    def get_changelog(self, issue_key: str) -> list[dict]:
        """All changelog entries for one issue (paginated with startAt on this endpoint)."""
        entries: list[dict] = []
        start = 0
        while True:
            data = self._get(f"/rest/api/3/issue/{issue_key}/changelog",
                             {"startAt": start, "maxResults": 100})
            values = data.get("values", [])
            entries.extend(values)
            if data.get("isLast", True) or not values:
                return entries
            start += len(values)
            time.sleep(PAGE_PAUSE_S)
