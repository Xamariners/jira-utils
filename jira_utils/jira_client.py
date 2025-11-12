from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from requests import Response, Session

from .config import JiraConfig


class JiraAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int, payload: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


@dataclass
class JiraClient:
    config: JiraConfig

    def __post_init__(self) -> None:
        self.session: Session = requests.Session()
        self.session.auth = (self.config.email, self.config.api_token)
        self.session.verify = self.config.verify_ssl
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.base_url = self.config.normalized_base_url

    def search_issues(
        self, jql: str, max_results: int = 50, next_page_token: Optional[str] = None
    ) -> dict:
        """
        Search issues using the enhanced POST /rest/api/3/search/jql endpoint.

        We optimistically request lightweight fields (summary, project, attachment) and
        page through results using the returned nextPageToken. Older tenants can still
        return the legacy payload (with startAt/total), so we normalize both shapes.
        """

        url = f"{self.base_url}/rest/api/3/search/jql"
        payload: Dict[str, object] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "project", "attachment"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        response = self.session.post(url, json=payload, timeout=30)
        self._raise_for_status(response, f"Failed to search issues with JQL: {jql}")
        data = response.json()
        normalized = self._normalize_search_response(data)
        normalized["warnings"] = normalized.get("warnings", []) or data.get("warningMessages", [])
        return normalized

    def delete_attachment(self, attachment_id: str) -> None:
        url = f"{self.base_url}/rest/api/3/attachment/{attachment_id}"
        response = self.session.delete(url, timeout=30)
        self._raise_for_status(response, f"Failed to delete attachment {attachment_id}")

    def get_issue_comments(self, issue_key: str) -> List[dict]:
        comments: List[dict] = []
        start_at = 0
        max_results = 100
        while True:
            url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
            params = {"startAt": start_at, "maxResults": max_results}
            response = self.session.get(url, params=params, timeout=30)
            self._raise_for_status(response, f"Failed to fetch comments for issue {issue_key}")
            payload = response.json()
            values = payload.get("comments", [])
            comments.extend(values)
            if start_at + len(values) >= payload.get("total", 0):
                break
            start_at += len(values)
        return comments

    def delete_issue_comment(self, issue_key: str, comment_id: str) -> None:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment/{comment_id}"
        response = self.session.delete(url, timeout=30)
        self._raise_for_status(
            response, f"Failed to delete comment {comment_id} on issue {issue_key}"
        )

    def close(self) -> None:
        self.session.close()

    def list_projects(self) -> List[dict]:
        url = f"{self.base_url}/rest/api/3/project/search"
        projects: List[dict] = []
        start_at = 0
        max_results = 100
        while True:
            params = {"startAt": start_at, "maxResults": max_results}
            response = self.session.get(url, params=params, timeout=30)
            self._raise_for_status(response, "Failed to list Jira projects.")
            payload = response.json()
            values = payload.get("values") or payload.get("projects") or []
            for project in values:
                key = project.get("key")
                name = project.get("name", key or "<unknown>")
                if key:
                    projects.append({"key": key, "name": name})
            is_last = payload.get("isLast")
            if is_last is True or not values:
                break
            start_at += len(values)
        return projects

    def list_issue_types(self) -> List[dict]:
        url = f"{self.base_url}/rest/api/3/issuetype"
        response = self.session.get(url, timeout=30)
        self._raise_for_status(response, "Failed to list Jira issue types.")
        payload = response.json()
        values = payload if isinstance(payload, list) else payload.get("values", [])
        results: List[dict] = []
        for item in values:
            name = item.get("name")
            if name:
                results.append({"id": item.get("id"), "name": name})
        return results

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:  # noqa: D401
        self.close()

    def _raise_for_status(self, response: Response, message: str) -> None:
        if 200 <= response.status_code < 300:
            return

        details: Dict = {}
        try:
            details = response.json()
        except Exception:  # noqa: broad-except
            pass

        error_messages = details.get("errorMessages")
        if isinstance(error_messages, list):
            joined = "; ".join(error_messages)
            message = f"{message}: {joined}"

        raise JiraAPIError(message, response.status_code, details)
    def _normalize_search_response(self, data: Dict[str, object]) -> Dict[str, object]:
        if "issues" in data:
            return {
                "issues": data.get("issues", []),
                "nextPageToken": data.get("nextPageToken"),
                "isLast": data.get("isLast", not data.get("nextPageToken")),
            }

        # Legacy/beta payloads returned under data["queries"][0]
        queries = data.get("queries")
        if isinstance(queries, list) and queries:
            query = queries[0] or {}
            return {
                "issues": query.get("issues", []),
                "nextPageToken": query.get("nextPageToken"),
                "isLast": query.get("isLast", not query.get("nextPageToken")),
                "warnings": query.get("warnings", []),
            }

        return {"issues": [], "nextPageToken": None, "isLast": True, "warnings": []}
