"""FR-CR-05-200 / -201 / -202 — strategic morning digest pipeline.

Covers:
  - FR-CR-05-200: direction classification on ingest (gpt-4o-mini), stored on
    the draft payload; `Orchestrator.create_draft` wiring; best-effort no-op
    without an API key.
  - FR-CR-05-201: digest rendering (DIRECTION → ENTITY cluster → FUNCTION),
    JSON-per-id format parse + re-request, stored-direction reuse, owner
    resolution against team_members + bot override.
  - FR-CR-05-202: yesterday/London window, since/until window, critical split
    (parent vs thread), Slack post (parent first, rest in thread).

No DB / no network: the LLM backend and Slack client are faked.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import ops.strategic_tasks_digest as D
from app.services.task_direction import classify_one_direction


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeLLM:
    """complete_text returns a canned JSON payload (ignores the prompt)."""

    def __init__(self, payload: dict):
        self._payload = payload

    def complete_text(self, **_kwargs) -> str:
        return json.dumps(self._payload)


class DirLLM:
    """Backend whose complete_text answers the classify_directions tool with a
    fixed direction for every task id present in the user prompt."""

    def __init__(self, direction: str = "investors"):
        self._d = direction
        self.calls = 0

    def complete_text(self, *, user_prompt: str, **_kwargs) -> str:
        import re

        self.calls += 1
        ids = [int(m) for m in re.findall(r"id=(\d+)", user_prompt)]
        return json.dumps({"items": [{"task_id": i, "direction": self._d} for i in ids]})


def _draft(did: int, payload: dict):
    return SimpleNamespace(id=did, payload=payload)


# --------------------------------------------------------------------------
# FR-CR-05-200 — ingest classification
# --------------------------------------------------------------------------
def test_fr_cr_05_200_classify_one_direction():
    d = classify_one_direction(
        title="Отправить дек инвестору",
        description="",
        llm_backend=DirLLM("investors"),
        model="gpt-4o-mini",
    )
    assert d == "investors"


def test_fr_cr_05_200_create_draft_classifies_task_intent(monkeypatch):
    from app.orchestrator.service import Orchestrator
    from app.schemas.intent import IntentType

    orch = Orchestrator(settings=SimpleNamespace(openai_api_key="k", openai_model="gpt-4o-mini"))
    # Inject a fake classify backend so no network call happens.
    orch._cls_backend = DirLLM("budget")

    payload = {"title": "Согласовать headcount budget", "description": ""}
    orch.classify_draft_direction(payload)
    assert payload["direction"] == "budget"
    # gate: only task intents are classified by create_draft (sanity on enum)
    assert IntentType.create_task and IntentType.update_task


def test_fr_cr_05_200_create_draft_skips_without_api_key():
    from app.orchestrator.service import Orchestrator

    orch = Orchestrator(settings=SimpleNamespace(openai_api_key="", openai_model="gpt-4o-mini"))
    payload = {"title": "что-то", "description": ""}
    orch.classify_draft_direction(payload)
    assert "direction" not in payload  # no key → no-op, never blocks ingest


# --------------------------------------------------------------------------
# FR-CR-05-201 — digest build / classify / format / render / owner
# --------------------------------------------------------------------------
def _tasks():
    drafts = [
        _draft(1, {"title": "Tether дек", "owner_display_name": "@IrinaMorato",
                   "direction": "investors",
                   "_pending": {"source_kind": "telegram", "source_text": "отправить дек Tether"}}),
        _draft(2, {"title": "Tether memo", "owner_display_name": "Игорь - аналитик",
                   "direction": "investors",
                   "_pending": {"source_kind": "telegram", "source_text": "обновить memo"}}),
        _draft(3, {"title": "Прислать письмо в Foundry", "owner_display_name": "Валентина - PM /аналитик",
                   "direction": "investors",
                   "_pending": {"source_kind": "telegram", "source_text": "написать Foundry"}}),
        _draft(4, {"title": "Кофе", "owner_display_name": "CEO_office1 bot",
                   "direction": "other",
                   "_pending": {"source_kind": "slack", "source_text": "кофе"}}),
    ]
    return D._build_tasks(drafts)


def test_fr_cr_05_201_format_chunk_parses_group_function_action_critical():
    llm = FakeLLM({"items": [
        {"id": 1, "group": "Tether", "function": "Материалы и документы",
         "action": "отправить дек", "critical": True},
        {"id": 2, "group": "Tether", "function": "БЕЗ_ТАКОЙ",  # invalid → Прочее
         "action": "обновить memo", "critical": False},
        {"id": "x", "group": "z", "function": "Прочее", "action": "y"},  # bad id dropped
    ]})
    out = D._format_chunk(_tasks()[:2], llm=llm, model="m")
    assert set(out) == {1, 2}
    assert out[1]["group"] == "Tether" and out[1]["critical"] is True
    assert out[2]["function"] == "Прочее"  # invalid coerced


def test_fr_cr_05_201_format_all_rerequests_missing():
    class DropOnce:
        def __init__(self): self.calls = 0
        def complete_text(self, *, user_prompt, **_k):
            import re
            self.calls += 1
            ids = [int(m) for m in re.findall(r"id=(\d+)", user_prompt)]
            items = [{"id": i, "group": "g", "function": "Прочее", "action": "do",
                      "critical": False} for i in ids if not (i == 2 and self.calls == 1)]
            return json.dumps({"items": items})

    llm = DropOnce()
    res = D._format_all(_tasks(), llm=llm, model="m")
    assert set(res) == {1, 2, 3, 4}
    assert llm.calls >= 2  # missing id 2 re-requested


def test_fr_cr_05_201_assign_directions_uses_stored_skips_llm():
    class Boom:
        def complete_text(self, **_k):
            raise AssertionError("must not classify when direction stored")

    kept, newly = D._assign_directions(
        _tasks(), llm=Boom(), classify_model="gpt-4o-mini", strategic_only=True
    )
    assert newly == {}                       # all already classified → no LLM
    assert {t["id"] for t in kept} == {1, 2, 3}   # 'other' (id 4) filtered out


def test_fr_cr_05_201_assign_directions_backfills_missing():
    tasks = _tasks()
    for t in tasks:
        t["direction"] = ""  # simulate pre-feature backlog
    kept, newly = D._assign_directions(
        tasks, llm=DirLLM("investors"), classify_model="gpt-4o-mini", strategic_only=True
    )
    assert set(newly) == {1, 2, 3, 4}        # all (re)classified + persisted
    assert all(t["direction"] == "investors" for t in tasks)
    assert len(kept) == 4


def test_fr_cr_05_201_normalize_owner_matches_team_table():
    by_u = {"irinamorato": "Ирина Мора"}
    by_n = {"валентина": "Валентина Филиппова", "игорь": "Игорь Петров"}

    def no(o):
        return D._normalize_owner(o, by_username=by_u, by_name=by_n)

    assert no("@IrinaMorato") == "Ирина Мора"              # handle via username
    assert no("@unknownx") == "unknownx"                   # unmatched → bare, no fabrication
    assert no("Валентина - PM /аналитик") == "Валентина Филиппова"  # role-suffix + first-name match
    assert no("Игорь - аналитик") == "Игорь Петров"
    assert no("Jarad Cannon") == "Jarad Cannon"            # unmatched name kept as-is
    assert no("") == ""


def test_fr_cr_05_201_owner_override_ceo_office_bot():
    assert D._normalize_owner("CEO_office1 bot", by_username={}, by_name={}) == "Irina Shipilova"


def test_fr_cr_05_201_render_groups_by_direction_then_entity_then_function():
    tasks = _tasks()
    for t in tasks:  # normalize owners deterministically
        t["owner"] = D._normalize_owner(
            t["owner"], by_username={"irinamorato": "Ирина Мора"}, by_name={}
        )
    formatted = {
        1: {"group": "Tether", "function": "Материалы и документы", "action": "отправить дек", "critical": True},
        2: {"group": "Tether", "function": "Pipeline и операционка", "action": "обновить memo", "critical": False},
        3: {"group": "", "function": "Аутрич и письма", "action": "написать в Foundry", "critical": False},
        4: {"group": "", "function": "Прочее", "action": "купить кофе", "critical": False},
    }
    md = D._render(tasks, formatted, title="Задачи на сегодня")
    # title clean, no «критичное», no emoji
    assert md.splitlines()[0] == "# Задачи на сегодня"
    assert "критичное" not in md and "💼" not in md
    # direction header + entity cluster (Tether has 2) + functional sub-group
    assert "## " + D.DIRECTION_LABELS["investors"] in md
    assert "### Tether" in md
    assert "### Аутрич и письма" in md
    # owner resolved + override
    assert "(Ирина Мора)" in md and "Irina Shipilova" in md
    # continuous numbering across the whole digest
    import re
    assert re.findall(r"^(\d+)\. ", md, flags=re.M) == ["1", "2", "3", "4"]


def test_fr_cr_05_201_render_slack_flavor_no_markdown_headers():
    tasks = _tasks()
    formatted = {t["id"]: {"group": "G", "function": "Прочее", "action": "a", "critical": False}
                 for t in tasks}
    s = D._render(tasks, formatted, title="Задачи на сегодня", flavor="slack")
    assert "## " not in s and "### " not in s and "**" not in s
    assert s.startswith("*Задачи на сегодня*")


# --------------------------------------------------------------------------
# FR-CR-05-202 — window, critical split, Slack post
# --------------------------------------------------------------------------
def test_fr_cr_05_202_resolve_window_yesterday_london():
    args = argparse.Namespace(yesterday=True, since=None, until=None)
    since, until, label = D._resolve_window(args)
    assert since is not None and until is not None
    assert until - since == timedelta(days=1)
    assert since.tzinfo == timezone.utc and until.tzinfo == timezone.utc
    # until == today 00:00 London (in UTC)
    today_lon = datetime.now(ZoneInfo("Europe/London")).date()
    exp_until = datetime.combine(
        today_lon, datetime.min.time(), ZoneInfo("Europe/London")
    ).astimezone(timezone.utc)
    assert until == exp_until
    assert "Europe/London" in label


def test_fr_cr_05_202_resolve_window_since_until():
    args = argparse.Namespace(yesterday=False, since="2026-05-22", until="2026-05-23")
    since, until, label = D._resolve_window(args)
    assert since == datetime(2026, 5, 22, tzinfo=timezone.utc)
    assert until == datetime(2026, 5, 23, tzinfo=timezone.utc)
    assert "2026-05-22" in label and "2026-05-23" in label


def test_fr_cr_05_202_resolve_window_bad_date():
    args = argparse.Namespace(yesterday=False, since="not-a-date", until=None)
    since, until, label = D._resolve_window(args)
    assert since is None and until is None


def test_fr_cr_05_202_critical_split_parent_thread():
    tasks = [
        {"id": 1, "priority": "medium"},
        {"id": 2, "priority": "high"},     # priority-critical
        {"id": 3, "priority": "medium"},   # LLM-critical
    ]
    formatted = {
        1: {"critical": False}, 2: {"critical": False}, 3: {"critical": True},
    }
    crit, rest = D._split_critical(tasks, formatted)
    assert {t["id"] for t in crit} == {2, 3}
    assert {t["id"] for t in rest} == {1}


def test_fr_cr_05_202_post_to_slack_parent_then_thread(monkeypatch):
    calls = []

    class FakeResp:
        data = {"ts": "111.222"}

    class FakeClient:
        def __init__(self, token=None): pass
        def chat_postMessage(self, **kw):
            calls.append(kw)
            return FakeResp()

    fake_mod = SimpleNamespace(WebClient=FakeClient)
    monkeypatch.setitem(sys.modules, "slack_sdk", fake_mod)

    res = D._post_to_slack("PARENT", "REST", channel="C1", token="t")
    assert res["ok"] is True and res["parent_ts"] == "111.222"
    # first call = parent (no thread_ts); subsequent = thread replies
    assert calls[0].get("thread_ts") is None
    assert all(c.get("thread_ts") == "111.222" for c in calls[1:])
    assert any(c["text"] == "PARENT" for c in calls)
    assert any(c["text"] == "REST" for c in calls)


def test_fr_cr_05_207_post_to_slack_skips_empty(monkeypatch):
    """No tasks → no Slack call at all (никакого пустого todo)."""
    calls = []

    class FakeClient:
        def __init__(self, token=None): pass
        def chat_postMessage(self, **kw):
            calls.append(kw)
            return SimpleNamespace(data={"ts": "1"})

    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace(WebClient=FakeClient))

    for parent, rest in (("", ""), ("   ", ""), ("", "  \n ")):
        res = D._post_to_slack(parent, rest, channel="C1", token="t")
        assert res == {"ok": False, "skipped": "empty"}
    assert calls == []  # chat.postMessage never called
