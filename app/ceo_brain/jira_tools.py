"""FR-CB2-4.7 — Local Jira tools for the CEO Brain responder.

The Atlassian Remote MCP endpoint (``mcp.atlassian.com/v1/mcp``),
when authenticated with a plain API token (Bearer), exposes only the
two Rovo "Teamwork Graph" tools (``getTeamworkGraphContext`` /
``getTeamworkGraphObject``) — graph-traversal helpers, *not* a JQL
search. The full Jira tool surface (``searchJiraIssuesUsingJql`` &
co.) is only reachable via the OAuth connector, which the headless
Messages-API bot can't drive. So status questions like «сколько задач
в on hold» came back empty.

The same API token *does* work against the Jira Cloud REST API
(Basic auth: ``email:token``). So we expose JQL search + issue fetch
as regular Anthropic ``tools`` and execute them locally — identical
shape to ``slack_tools`` (no external MCP, no OAuth dance).

Module shape mirrors ``slack_tools``:
    - ``JIRA_TOOL_SCHEMAS`` — Anthropic tool definitions.
    - ``build_jira_executors(site_url, email, api_token)`` — returns
      ``{tool_name: callable(input_dict) -> json_str}``.
    - ``build_jira_executors_from_env()`` — convenience wrapper that
      sources creds from ``ATLASSIAN_CLOUD_ID`` / ``ATLASSIAN_EMAIL``
      / ``ATLASSIAN_API_TOKEN``; returns ``({}, [])`` when unset.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable

import requests

from app.logging_setup import get_logger

log = get_logger(__name__)


# Enhanced JQL search (the legacy ``/rest/api/3/search`` was removed
# by Atlassian in May 2025; ``/search/jql`` is the replacement).
_SEARCH_PATH = "/rest/api/3/search/jql"
_ISSUE_PATH = "/rest/api/3/issue/{key}"
_DEFAULT_FIELDS = [
    "summary",
    "status",
    "assignee",
    "project",
    "priority",
    "updated",
    "duedate",
]
_MAX_RESULT_CHARS = 8000


JIRA_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "jira_search",
        "description": (
            "Поиск задач (issues) в Jira Cloud по JQL. Используй для "
            "любых вопросов про статусы/количество/списки задач — "
            "например «сколько задач в on hold», «что в работе у X». "
            "JQL-примеры: `status = \"On Hold\"`, "
            "`project = \"CEO Brain\" AND status = \"On Hold\"`, "
            "`assignee = currentUser() AND statusCategory != Done`. "
            "Возвращает ключ, заголовок, статус, исполнителя, проект, "
            "срок и ссылку. Поле `total` — общее число совпадений."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {
                    "type": "string",
                    "description": (
                        "JQL-запрос. Названия статусов в кавычках, если "
                        "в них есть пробел (\"On Hold\")."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Сколько задач вернуть (1-50).",
                    "default": 50,
                },
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_get_issue",
        "description": (
            "Детали одной задачи Jira по ключу (например `ENG-123`): "
            "заголовок, статус, исполнитель, описание, срок, ссылка."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "Ключ задачи, напр. `ENG-123`.",
                },
            },
            "required": ["issue_key"],
        },
    },
]


def _truncate(s: str, n: int = _MAX_RESULT_CHARS) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[truncated {len(s) - n} chars]"


def _normalize_site_url(site: str) -> str:
    """Accept ``sklvc.atlassian.net`` or a full URL; return a clean
    ``https://host`` base with no trailing slash."""
    site = (site or "").strip().rstrip("/")
    if not site:
        return ""
    if not site.startswith("http://") and not site.startswith("https://"):
        site = "https://" + site
    return site


def _strip_issue(issue: dict, base_url: str) -> dict:
    """Keep only fields the model actually needs."""
    f = issue.get("fields") or {}
    status = f.get("status") or {}
    assignee = f.get("assignee") or {}
    project = f.get("project") or {}
    key = issue.get("key")
    return {
        "key": key,
        "summary": f.get("summary"),
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("name"),
        "assignee": assignee.get("displayName"),
        "project": project.get("key") or project.get("name"),
        "duedate": f.get("duedate"),
        "updated": f.get("updated"),
        "url": f"{base_url}/browse/{key}" if (base_url and key) else None,
    }


def build_jira_executors(
    *,
    site_url: str,
    email: str,
    api_token: str,
) -> dict[str, Callable[[dict[str, Any]], str]]:
    """Build name→callable map. Each callable returns a JSON string
    for the ``content`` field of a ``tool_result`` block.

    Auth is HTTP Basic ``email:api_token`` — the standard Jira Cloud
    REST scheme (works with the same ``ATATT…`` token that the MCP
    endpoint only half-supports)."""
    base_url = _normalize_site_url(site_url)
    token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    def _ok(payload: Any) -> str:
        return _truncate(json.dumps(payload, ensure_ascii=False, default=str))

    def _err(msg: str) -> str:
        return _ok({"error": msg})

    def jira_search(inp: dict) -> str:
        jql = (inp.get("jql") or "").strip()
        if not jql:
            return _err("jira_search requires `jql`")
        max_results = min(max(int(inp.get("max_results") or 50), 1), 50)
        try:
            r = requests.post(
                base_url + _SEARCH_PATH,
                headers=headers,
                json={
                    "jql": jql,
                    "maxResults": max_results,
                    "fields": _DEFAULT_FIELDS,
                },
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ceo_brain_jira_search_failed", error=str(e))
            return _err(f"jira_search_request_failed: {e}")
        if r.status_code != 200:
            return _err(
                f"jira_search_http_{r.status_code}: {_truncate(r.text, 500)}"
            )
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return _err(f"jira_search_bad_json: {e}")
        issues = data.get("issues") or []
        return _ok({
            "jql": jql,
            "total": data.get("total", len(issues)),
            "returned": len(issues),
            "issues": [_strip_issue(i, base_url) for i in issues],
        })

    def jira_get_issue(inp: dict) -> str:
        key = (inp.get("issue_key") or "").strip()
        if not key:
            return _err("jira_get_issue requires `issue_key`")
        try:
            r = requests.get(
                base_url + _ISSUE_PATH.format(key=key),
                headers=headers,
                params={"fields": ",".join(_DEFAULT_FIELDS + ["description"])},
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ceo_brain_jira_get_issue_failed", error=str(e))
            return _err(f"jira_get_issue_request_failed: {e}")
        if r.status_code != 200:
            return _err(
                f"jira_get_issue_http_{r.status_code}: {_truncate(r.text, 500)}"
            )
        try:
            issue = r.json()
        except Exception as e:  # noqa: BLE001
            return _err(f"jira_get_issue_bad_json: {e}")
        out = _strip_issue(issue, base_url)
        out["description"] = (issue.get("fields") or {}).get("description")
        return _ok(out)

    return {
        "jira_search": jira_search,
        "jira_get_issue": jira_get_issue,
    }


def build_jira_executors_from_env() -> tuple[
    dict[str, Callable[[dict[str, Any]], str]], list[dict[str, Any]]
]:
    """Source creds from env and build executors + schemas.

    Returns ``({}, [])`` (no tools) when any of site / email / token
    is missing, so the responder can wire Jira opportunistically.

    Env:
        - ``ATLASSIAN_CLOUD_ID``  — site URL or host (``sklvc.atlassian.net``)
        - ``ATLASSIAN_EMAIL``     — account email that owns the token
        - ``ATLASSIAN_API_TOKEN`` — the ``ATATT…`` API token
    """
    site = os.environ.get("ATLASSIAN_CLOUD_ID", "").strip()
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if not (site and email and token):
        log.info(
            "ceo_brain_jira_tools_disabled",
            have_site=bool(site), have_email=bool(email),
            have_token=bool(token),
        )
        return {}, []
    execs = build_jira_executors(site_url=site, email=email, api_token=token)
    log.info("ceo_brain_jira_tools_enabled", tools=list(execs.keys()))
    return execs, list(JIRA_TOOL_SCHEMAS)


__all__ = [
    "JIRA_TOOL_SCHEMAS",
    "build_jira_executors",
    "build_jira_executors_from_env",
]
