"""CR-03 Phase C: completion artifact on Mark done."""
from __future__ import annotations

import pytest

from app.models import Task, TaskStatus
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.task_actions import (
    handle_complete_task_submit,
    handle_mark_done,
)


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


class _ViewCli:
    def __init__(self):
        self.opened: list[dict] = []

    def views_open(self, trigger_id, view):
        self.opened.append({"trigger_id": trigger_id, "view": view})
        return {"ok": True}


# --------------------------------------------------------------------------- #
# complete_task_modal shape
# --------------------------------------------------------------------------- #


def test_complete_task_modal_has_url_and_text_inputs():
    view = bk.complete_task_modal(task_id=7)
    assert view["type"] == "modal"
    assert view["callback_id"] == bk.MODAL_CALLBACK_COMPLETE_TASK
    assert view["private_metadata"] == "7"
    block_ids = [b.get("block_id") for b in view["blocks"] if "block_id" in b]
    assert bk.BLOCK_ARTIFACT in block_ids
    assert bk.BLOCK_ARTIFACT_TEXT in block_ids


# --------------------------------------------------------------------------- #
# Mark done opens the modal instead of transitioning directly
# --------------------------------------------------------------------------- #


def test_mark_done_opens_completion_modal(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(
            title="t",
            status=TaskStatus.in_progress,
            owner_user_id="U1",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    cli = _ViewCli()
    handle_mark_done(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "trigger_id": "trg",
        },
        sender=sender,
        client=cli,
        ack=ack,
    )
    assert cli.opened
    view = cli.opened[0]["view"]
    assert view["callback_id"] == bk.MODAL_CALLBACK_COMPLETE_TASK
    # Task must still be in_progress — no transition yet.
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.in_progress


def test_mark_done_without_client_still_completes(
    patched_session_scope, SessionFactory, ack
):
    """Defensive: if WebClient isn't available, fall back to direct transition."""
    with SessionFactory() as s:
        t = Task(
            title="t",
            status=TaskStatus.in_progress,
            owner_user_id="U1",
        )
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    handle_mark_done(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
        },
        sender=sender,
        client=None,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.done


# --------------------------------------------------------------------------- #
# Modal submit: artifact required, task transitions to done
# --------------------------------------------------------------------------- #


def _ack_recorder():
    captured: dict = {}

    def _ack(response_action=None, errors=None):
        captured["response_action"] = response_action
        captured["errors"] = errors or {}

    return _ack, captured


def test_complete_submit_requires_non_empty_artifact(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.in_progress, owner_user_id="U1")
        s.add(t)
        s.commit()
        tid = t.id

    ack_fn, captured = _ack_recorder()
    view = {
        "state": {
            "values": {
                bk.BLOCK_ARTIFACT: {bk.INPUT_ARTIFACT_URL: {"value": ""}},
                bk.BLOCK_ARTIFACT_TEXT: {bk.INPUT_ARTIFACT_TEXT: {"value": ""}},
            }
        },
        "private_metadata": str(tid),
    }
    handle_complete_task_submit(
        body={"user": {"id": "U1"}},
        view=view,
        sender=_Sender(),
        ack=ack_fn,
    )
    assert captured["response_action"] == "errors"
    assert bk.BLOCK_ARTIFACT in captured["errors"]
    # Task stays in_progress.
    with SessionFactory() as s:
        assert s.get(Task, tid).status == TaskStatus.in_progress


def test_complete_submit_saves_url_artifact_and_transitions(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.in_progress, owner_user_id="U1")
        s.add(t)
        s.commit()
        tid = t.id

    ack_fn, _ = _ack_recorder()
    view = {
        "state": {
            "values": {
                bk.BLOCK_ARTIFACT: {
                    bk.INPUT_ARTIFACT_URL: {"value": "https://docs.example.com/report"}
                },
                bk.BLOCK_ARTIFACT_TEXT: {bk.INPUT_ARTIFACT_TEXT: {"value": ""}},
            }
        },
        "private_metadata": str(tid),
    }
    handle_complete_task_submit(
        body={"user": {"id": "U1"}},
        view=view,
        sender=_Sender(),
        ack=ack_fn,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.status == TaskStatus.done
        assert t.completion_artifact == "https://docs.example.com/report"
        assert t.completion_artifact_kind == "url"
        assert t.completed_at is not None


def test_complete_submit_accepts_text_artifact(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.in_progress, owner_user_id="U1")
        s.add(t)
        s.commit()
        tid = t.id

    ack_fn, _ = _ack_recorder()
    view = {
        "state": {
            "values": {
                bk.BLOCK_ARTIFACT: {bk.INPUT_ARTIFACT_URL: {"value": ""}},
                bk.BLOCK_ARTIFACT_TEXT: {
                    bk.INPUT_ARTIFACT_TEXT: {"value": "Собрал вручную — проверь табличку."}
                },
            }
        },
        "private_metadata": str(tid),
    }
    handle_complete_task_submit(
        body={"user": {"id": "U1"}},
        view=view,
        sender=_Sender(),
        ack=ack_fn,
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.completion_artifact_kind == "text"
        assert "табличку" in t.completion_artifact


def test_complete_submit_refreshes_task_card(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        t = Task(
            title="t",
            status=TaskStatus.in_progress,
            owner_user_id="U1",
            card_channel="C1",
            card_ts="100.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    ack_fn, _ = _ack_recorder()
    sender = _Sender()
    view = {
        "state": {
            "values": {
                bk.BLOCK_ARTIFACT: {bk.INPUT_ARTIFACT_URL: {"value": "https://x/y"}},
                bk.BLOCK_ARTIFACT_TEXT: {bk.INPUT_ARTIFACT_TEXT: {"value": ""}},
            }
        },
        "private_metadata": str(tid),
    }
    handle_complete_task_submit(
        body={"user": {"id": "U1"}},
        view=view,
        sender=sender,
        ack=ack_fn,
    )
    # Channel card was chat.update'd.
    assert any(u["channel"] == "C1" and u["ts"] == "100.0" for u in sender.updates)


# --------------------------------------------------------------------------- #
# task_card renders the artifact on done
# --------------------------------------------------------------------------- #


def test_task_card_shows_url_artifact_on_done(session):
    t = Task(
        title="t",
        status=TaskStatus.done,
        owner_user_id="U1",
        completion_artifact="https://docs.example.com/r",
        completion_artifact_kind="url",
    )
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U1")
    texts = [
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    ]
    joined = "\n".join(texts)
    assert "Артефакт" in joined
    assert "https://docs.example.com/r" in joined


def test_task_card_shows_text_artifact_on_done(session):
    t = Task(
        title="t",
        status=TaskStatus.done,
        owner_user_id="U1",
        completion_artifact="писал руками",
        completion_artifact_kind="text",
    )
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U1")
    texts = [
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    ]
    joined = "\n".join(texts)
    assert "писал руками" in joined


def test_task_card_no_artifact_section_when_not_done(session):
    t = Task(
        title="t",
        status=TaskStatus.in_progress,
        owner_user_id="U1",
        completion_artifact="https://x/y",
        completion_artifact_kind="url",
    )
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U1")
    texts = [
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    ]
    joined = "\n".join(texts)
    assert "Артефакт" not in joined
