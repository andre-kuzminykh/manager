"""FR-CR-05-02 — subscriber updates throughout the day.

When a Task transitions (start / done / cancel / re-open) or gets
edited, every non-owner subscriber gets a one-line DM. The dispatch
fires from inside ``TransitionService.apply`` and ``apply_edit_reply``
so any future trigger (slash command, scheduled rule, future API)
inherits it for free.

Per-recipient idempotency lives in ``audit_logs`` under
``category='subscriber_update'`` keyed by
``(task_id, recipient_user_id, transition_id_or_edit_marker)``. A
replay of the same transition (or a worker restart after a crash)
won't double-DM.

The dispatcher is process-aware: a single VM hosts both the Slack
bolt app and the Telegram listener as separate containers, but each
process can DM in either channel by holding **both** senders. Slack
DMs use ``WebClient.chat_postMessage(channel=user_id, text=…)``
(``channel`` accepts a user id). Telegram DMs use
``TelegramSender.send_message(chat_id=int(user_id), …)``.

Routing rule (same as the digests): numeric recipient → Telegram,
``U…`` / ``W…`` → Slack. Anything else is dropped with a log line.
"""
from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AuditLog, Task, TaskStatusHistory
from app.services.subscriptions import SubscriptionService

log = get_logger(__name__)


class SlackPoster(Protocol):
    """Anything that can post a Slack DM. We accept a thin
    duck-typed surface so tests can pass a fake. The real
    implementation is :class:`slack_sdk.WebClient`.
    """

    def chat_postMessage(self, *, channel: str, text: str) -> Any: ...


class TelegramPoster(Protocol):
    """Anything that can DM a Telegram user. The real impl is
    :class:`app.telegram_bot.sender.TelegramSender` — its
    ``send_message`` accepts the user id as ``chat_id``.
    """

    def send_message(self, *, chat_id: int | str, text: str, **kw: Any) -> Any: ...


def _is_telegram_uid(uid: str | None) -> bool:
    return bool(uid) and uid.lstrip("-").isdigit()


def _is_slack_uid(uid: str | None) -> bool:
    return bool(uid) and uid[:1] in ("U", "W")


class SubscriberDispatcher:
    """Routes subscriber-update DMs to the right channel.

    Either sender is optional — a process that only knows how to
    talk to Slack can pass ``telegram_sender=None`` and the
    dispatcher will skip TG recipients (logging a hint). Same the
    other way.
    """

    def __init__(
        self,
        *,
        slack_poster: SlackPoster | None = None,
        telegram_sender: TelegramPoster | None = None,
        subscriptions: SubscriptionService | None = None,
    ) -> None:
        self._slack = slack_poster
        self._tg = telegram_sender
        self._subs = subscriptions or SubscriptionService()

    # --------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------- #

    def dispatch_status_change(
        self,
        session: Session,
        *,
        task: Task,
        history: TaskStatusHistory,
    ) -> int:
        """DM every non-owner subscriber that ``task`` flipped to a
        new status. Returns the number of DMs that were actually
        sent (excluding skipped-as-already-dispatched and unknown-
        channel recipients)."""
        text = self._format_status_text(task, history)
        marker = f"transition_{history.id}"
        return self._fanout(session, task=task, text=text, marker=marker)

    def dispatch_edit(
        self,
        session: Session,
        *,
        task: Task,
        applied_payload: dict[str, str],
        actor_user_id: str | None,
    ) -> int:
        """DM every non-owner subscriber that ``task`` was edited.

        ``applied_payload`` is the dict the LLM Edit parser returned
        (or the structured ``key=value`` parser); each key is one
        field that changed.
        """
        text = self._format_edit_text(task, applied_payload, actor_user_id)
        # The marker uses the AuditLog primary-key autoincrement of
        # the task_edit row, but we don't have one here. Fall back to
        # `(updated_at iso)` — close enough for de-dup and stable
        # within a request.
        ts = task.updated_at.isoformat() if task.updated_at else ""
        marker = f"edit_{ts}"
        return self._fanout(session, task=task, text=text, marker=marker)

    # --------------------------------------------------------------- #
    # Internals
    # --------------------------------------------------------------- #

    def _fanout(
        self,
        session: Session,
        *,
        task: Task,
        text: str,
        marker: str,
    ) -> int:
        recipients = [
            uid
            for uid in self._subs.list_subscribers(session, task=task)
            if uid and uid != task.owner_user_id
        ]
        if not recipients:
            return 0
        sent = 0
        for uid in recipients:
            if self._already_dispatched(session, task=task, recipient=uid, marker=marker):
                continue
            try:
                ok = self._dm_one(uid, text)
            except Exception as e:  # noqa: BLE001 — never break the caller
                log.warning(
                    "subscriber_update_dm_failed",
                    task_id=task.id,
                    recipient=uid,
                    marker=marker,
                    error=str(e),
                )
                continue
            self._record(session, task=task, recipient=uid, marker=marker, sent=ok)
            if ok:
                sent += 1
        return sent

    def _dm_one(self, recipient: str, text: str) -> bool:
        if _is_telegram_uid(recipient):
            if self._tg is None:
                log.info("subscriber_update_no_tg_sender", recipient=recipient)
                return False
            self._tg.send_message(chat_id=int(recipient), text=text)
            return True
        if _is_slack_uid(recipient):
            if self._slack is None:
                log.info("subscriber_update_no_slack_sender", recipient=recipient)
                return False
            self._slack.chat_postMessage(channel=recipient, text=text)
            return True
        log.info("subscriber_update_unknown_uid_shape", recipient=recipient)
        return False

    def _already_dispatched(
        self,
        session: Session,
        *,
        task: Task,
        recipient: str,
        marker: str,
    ) -> bool:
        return (
            session.query(AuditLog)
            .filter(
                AuditLog.category == "subscriber_update",
                AuditLog.entity_type == "task",
                AuditLog.entity_id == str(task.id),
                AuditLog.actor == recipient,
                AuditLog.action == marker,
            )
            .first()
            is not None
        )

    def _record(
        self,
        session: Session,
        *,
        task: Task,
        recipient: str,
        marker: str,
        sent: bool,
    ) -> None:
        session.add(
            AuditLog(
                category="subscriber_update",
                action=marker,
                entity_type="task",
                entity_id=str(task.id),
                actor=recipient,
                payload={"sent": sent},
            )
        )
        session.flush()

    def _format_status_text(
        self,
        task: Task,
        history: TaskStatusHistory,
    ) -> str:
        old = history.from_status.value if history.from_status else "?"
        new = history.to_status.value
        return (
            f"📌 #{task.id} {task.title} — {old} → {new.replace('_', ' ')}"
        )

    def _format_edit_text(
        self,
        task: Task,
        applied: dict[str, str],
        actor_user_id: str | None,
    ) -> str:
        if not applied:
            changes = "edited"
        else:
            parts = [f"{k}={v}" if v else f"{k} cleared" for k, v in applied.items()]
            changes = ", ".join(parts)
        actor = f" by {actor_user_id}" if actor_user_id else ""
        return f"✏ #{task.id} {task.title} — {changes}{actor}"


# ----------------------------------------------------------------------- #
# Module-level singleton — set once at startup, read everywhere.
# ----------------------------------------------------------------------- #

_active: SubscriberDispatcher | None = None


def set_active_dispatcher(d: SubscriberDispatcher | None) -> None:
    global _active
    _active = d


def get_active_dispatcher() -> SubscriberDispatcher | None:
    return _active


def dispatch_status_change(
    session: Session,
    *,
    task: Task,
    history: TaskStatusHistory,
) -> int:
    """Best-effort dispatch via the registered dispatcher. Returns 0
    when no dispatcher is set (tests, dev runs without senders).
    Never raises."""
    d = _active
    if d is None:
        return 0
    try:
        return d.dispatch_status_change(session, task=task, history=history)
    except Exception as e:  # noqa: BLE001
        log.warning("subscriber_update_dispatch_failed", task_id=task.id, error=str(e))
        return 0


def dispatch_edit(
    session: Session,
    *,
    task: Task,
    applied_payload: dict[str, str],
    actor_user_id: str | None = None,
) -> int:
    """Best-effort edit fanout. Same caveats as
    :func:`dispatch_status_change`."""
    d = _active
    if d is None:
        return 0
    try:
        return d.dispatch_edit(
            session, task=task, applied_payload=applied_payload, actor_user_id=actor_user_id
        )
    except Exception as e:  # noqa: BLE001
        log.warning("subscriber_edit_dispatch_failed", task_id=task.id, error=str(e))
        return 0
