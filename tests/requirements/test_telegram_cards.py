"""FR-CR-04-31 — Telegram cards delivered as DMs to author /
owner / admins (no group post).

Covers:

- recipient set: dedup, order (author → owner → admins), TG-only
  filter (Slack uids dropped);
- post_initial_card sends one DM per recipient and stores the
  message_ids on `task.extra["telegram_cards"]`;
- refresh_card iterates every stored card;
- render_tombstone same;
- back-compat: legacy single-card layout (only `card_channel/
  card_ts`, no `extra`) still works.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus
from app.telegram_bot.cards import (
    _is_telegram_uid,
    _recipient_user_ids,
    _stored_cards,
    post_initial_card,
    refresh_card,
    render_tombstone,
)


@dataclass
class _RecordingSender:
    enabled: bool = True
    sent: list[dict] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)
    next_message_id: int = 1000

    def send_message(self, **kwargs):
        mid = self.next_message_id
        self.next_message_id += 1
        self.sent.append({**kwargs, "_assigned_message_id": mid})
        return {"message_id": mid}

    def update_message(self, **kwargs):
        self.updated.append(kwargs)
        return {}


def _mk_task(session, **kw) -> Task:
    base = dict(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        owner_user_id="222",
        source_kind=TaskSourceKind.telegram,
    )
    base.update(kw)
    t = Task(**base)
    session.add(t)
    session.flush()
    return t


# --------------------------------------------------------------------------- #
# Recipient set
# --------------------------------------------------------------------------- #


def test_is_telegram_uid_filter():
    assert _is_telegram_uid("222")
    assert _is_telegram_uid("-100222")
    assert not _is_telegram_uid("U09SLACK")
    assert not _is_telegram_uid(None)
    assert not _is_telegram_uid("")


def test_recipient_set_author_then_owner_then_admins(session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777,888")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        t = _mk_task(session, owner_user_id="222")
        rec = _recipient_user_ids(t, author_id="111")
        assert rec == ["111", "222", "777", "888"]
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_recipient_set_dedups_when_author_is_owner(session):
    t = _mk_task(session, owner_user_id="111")
    rec = _recipient_user_ids(t, author_id="111")
    assert rec == ["111"]


def test_recipient_set_filters_slack_uids(session):
    t = _mk_task(session, owner_user_id="U09SLACK")
    rec = _recipient_user_ids(t, author_id="111")
    assert rec == ["111"]  # owner dropped


# --------------------------------------------------------------------------- #
# post_initial_card
# --------------------------------------------------------------------------- #


def test_post_initial_card_sends_one_dm_per_recipient(session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        t = _mk_task(session, owner_user_id="222")
        sender = _RecordingSender()
        post_initial_card(
            sender=sender,
            session=session,
            task=t,
            chat_id=-1001234,  # group source — ignored
            reply_to_message_id=42,
            author_user_id="111",
        )
        chats = [m["chat_id"] for m in sender.sent]
        assert chats == [111, 222, 999]
        # No reply_to_message_id is forwarded (the source message id
        # belongs to the group, but we DM users, not the group).
        assert all("reply_to_message_id" not in m for m in sender.sent)
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_post_initial_card_persists_card_list_and_back_compat_pair(session):
    t = _mk_task(session, owner_user_id="222")
    sender = _RecordingSender(next_message_id=500)
    post_initial_card(
        sender=sender,
        session=session,
        task=t,
        chat_id=-1,
        reply_to_message_id=1,
        author_user_id="111",
    )
    # extra carries the per-recipient list
    cards = (t.extra or {}).get("telegram_cards")
    assert cards is not None
    assert len(cards) == 2  # author + owner
    assert {c["chat_id"] for c in cards} == {111, 222}
    # back-compat: card_channel / card_ts point at the first DM
    assert t.card_channel == "111"
    assert t.card_ts == "500"


def test_post_initial_card_skips_when_no_recipients(session):
    """Slack-shaped owner uid + no author + no admins → nobody to
    DM. We must not crash; just log and return."""
    t = _mk_task(session, owner_user_id="U09SLACK")
    sender = _RecordingSender()
    post_initial_card(
        sender=sender,
        session=session,
        task=t,
        chat_id=-1,
        reply_to_message_id=None,
        author_user_id=None,
    )
    assert sender.sent == []
    assert t.card_channel is None


def test_post_initial_card_noop_for_slack_task(session):
    t = _mk_task(session, source_kind=TaskSourceKind.slack, owner_user_id="111")
    sender = _RecordingSender()
    post_initial_card(
        sender=sender,
        session=session,
        task=t,
        chat_id=1,
        reply_to_message_id=None,
        author_user_id="111",
    )
    assert sender.sent == []


# --------------------------------------------------------------------------- #
# refresh_card / render_tombstone iterate every stored card
# --------------------------------------------------------------------------- #


def test_refresh_card_edits_every_stored_card(session):
    t = _mk_task(
        session,
        owner_user_id="222",
        extra={
            "telegram_cards": [
                {"chat_id": 111, "message_id": 500},
                {"chat_id": 222, "message_id": 501},
                {"chat_id": 999, "message_id": 502},
            ]
        },
    )
    sender = _RecordingSender()
    refresh_card(sender=sender, session=session, task=t)
    chats = [u["chat_id"] for u in sender.updated]
    assert chats == [111, 222, 999]


def test_refresh_card_back_compat_legacy_single_pair(session):
    """Tasks created before FR-CR-04-31 only have card_channel /
    card_ts — refresh must still work for them."""
    t = _mk_task(
        session,
        owner_user_id="222",
        card_channel="111",
        card_ts="500",
    )
    sender = _RecordingSender()
    refresh_card(sender=sender, session=session, task=t)
    assert sender.updated == [
        {
            "chat_id": 111,
            "message_id": 500,
            "text": sender.updated[0]["text"],
            "reply_markup": sender.updated[0]["reply_markup"],
        }
    ]


def test_render_tombstone_iterates_all_cards(session):
    t = _mk_task(
        session,
        owner_user_id="222",
        extra={
            "telegram_cards": [
                {"chat_id": 111, "message_id": 1},
                {"chat_id": 222, "message_id": 2},
            ]
        },
    )
    sender = _RecordingSender()
    render_tombstone(sender=sender, task=t, actor="111")
    assert len(sender.updated) == 2
    for u in sender.updated:
        assert "deleted" in u["text"].lower()
        assert u["reply_markup"] == {"inline_keyboard": []}


def test_stored_cards_returns_empty_when_no_card_anywhere(session):
    t = _mk_task(session)
    assert _stored_cards(t) == []
