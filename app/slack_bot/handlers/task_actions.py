"""CR-01 task-card action handlers: start_work, submit_review, mark_done,
subscribe/unsubscribe, show_context."""
from __future__ import annotations

from typing import Any, Protocol

from slack_bolt import Ack
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ContextSnapshot, Task, TaskStatus
from app.services import (
    InvalidTransition,
    NotificationService,
    SubscriptionService,
    TransitionService,
)
from app.slack_bot import blocks as bk


log = get_logger(__name__)


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...


def _draft_id_from(body: dict[str, Any]) -> int | None:
    try:
        return int((body.get("actions") or [{}])[0].get("value") or 0) or None
    except (TypeError, ValueError):
        return None


def _actor(body: dict[str, Any]) -> str | None:
    return (body.get("user") or {}).get("id")


def _channel(body: dict[str, Any]) -> str | None:
    return (body.get("channel") or {}).get("id")


def _apply_transition(
    body: dict[str, Any],
    *,
    new_status: TaskStatus,
    sender: _Sender,
    ack: Ack,
    require_owner: bool = False,
) -> None:
    ack()
    task_id = _draft_id_from(body)
    if task_id is None:
        return
    actor = _actor(body)
    transitions = TransitionService()
    notifier = NotificationService(sender=sender)

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            log.warning("transition_unknown_task", task_id=task_id)
            return
        if require_owner and task.owner_user_id and actor and actor != task.owner_user_id:
            channel = _channel(body)
            if channel:
                sender.post_message(
                    channel=channel,
                    text=f"Only <@{task.owner_user_id}> can start this task.",
                )
            return
        old = task.status
        try:
            transitions.apply(
                session,
                task=task,
                new_status=new_status,
                actor_slack_user_id=actor,
            )
        except InvalidTransition as e:
            channel = _channel(body)
            if channel:
                sender.post_message(channel=channel, text=f":warning: {e}")
            return
        notifier.broadcast_status_change(
            session,
            task=task,
            from_status=old,
            to_status=new_status,
            actor_slack_user_id=actor,
        )


def handle_start_work(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(
        body, new_status=TaskStatus.in_progress, sender=sender, ack=ack, require_owner=True
    )


def handle_submit_review(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(body, new_status=TaskStatus.review, sender=sender, ack=ack)


def handle_mark_done(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(body, new_status=TaskStatus.done, sender=sender, ack=ack)


def handle_subscribe(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    ack()
    task_id = _draft_id_from(body)
    actor = _actor(body)
    if task_id is None or not actor:
        return
    subs = SubscriptionService()
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        subs.subscribe(session, task=task, slack_user_id=actor)
    channel = _channel(body)
    if channel:
        sender.post_message(
            channel=channel, text=f":bell: <@{actor}> subscribed to task #{task_id}"
        )


def handle_unsubscribe(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    ack()
    task_id = _draft_id_from(body)
    actor = _actor(body)
    if task_id is None or not actor:
        return
    subs = SubscriptionService()
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        subs.unsubscribe(session, task=task, slack_user_id=actor)
    channel = _channel(body)
    if channel:
        sender.post_message(
            channel=channel,
            text=f":no_bell: <@{actor}> unsubscribed from task #{task_id}",
        )


def handle_open_source(*, body: dict[str, Any], ack: Ack) -> None:
    """URL button — Slack handles the link client-side. We just ack."""
    ack()


def handle_show_context(
    *, body: dict[str, Any], client: WebClient, ack: Ack
) -> None:
    ack()
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    try:
        snapshot_id = int((body.get("actions") or [{}])[0].get("value") or 0)
    except (TypeError, ValueError):
        return
    if not snapshot_id:
        return
    with session_scope() as session:
        snap = session.get(ContextSnapshot, snapshot_id)
        if snap is None:
            return
        view = bk.context_view_modal(snapshot=snap)
    client.views_open(trigger_id=trigger_id, view=view)
