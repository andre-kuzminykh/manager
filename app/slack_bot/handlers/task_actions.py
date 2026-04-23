"""CR-01 task-card action handlers: start_work, submit_review, mark_done,
subscribe/unsubscribe, show_context, manage_subscriptions."""
from __future__ import annotations

from typing import Any, Protocol

from slack_bolt import Ack
from slack_sdk import WebClient

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ContextSnapshot, Task, TaskStatus, TaskSubscription
from app.services import (
    InvalidTransition,
    NotificationService,
    SubscriptionService,
    TransitionService,
    refresh_task_card,
)
from app.slack_bot import blocks as bk


log = get_logger(__name__)


def _subscribed_tasks(session, user_id: str) -> list[Task]:
    return (
        session.query(Task)
        .join(TaskSubscription, TaskSubscription.task_id == Task.id)
        .filter(TaskSubscription.slack_user_id == user_id)
        .order_by(Task.id)
        .all()
    )


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
        # Refresh both the channel widget and the DM mirror so the task card
        # visually evolves through its lifecycle.
        if hasattr(sender, "update_message"):
            refresh_task_card(sender, task)


def handle_start_work(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(
        body, new_status=TaskStatus.in_progress, sender=sender, ack=ack, require_owner=True
    )


def handle_submit_review(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(body, new_status=TaskStatus.review, sender=sender, ack=ack)


def handle_mark_done(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(body, new_status=TaskStatus.done, sender=sender, ack=ack)


def _toggle_subscription(
    *,
    body: dict[str, Any],
    sender: _Sender,
    client: Any | None,
    subscribe: bool,
) -> None:
    """Subscribe or unsubscribe the clicker, then chat.update both task
    cards (channel + owner DM) so the button label flips and a star
    appears next to the title for subscribers. Posts a DM ack to the
    clicker with a hyperlink to the channel card."""
    task_id = _draft_id_from(body)
    actor = _actor(body)
    if task_id is None or not actor:
        return
    subs = SubscriptionService()

    permalink: str | None = None
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        if subscribe:
            subs.subscribe(session, task=task, slack_user_id=actor)
        else:
            subs.unsubscribe(session, task=task, slack_user_id=actor)

        # Refresh the channel card with the actor's perspective so the
        # button label and the star reflect the latest state.
        if hasattr(sender, "update_message"):
            try:
                if task.card_channel and task.card_ts:
                    sender.update_message(
                        channel=task.card_channel,
                        ts=task.card_ts,
                        blocks=bk.task_card(
                            task=task,
                            viewer_slack_user_id=actor,
                            is_subscribed=subscribe,
                        ),
                        text=f"Task #{task.id}",
                    )
                if task.dm_channel and task.dm_ts:
                    sender.update_message(
                        channel=task.dm_channel,
                        ts=task.dm_ts,
                        blocks=bk.task_card(
                            task=task,
                            viewer_slack_user_id=task.owner_user_id,
                            is_subscribed=subs.is_subscribed(
                                session, task=task, slack_user_id=task.owner_user_id or ""
                            ),
                        ),
                        text=f"Task #{task.id}",
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("subscription_card_refresh_failed", error=str(e))

        # Compute a permalink to the channel card for the DM ack.
        if client is not None and task.card_channel and task.card_ts:
            try:
                resp = client.chat_getPermalink(
                    channel=task.card_channel, message_ts=task.card_ts
                )
                permalink = resp.get("permalink")
            except Exception:  # noqa: BLE001
                permalink = None

    icon = ":bell:" if subscribe else ":no_bell:"
    verb = "subscribed to" if subscribe else "unsubscribed from"
    link = f"<{permalink}|task #{task_id}>" if permalink else f"task #{task_id}"
    try:
        sender.post_message(
            channel=actor,
            text=f"{icon} {verb} {link}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("subscription_dm_ack_failed", error=str(e))


def handle_subscribe(
    *,
    body: dict[str, Any],
    sender: _Sender,
    ack: Ack,
    client: Any | None = None,
) -> None:
    ack()
    _toggle_subscription(body=body, sender=sender, client=client, subscribe=True)


def handle_unsubscribe(
    *,
    body: dict[str, Any],
    sender: _Sender,
    ack: Ack,
    client: Any | None = None,
) -> None:
    ack()
    _toggle_subscription(body=body, sender=sender, client=client, subscribe=False)


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


def handle_manage_subscriptions(
    *, body: dict[str, Any], client: WebClient, ack: Ack
) -> None:
    """Open the subscriptions-management modal for the clicker."""
    ack()
    trigger_id = body.get("trigger_id")
    user_id = (body.get("user") or {}).get("id")
    if not trigger_id or not user_id:
        return
    with session_scope() as session:
        tasks = _subscribed_tasks(session, user_id)
        view = bk.subscriptions_modal(tasks)
    client.views_open(trigger_id=trigger_id, view=view)


def handle_unsubscribe_in_modal(
    *, body: dict[str, Any], client: WebClient, ack: Ack
) -> None:
    """Unsubscribe from one task and refresh the modal in place."""
    ack()
    try:
        task_id = int((body.get("actions") or [{}])[0].get("value") or 0)
    except (TypeError, ValueError):
        task_id = 0
    user_id = (body.get("user") or {}).get("id")
    view_id = (body.get("view") or {}).get("id")
    if not task_id or not user_id:
        return

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is not None:
            SubscriptionService().unsubscribe(
                session, task=task, slack_user_id=user_id
            )
        tasks = _subscribed_tasks(session, user_id)
        new_view = bk.subscriptions_modal(tasks)

    if view_id:
        try:
            client.views_update(view_id=view_id, view=new_view)
        except Exception as e:  # noqa: BLE001
            log.warning("views_update_failed", error=str(e))
