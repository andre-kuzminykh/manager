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
from app.sync.task_sync import sync_task as _sync_task


log = get_logger(__name__)


def _subscribed_tasks(session, user_id: str) -> list[Task]:
    return (
        session.query(Task)
        .join(TaskSubscription, TaskSubscription.task_id == Task.id)
        .filter(
            TaskSubscription.slack_user_id == user_id,
            Task.deleted_at.is_(None),
        )
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


def _may_edit_task(actor: str | None, task: Task) -> bool:
    """Owner or admin can edit / cancel / delete the task."""
    from app.services.employees import is_admin as _is_admin

    if actor is None:
        return False
    if task.owner_user_id == actor:
        return True
    return _is_admin(actor)


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
    # Push the transition to Google Sheets / Tasks (best-effort).
    _sync_task(task_id)


def handle_start_work(*, body: dict[str, Any], sender: _Sender, ack: Ack) -> None:
    _apply_transition(
        body, new_status=TaskStatus.in_progress, sender=sender, ack=ack, require_owner=True
    )


def handle_mark_done(
    *,
    body: dict[str, Any],
    sender: _Sender,
    ack: Ack,
    client: Any | None = None,
) -> None:
    """CR-03 FR-CR-03-7: Mark done no longer transitions directly — it opens
    a modal asking for a completion artifact (URL or text). The transition
    and refresh happen inside the submit handler."""
    ack()
    task_id = _draft_id_from(body)
    trigger_id = body.get("trigger_id")
    if task_id is None or not trigger_id or client is None:
        # Graceful fallback: if we don't have a WebClient, transition
        # directly so the button doesn't appear dead.
        _apply_transition(
            body, new_status=TaskStatus.done, sender=sender, ack=(lambda *a, **k: None)
        )
        return
    view = bk.complete_task_modal(task_id=task_id)
    try:
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:  # noqa: BLE001
        log.warning("complete_modal_open_failed", error=str(e))


def handle_complete_task_submit(
    *,
    body: dict[str, Any],
    view: dict[str, Any],
    sender: _Sender,
    ack: Ack,
) -> None:
    """Modal submit: optionally save an artifact, transition to done,
    refresh in-channel and DM cards. Both fields are optional
    (FR-CR-04-21) — submitting an empty modal is a valid completion."""
    values = view.get("state", {}).get("values", {})
    url = _state_value(values, bk.BLOCK_ARTIFACT, bk.INPUT_ARTIFACT_URL)
    text = _state_value(values, bk.BLOCK_ARTIFACT_TEXT, bk.INPUT_ARTIFACT_TEXT)
    url = (url or "").strip()
    text = (text or "").strip()
    ack()

    try:
        task_id = int(view.get("private_metadata") or 0)
    except ValueError:
        return
    actor = (body.get("user") or {}).get("id")

    transitions = TransitionService()
    notifier = NotificationService(sender=sender)

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            return
        # Both fields optional — only persist artifact when present.
        if url:
            task.completion_artifact = url
            task.completion_artifact_kind = "url"
        elif text:
            task.completion_artifact = text
            task.completion_artifact_kind = "text"
        old = task.status
        try:
            transitions.apply(
                session, task=task, new_status=TaskStatus.done, actor_slack_user_id=actor
            )
        except InvalidTransition as e:
            # Already done — still keep the artifact if one was provided.
            log.info("complete_on_done_task", task_id=task_id, err=str(e))
        notifier.broadcast_status_change(
            session,
            task=task,
            from_status=old,
            to_status=TaskStatus.done,
            actor_slack_user_id=actor,
        )
        if hasattr(sender, "update_message"):
            refresh_task_card(sender, task)
    _sync_task(task_id)


def _state_value(values: dict[str, Any], block_id: str, action_id: str) -> Any:
    block = values.get(block_id, {})
    element = block.get(action_id, {})
    return element.get("value")


# --------------------------------------------------------------------------- #
# Cancel — drop the task back to todo (this week) or backlog (later).
# --------------------------------------------------------------------------- #


def _route_on_cancel(task: Task, *, today=None) -> TaskStatus:
    """Pick the destination status when the user hits *Cancel*.

    Tasks with a deadline within the current calendar week (Mon-Sun)
    land in `todo` so they stay scheduled; everything else drops to
    `backlog` so it doesn't clutter the active queue.
    """
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    today = today or _date.today()
    week_end = today + _timedelta(days=(6 - today.weekday()))
    if task.due_date and task.due_date <= week_end:
        return TaskStatus.todo
    return TaskStatus.backlog


def handle_cancel_task(
    *, body: dict[str, Any], sender: _Sender, ack: Ack
) -> None:
    """FR-CR-04-21: cancel = "I'm not finishing this now". Routes the
    task back to todo (within the current week) or backlog (later)."""
    ack()
    task_id = _draft_id_from(body)
    if task_id is None:
        return
    actor = _actor(body)
    transitions = TransitionService()
    notifier = NotificationService(sender=sender)

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            return
        if not _may_edit_task(actor, task):
            channel = _channel(body)
            if channel and actor and hasattr(sender, "post_ephemeral"):
                try:
                    sender.post_ephemeral(
                        channel=channel,
                        user=actor,
                        text=":lock: Only the owner or an admin can cancel this.",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return
        target = _route_on_cancel(task)
        if task.status == target:
            return  # already there — no-op
        old = task.status
        try:
            transitions.apply(
                session,
                task=task,
                new_status=target,
                actor_slack_user_id=actor,
                reason="cancelled",
            )
        except InvalidTransition as e:
            log.warning("cancel_invalid", task_id=task_id, err=str(e))
            return
        notifier.broadcast_status_change(
            session,
            task=task,
            from_status=old,
            to_status=target,
            actor_slack_user_id=actor,
        )
        if hasattr(sender, "update_message"):
            refresh_task_card(sender, task)
    _sync_task(task_id)


# --------------------------------------------------------------------------- #
# Delete — owner+admin only, confirmation modal, soft delete.
# --------------------------------------------------------------------------- #


def handle_delete_task_open(
    *, body: dict[str, Any], client: WebClient, sender: _Sender, ack: Ack
) -> None:
    ack()
    task_id = _draft_id_from(body)
    trigger_id = body.get("trigger_id")
    actor = _actor(body)
    if task_id is None or not trigger_id:
        return
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            return
        if not _may_edit_task(actor, task):
            channel = _channel(body)
            if channel and actor and hasattr(sender, "post_ephemeral"):
                try:
                    sender.post_ephemeral(
                        channel=channel,
                        user=actor,
                        text=":lock: Only the owner or an admin can delete this.",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return
        view = bk.delete_task_modal(task_id=task_id, title=task.title)
    try:
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:  # noqa: BLE001
        log.warning("delete_modal_open_failed", error=str(e))


def handle_delete_task_submit(
    *, body: dict[str, Any], view: dict[str, Any], sender: _Sender, ack: Ack
) -> None:
    """Confirm-modal submit: stamp `deleted_at`, write an audit row, then
    update the channel widget and DM mirror to a tombstone block so it's
    obvious the task is gone."""
    from datetime import datetime, timezone

    from app.models import AuditLog

    ack()
    try:
        task_id = int(view.get("private_metadata") or 0)
    except ValueError:
        return
    actor = (body.get("user") or {}).get("id")

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            return
        if not _may_edit_task(actor, task):
            return
        task.deleted_at = datetime.now(timezone.utc)
        session.add(
            AuditLog(
                category="task",
                action="task_deleted",
                entity_type="task",
                entity_id=str(task.id),
                actor=actor,
                payload={
                    "title": task.title,
                    "owner_user_id": task.owner_user_id,
                    "status_at_delete": task.status.value,
                },
            )
        )
        session.flush()
        # Replace both cards with a tombstone so the user gets visual
        # confirmation that the task is gone.
        tomb_blocks = [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":wastebasket: Task #{task.id} — *{task.title}* "
                            f"deleted by <@{actor}>"
                        ),
                    }
                ],
            }
        ]
        tomb_text = f":wastebasket: Task #{task.id} deleted"
        if hasattr(sender, "update_message"):
            for ch, ts in (
                (task.card_channel, task.card_ts),
                (task.dm_channel, task.dm_ts),
            ):
                if ch and ts:
                    try:
                        sender.update_message(
                            channel=ch, ts=ts, blocks=tomb_blocks, text=tomb_text
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "delete_card_refresh_failed",
                            channel=ch,
                            error=str(e),
                        )
    # Push the soft-delete to Sheets so the row's status flips to "deleted".
    _sync_task(task_id)


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
    anchor_needed: bool = False
    card_blocks: list[dict[str, Any]] | None = None
    task_title: str = ""
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        task_title = task.title
        if subscribe:
            sub = subs.subscribe(session, task=task, slack_user_id=actor)
            # Remember whether we still need to post an anchor DM to this
            # user so later broadcasts can thread under it.
            anchor_needed = sub.dm_ts is None
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
                        text=f":clipboard: Task #{task.id}: {task.title}",
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
                        text=f":clipboard: Task #{task.id}: {task.title}",
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

        # Post an anchor task card to the new subscriber's DM so every
        # future broadcast about this task threads under a single
        # message. Only do this once per (task, user).
        if subscribe and anchor_needed:
            try:
                anchor_resp = sender.post_message(
                    channel=actor,
                    blocks=bk.task_card(
                        task=task,
                        viewer_slack_user_id=actor,
                        is_subscribed=True,
                    ),
                    text=f":clipboard: Task #{task.id}: {task.title}",
                )
                if isinstance(anchor_resp, dict):
                    sub.dm_ts = anchor_resp.get("ts")
                    session.flush()
            except Exception as e:  # noqa: BLE001
                log.warning("subscription_anchor_failed", error=str(e))

        # Recompute the anchor ts for the ack text below.
        anchor_ts = sub.dm_ts if subscribe else None

        # Render the task card from the actor's perspective so the ack
        # DM embeds a preview of the task (title, meta, buttons).
        card_blocks = bk.task_card(
            task=task,
            viewer_slack_user_id=actor,
            is_subscribed=subscribe,
        )

    icon = ":bell:" if subscribe else ":no_bell:"
    verb = "subscribed to" if subscribe else "unsubscribed from"
    link = f"<{permalink}|task #{task_id}>" if permalink else f"task #{task_id}"
    ack_line = f"{icon} {verb} {link}"
    # Compose: a short context block with the ack line, then the
    # task-card preview so the DM shows status / owner / buttons at
    # a glance.
    ack_blocks: list[dict[str, Any]] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ack_line}]}
    ]
    if card_blocks:
        ack_blocks.extend(card_blocks)
    ack_kwargs: dict[str, Any] = {
        "channel": actor,
        "text": f"{ack_line}: {task_title}",
        "blocks": ack_blocks,
        "unfurl_links": True,
    }
    # Thread the ack under the anchor DM (subscribe path) so the user's
    # notifications for this task stay grouped.
    if anchor_ts:
        ack_kwargs["thread_ts"] = anchor_ts
    try:
        sender.post_message(**ack_kwargs)
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


# --------------------------------------------------------------------------- #
# Task-card "Edit" — owner / admin can edit title, owner, priority, due_date,
# description, and estimated_minutes from the task card itself. This is the
# non-admin-review counterpart to handle_admin_edit_*.
# --------------------------------------------------------------------------- #


def handle_task_edit_open(
    *, body: dict[str, Any], client: WebClient, sender: _Sender, ack: Ack
) -> None:
    ack()
    task_id = _draft_id_from(body)
    trigger_id = body.get("trigger_id")
    actor = _actor(body)
    if task_id is None or not trigger_id:
        return
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        if not _may_edit_task(actor, task):
            channel = _channel(body)
            if channel and actor:
                try:
                    sender.post_ephemeral(
                        channel=channel,
                        user=actor,
                        text=":lock: Only the owner or an admin can edit this.",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return
        initial = {
            "title": task.title,
            "description": task.description,
            "owner_user_id": task.owner_user_id,
            "owner_display_name": task.owner_display_name,
            "priority": task.priority.value,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "due_time": task.due_time.strftime("%H:%M") if task.due_time else None,
            "start_date": task.start_date.isoformat() if task.start_date else None,
            "start_time": task.start_time.strftime("%H:%M") if task.start_time else None,
            "category": task.category,
            "is_recurring": task.is_recurring,
            "recurring_weekdays": task.recurring_weekdays or [],
            "recurring_start_time": (
                task.recurring_start_time.strftime("%H:%M")
                if task.recurring_start_time
                else None
            ),
            "recurring_end_time": (
                task.recurring_end_time.strftime("%H:%M")
                if task.recurring_end_time
                else None
            ),
            "estimated_minutes": task.estimated_minutes,
        }

    import json as _json

    from app.config import get_settings

    view = bk.task_modal(
        private_metadata=_json.dumps({"edit_task_id": task_id}),
        initial=initial,
        allowed_owners=get_settings().allowed_owners(),
    )
    view["callback_id"] = bk.MODAL_CALLBACK_EDIT_TASK
    try:
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:  # noqa: BLE001
        log.warning("task_edit_views_open_failed", error=str(e))


def handle_task_edit_submit(
    *, body: dict[str, Any], view: dict[str, Any], sender: _Sender, ack: Ack
) -> None:
    from datetime import date as _date
    from datetime import time as _time

    from app.slack_bot.handlers.views import _extract_task_payload

    payload = _extract_task_payload(view)
    if not payload["title"]:
        ack(
            response_action="errors",
            errors={bk.BLOCK_TITLE: "Title is required"},
        )
        return
    ack()

    import json as _json

    pm = _json.loads(view.get("private_metadata") or "{}")
    task_id = pm.get("edit_task_id")
    actor = _actor(body)
    if not task_id:
        return

    with session_scope() as session:
        task = session.get(Task, int(task_id))
        if task is None:
            return
        if not _may_edit_task(actor, task):
            return
        task.title = payload["title"]
        task.description = payload.get("description")
        if payload.get("owner_user_id"):
            task.owner_user_id = payload["owner_user_id"]
        if payload.get("owner_display_name"):
            task.owner_display_name = payload["owner_display_name"]
        new_prio = payload.get("priority")
        if new_prio:
            from app.models.task import TaskPriority

            task.priority = TaskPriority(new_prio)
        new_due = payload.get("due_date")
        new_due_d: _date | None = None
        if isinstance(new_due, str) and new_due:
            try:
                new_due_d = _date.fromisoformat(new_due)
            except ValueError:
                new_due_d = None
        task.due_date = new_due_d

        def _parse_time(raw):
            if isinstance(raw, str) and raw:
                try:
                    hh, mm = raw.split(":")[:2]
                    return _time(int(hh), int(mm))
                except (ValueError, IndexError):
                    return None
            return None

        def _parse_date(raw):
            if isinstance(raw, str) and raw:
                try:
                    return _date.fromisoformat(raw)
                except ValueError:
                    return None
            return None

        task.due_time = _parse_time(payload.get("due_time"))
        task.start_date = _parse_date(payload.get("start_date"))
        task.start_time = _parse_time(payload.get("start_time"))
        task.category = payload.get("category")

        # Recurring: only honour the schedule if BOTH the checkbox is
        # ticked AND at least one weekday is picked. Otherwise ignore
        # leftover values that may sit in the modal state.
        weekdays = payload.get("recurring_weekdays") or []
        if payload.get("is_recurring") and weekdays:
            task.is_recurring = True
            task.recurring_weekdays = weekdays
            task.recurring_start_time = _parse_time(payload.get("recurring_start_time"))
            task.recurring_end_time = _parse_time(payload.get("recurring_end_time"))
        else:
            task.is_recurring = False
            task.recurring_weekdays = None
            task.recurring_start_time = None
            task.recurring_end_time = None

        if payload.get("estimated_minutes") is not None:
            task.estimated_minutes = payload.get("estimated_minutes")
        # Once the human has edited the task, the "owner_assumed" flag should
        # come off — the label in the card stops saying "(предположительно)".
        if task.extra and task.extra.get("owner_assumed"):
            extra = dict(task.extra)
            extra.pop("owner_assumed", None)
            task.extra = extra or None
        session.flush()
        if hasattr(sender, "update_message"):
            refresh_task_card(sender, task)
    # Push the edited fields to Google Sheets / Tasks.
    _sync_task(int(task_id))
