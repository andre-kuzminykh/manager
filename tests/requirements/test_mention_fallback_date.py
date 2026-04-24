"""Requirement coverage: FR-CR-04-3 (date resolver runs on the
@mention synth fallback too), FR-CR-02-1 (mention-always-replies).

When the LLM classifies a @mention as no_action (regular behaviour on
gpt-4o-mini), handle_app_mention falls back to a synthesised create_task
draft. That fallback must still pick up explicit date phrases — otherwise
the user types "ко вторнику" and the task lands in Backlog with due=null.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app.models import Task
from app.schemas.intent import IntentClassification, IntentType
from tests.requirements.conftest import StubClassifier, _make_services


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


def test_mention_fallback_runs_date_resolver_on_source_text(
    patched_session_scope,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_app_mention

    stub = StubClassifier(
        IntentClassification(intent=IntentType.no_action, confidence=0.1)
    )
    services = _make_services(slack_client, stub)

    # Make the resolver deterministic regardless of the host's real date.
    observed_args: list[tuple[str, date]] = []

    def _fake_resolver(text, today):
        observed_args.append((text, today))
        return date(2026, 4, 28)

    with patch(
        "app.intent.date_resolver.resolve_due_date", side_effect=_fake_resolver
    ):
        handle_app_mention(
            event={
                "ts": "1.0",
                "user": "U-author",
                "text": "<@UBOT> надо подготовить питчдек ко вторнику",
                "channel": "C1",
                "channel_type": "channel",
            },
            body={"event_id": "mention-fallback-1"},
            client=slack_client,
            context=bolt_context,
            services=services,
            sender=_Sender(),
            ack=ack,
        )

    # 1) Resolver was called with the mention-stripped text (not the raw
    #    "<@UBOT> ..." form).
    assert any(
        args[0] == "надо подготовить питчдек ко вторнику"
        for args in observed_args
    ), observed_args

    # 2) The resolved date is live on the persisted Task.
    with SessionFactory() as s:
        task = s.query(Task).one()
        assert task.due_date == date(2026, 4, 28)
