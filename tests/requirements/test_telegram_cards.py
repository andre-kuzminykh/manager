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


def test_render_tombstone_resolves_actor_uid_to_team_name(session):
    """FR-CR-05-33 — when a session is passed, the tombstone line
    shows the actor's friendly name from team_members instead of
    the raw numeric uid. Operator complained about
    «deleted by 222968032» — confusing."""
    from app.models import TeamMember

    session.add(
        TeamMember(
            real_name="Андрей Кузьминых",
            telegram_user_id=222968032,
            telegram_username="andre_andreevich",
            active=True,
        )
    )
    session.flush()
    t = _mk_task(
        session,
        title="написать Крису",
        owner_user_id="222",
        extra={
            "telegram_cards": [
                {"chat_id": 111, "message_id": 1},
            ]
        },
    )
    sender = _RecordingSender()
    render_tombstone(sender=sender, task=t, actor="222968032", session=session)
    assert len(sender.updated) == 1
    body = sender.updated[0]["text"]
    # Real name resolved.
    assert "Андрей Кузьминых" in body
    # No raw numeric uid.
    assert "222968032" not in body


def test_render_tombstone_falls_back_to_uid_without_session(session):
    """Back-compat: render_tombstone without a session keeps the
    raw uid label so the legacy callers don't have to thread
    session through."""
    t = _mk_task(
        session,
        title="x",
        extra={"telegram_cards": [{"chat_id": 111, "message_id": 1}]},
    )
    sender = _RecordingSender()
    render_tombstone(sender=sender, task=t, actor="222968032")
    body = sender.updated[0]["text"]
    assert "222968032" in body


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


# --------------------------------------------------------------------------- #
# FR-CR-05-09 — inline-quote fallback for confirm widgets
# --------------------------------------------------------------------------- #


@dataclass
class _ForwardFailingSender:
    """Forward returns empty (Telegram «message to forward not found»);
    send_message records calls and returns a fresh message_id."""

    enabled: bool = True
    sent: list[dict] = field(default_factory=list)
    forwards: list[dict] = field(default_factory=list)
    next_message_id: int = 9000

    def send_message(self, **kwargs):
        mid = self.next_message_id
        self.next_message_id += 1
        self.sent.append({**kwargs, "_assigned_message_id": mid})
        return {"message_id": mid}

    def update_message(self, **kwargs):
        return {}

    def forward_message(self, **kwargs):
        # Simulate the «not found» case the Bot API returns for
        # historical-migration messages the bot never observed.
        self.forwards.append(kwargs)
        return {}


def _mk_proposed_draft(session, *, payload):
    """Build a minimal `ActionDraft(state=proposed)` with the
    foreign-key chain (ContextSnapshot + IntentInference) populated
    so an SQLite NOT NULL constraint isn't tripped on insert."""
    from app.models import (
        ActionDraft,
        ActionDraftState,
        ContextSnapshot,
        IntentInference,
    )
    from app.models.intent import IntentType as IE

    snap = ContextSnapshot(
        conversation_id="C-test",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "x", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    d = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        created_by_slack_user_id="111",
        slack_message_ts="42",
    )
    session.add(d)
    session.flush()
    return d


def test_draft_widget_text_drops_create_header_and_uses_emoji_only_priority(
    session,
):
    """FR-CR-05-13 — widget body no longer leads with «📥 Create
    this task?». The first line is the priority emoji + the task
    title in bold; the «high» / «medium» word is gone. The
    inline keyboard already says ✅ / ✏ / ✖ so the operator
    knows what to do."""
    from app.telegram_bot.cards import _build_draft_widget_text

    draft = _mk_proposed_draft(
        session,
        payload={
            "title": "написать Андрею",
            "description": "Андрей спрашивал про SoW.",
            "priority": "high",
            "owner_user_id": "111",
            "owner_display_name": "Андре",
            "due_date": "2026-05-10",
        },
    )
    text = _build_draft_widget_text(draft)
    # No «Create this task?» / «📥» header anymore.
    assert "Create this task" not in text
    assert "📥" not in text
    # First line: priority emoji + bold title.
    first_line = text.splitlines()[0]
    assert "🟠" in first_line  # high
    assert "<b>написать Андрею</b>" in first_line
    # No «high» / «medium» text in the body — emoji only.
    assert "high" not in text
    assert "medium" not in text
    # Description + meta still present.
    assert "📝" in text
    assert "Андре" in text
    assert "2026-05-10" in text


def test_draft_widget_text_wraps_title_in_source_permalink(session):
    """FR-CR-05-18 — the title is the deeplink. Tap on the bold
    title in the widget = open the original chat message."""
    from app.telegram_bot.cards import _build_draft_widget_text

    draft = _mk_proposed_draft(
        session,
        payload={
            "title": "написать Андрею",
            "_pending": {
                "permalink": "https://t.me/c/2061886148/2981",
                "source_chat_id": -1002061886148,
                "source_message_id": 2981,
            },
        },
    )
    text = _build_draft_widget_text(draft)
    assert (
        '<a href="https://t.me/c/2061886148/2981">'
        "<b>написать Андрею</b></a>" in text
    )
    # No separate 🔗 line.
    assert "🔗" not in text


def test_draft_widget_text_falls_back_to_plain_bold_without_permalink(session):
    """Private DMs and basic groups have no shareable URL — title
    renders as plain `<b>title</b>` without a broken `<a href="">`
    wrapper."""
    from app.telegram_bot.cards import _build_draft_widget_text

    draft = _mk_proposed_draft(
        session,
        payload={
            "title": "x",
            "_pending": {"permalink": None},
        },
    )
    text = _build_draft_widget_text(draft)
    assert "<a href=" not in text.split("\n")[0]
    assert "<b>x</b>" in text


def test_draft_widget_text_renders_plain_text_when_no_username_anywhere(session):
    """FR-CR-05-26 — when the registry has no `telegram_username`
    for this user, the widget owner renders as plain text. No
    `tg://user?id=` fallback any more (operator-facing rule:
    «если username нет, то ссылку не выводи»)."""
    from app.telegram_bot.cards import _build_draft_widget_text

    draft = _mk_proposed_draft(
        session,
        payload={
            "title": "x",
            "owner_user_id": "222968032",
            "owner_display_name": "Андрей Кузьминых",
        },
    )
    text = _build_draft_widget_text(draft)
    assert "Андрей Кузьминых" in text
    assert "tg://user?id=" not in text


def test_draft_widget_text_renders_owner_as_tme_link_when_username_in_registry(
    session,
):
    """FR-CR-05-26 — when the registry has a `telegram_username`,
    the visible label is the registry's `real_name` and it's
    wrapped in a `https://t.me/<handle>` link."""
    from app.models import TeamMember
    from app.telegram_bot.cards import _build_draft_widget_text
    from datetime import datetime, timezone as _tz

    session.add(
        TeamMember(
            real_name="Артем Соколов",
            telegram_user_id=97239970,
            telegram_username="artem_sokolov",
            active=True,
            last_synced_at=datetime.now(_tz.utc),
        )
    )
    session.flush()

    draft = _mk_proposed_draft(
        session,
        payload={
            "title": "x",
            "owner_user_id": "97239970",
            "owner_display_name": "Артем",  # short form — registry wins
        },
    )
    text = _build_draft_widget_text(draft, session=session)
    assert '<a href="https://t.me/artem_sokolov">' in text
    assert "Артем Соколов" in text
    # Short form ignored — registry's full name wins.
    assert ">Артем</a>" not in text


def test_post_draft_confirmation_sends_only_widget_no_forward_no_quote(
    session, monkeypatch
):
    """FR-CR-05-10 — `post_draft_confirmation` no longer fans out
    a `forwardMessage` and a separate inline-quote DM. The widget
    itself carries the LLM-generated context summary in its
    description, so each recipient gets EXACTLY ONE message: the
    widget."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings
    from app.telegram_bot.cards import post_draft_confirmation

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        draft = _mk_proposed_draft(
            session,
            payload={
                "title": "написать Андрею",
                "description": "Андрей спрашивал про SoW по сделке Acme.",
                "owner_user_id": "111",
                "_pending": {
                    "source_chat_id": -1001234,
                    "source_message_id": 42,
                },
            },
        )

        sender = _ForwardFailingSender()
        post_draft_confirmation(
            sender=sender,
            session=session,
            draft=draft,
            source_chat_id=-1001234,
            source_message_id=42,
            author_user_id="111",
            owner_user_id="111",
        )
        # FR-CR-05-10: zero forwards, zero quote DMs. Just the
        # widget itself, one per recipient (author + admin = 2).
        assert sender.forwards == []
        assert len(sender.sent) == 2
        for s in sender.sent:
            assert "<blockquote>" not in s["text"]
            # The LLM-generated description carries context.
            assert "Андрей" in s["text"] or "написать" in s["text"]
            # And the widget keyboard rides along.
            assert "reply_markup" in s
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
