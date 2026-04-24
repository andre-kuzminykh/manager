"""CR-03 Phase B: always-create + admin review (Confirm / Edit / Reject)."""
from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.models import (
    ActionDraft,
    ActionDraftState,
    AuditLog,
    Task,
    TaskStatus,
)
from app.services import post_admin_review
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.admin_review import (
    handle_admin_confirm,
    handle_admin_edit_open,
    handle_admin_edit_submit,
    handle_admin_reject,
)


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "0.0"}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


# --------------------------------------------------------------------------- #
# admin_review_card shape
# --------------------------------------------------------------------------- #


def test_admin_review_card_has_three_action_buttons(session):
    t = Task(title="t", status=TaskStatus.backlog, owner_user_id="U-owner")
    session.add(t)
    session.flush()
    blocks = bk.admin_review_card(task=t, reasoning="why", source_permalink="https://x/y")
    actions = next(b for b in blocks if b["type"] == "actions")
    ids = [el["action_id"] for el in actions["elements"]]
    assert ids == [
        bk.ACTION_ADMIN_CONFIRM_TASK,
        bk.ACTION_ADMIN_EDIT_TASK,
        bk.ACTION_ADMIN_REJECT_TASK,
    ]


def test_admin_review_card_shows_reasoning_and_source_link(session):
    t = Task(title="t", status=TaskStatus.backlog)
    session.add(t)
    session.flush()
    blocks = bk.admin_review_card(task=t, reasoning="trigger matched", source_permalink="https://x/y")
    ctx_texts = [el["text"] for b in blocks if b["type"] == "context" for el in b["elements"]]
    joined = "\n".join(ctx_texts)
    assert "trigger matched" in joined
    assert "https://x/y" in joined


# --------------------------------------------------------------------------- #
# post_admin_review service
# --------------------------------------------------------------------------- #


def test_post_admin_review_dms_each_admin_with_card(session):
    t = Task(title="t", status=TaskStatus.backlog, owner_user_id="U-owner")
    session.add(t)
    session.flush()
    sender = _Sender()
    count = post_admin_review(
        session,
        task=t,
        sender=sender,
        admins={"U-adm1", "U-adm2"},
    )
    assert count == 2
    channels = {m["channel"] for m in sender.posts}
    assert channels == {"U-adm1", "U-adm2"}


def test_post_admin_review_skips_owner_if_also_admin(session):
    """If the task owner is also an admin, skip the admin DM — they
    already got the full task card via finalize."""
    t = Task(title="t", status=TaskStatus.backlog, owner_user_id="U-owner")
    session.add(t)
    session.flush()
    sender = _Sender()
    post_admin_review(session, task=t, sender=sender, admins={"U-owner", "U-adm2"})
    channels = {m["channel"] for m in sender.posts}
    assert "U-owner" not in channels
    assert "U-adm2" in channels


def test_post_admin_review_writes_audit_row(session):
    t = Task(title="t", status=TaskStatus.backlog)
    session.add(t)
    session.flush()
    sender = _Sender()
    post_admin_review(session, task=t, sender=sender, admins={"U-adm"})
    log = (
        session.query(AuditLog)
        .filter(AuditLog.category == "admin_review")
        .filter(AuditLog.action == "awaiting_confirmation")
        .one()
    )
    assert log.entity_id == str(t.id)
    assert log.payload["admins"] == ["U-adm"]


def test_post_admin_review_no_admins_still_audits(session):
    t = Task(title="t", status=TaskStatus.backlog)
    session.add(t)
    session.flush()
    sender = _Sender()
    count = post_admin_review(session, task=t, sender=sender, admins=set())
    assert count == 0
    log = (
        session.query(AuditLog)
        .filter(AuditLog.category == "admin_review")
        .one()
    )
    assert "no admins" in (log.payload or {}).get("reason", "")


def test_post_admin_review_posts_ephemeral_in_source_thread(session):
    t = Task(title="t", status=TaskStatus.backlog)
    session.add(t)
    session.flush()
    sender = _Sender()
    post_admin_review(
        session,
        task=t,
        sender=sender,
        admins={"U-adm"},
        source_channel="C1",
        source_thread_ts="100.0",
    )
    assert sender.ephemerals
    e = sender.ephemerals[0]
    assert e["channel"] == "C1"
    assert e["user"] == "U-adm"
    assert e["thread_ts"] == "100.0"


# --------------------------------------------------------------------------- #
# handle_admin_confirm / reject (gating)
# --------------------------------------------------------------------------- #


def test_admin_confirm_records_audit_and_replaces_card(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-adm")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.backlog)
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    handle_admin_confirm(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-adm"},
            "channel": {"id": "U-adm"},
            "message": {"ts": "99.0"},
        },
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        log = (
            s.query(AuditLog)
            .filter(AuditLog.action == "admin_confirmed")
            .one()
        )
        assert log.actor == "U-adm"
    assert sender.updates  # card replaced

    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_admin_reject_deletes_task_and_audits(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-adm")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.backlog)
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    handle_admin_reject(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-adm"},
            "channel": {"id": "U-adm"},
            "message": {"ts": "99.0"},
        },
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.get(Task, tid) is None
        log = (
            s.query(AuditLog)
            .filter(AuditLog.action == "admin_rejected")
            .one()
        )
        assert log.actor == "U-adm"

    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_non_admin_click_is_blocked_with_ephemeral_hint(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-adm")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    with SessionFactory() as s:
        t = Task(title="t", status=TaskStatus.backlog)
        s.add(t)
        s.commit()
        tid = t.id

    sender = _Sender()
    handle_admin_reject(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-bystander"},
            "channel": {"id": "C1"},
            "message": {"ts": "99.0"},
        },
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        assert s.get(Task, tid) is not None  # NOT deleted
    assert sender.ephemerals
    assert ":lock:" in sender.ephemerals[0]["text"]

    get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# admin edit open + submit
# --------------------------------------------------------------------------- #


class _ViewCli:
    def __init__(self):
        self.opened = []

    def views_open(self, trigger_id, view):
        self.opened.append((trigger_id, view))
        return {"ok": True}


def test_admin_edit_open_opens_modal_with_prefilled_values(
    patched_session_scope, SessionFactory, ack, monkeypatch
):
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-adm")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    with SessionFactory() as s:
        t = Task(
            title="old title",
            status=TaskStatus.backlog,
            owner_user_id="U1",
            owner_display_name="Ivan",
        )
        s.add(t)
        s.commit()
        tid = t.id

    cli = _ViewCli()
    handle_admin_edit_open(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-adm"},
            "channel": {"id": "U-adm"},
            "message": {"ts": "99.0"},
            "trigger_id": "trig",
        },
        client=cli,
        sender=_Sender(),
        ack=ack,
    )
    assert cli.opened
    view = cli.opened[0][1]
    assert view["callback_id"] == bk.MODAL_CALLBACK_ADMIN_EDIT
    title_block = next(b for b in view["blocks"] if b["block_id"] == bk.BLOCK_TITLE)
    assert title_block["element"]["initial_value"] == "old title"

    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_admin_edit_submit_updates_task_and_audits(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(
            title="old",
            status=TaskStatus.backlog,
            owner_user_id="U1",
            card_channel="C1",
            card_ts="100.0",
        )
        s.add(t)
        s.commit()
        tid = t.id

    import json as _json

    view = {
        "state": {
            "values": {
                bk.BLOCK_TITLE: {bk.INPUT_TITLE: {"value": "new title"}},
                bk.BLOCK_PRIORITY: {bk.INPUT_PRIORITY: {"selected_option": {"value": "high"}}},
            }
        },
        "private_metadata": _json.dumps(
            {"edit_task_id": tid, "admin_review_msg": {"channel": "U-adm", "ts": "9.9"}}
        ),
    }

    sender = _Sender()
    handle_admin_edit_submit(
        body={"user": {"id": "U-adm"}}, view=view, sender=sender, ack=ack
    )
    with SessionFactory() as s:
        t = s.get(Task, tid)
        assert t.title == "new title"
        assert t.priority.value == "high"
        log = s.query(AuditLog).filter(AuditLog.action == "task_edited").one()
        assert "title" in log.payload["diff"]
        assert log.payload["diff"]["title"] == ["old", "new title"]
    # The admin-review DM/ephemeral was replaced with "edited" note.
    assert any(u["channel"] == "U-adm" and u["ts"] == "9.9" for u in sender.updates)


# --------------------------------------------------------------------------- #
# always-create hooked up in handle_message
# --------------------------------------------------------------------------- #


def test_handle_message_passive_offers_soft_prompt_does_not_auto_create(
    patched_session_scope, services_task, bolt_context, slack_client, SessionFactory, monkeypatch, ack
):
    """Per product decision 2026-04-24, passive never auto-creates. It
    offers (soft_prompt in the source thread). No admin review is
    triggered — that was also dropped with auto-create."""
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-adm")
    from app.config import get_settings
    from app.slack_bot.handlers.events import handle_message

    get_settings.cache_clear()  # type: ignore[attr-defined]

    sender = _Sender()
    handle_message(
        event={
            "ts": "500.0",
            "user": "U-author",
            "text": "надо задачу на завтра",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "auto-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        # No task auto-created on passive path.
        assert s.query(Task).count() == 0
        # Draft persists in proposed state for the soft-prompt button.
        assert s.query(ActionDraft).one().state == ActionDraftState.proposed
        # No admin review.
        assert s.query(AuditLog).filter(AuditLog.category == "admin_review").count() == 0
    # Admin got nothing.
    assert not any(m["channel"] == "U-adm" for m in sender.posts)

    get_settings.cache_clear()  # type: ignore[attr-defined]
