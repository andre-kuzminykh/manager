"""CR-03 Phase B: admin review orchestration.

When a task is auto-created on high-confidence passive detection, we:
- DM every configured admin with the admin_review_card,
- optionally post an ephemeral message in the source thread targeting
  each admin (thread comment visible only to the admin),
- log an `audit_logs` row (category=admin_review, action=awaiting_confirmation).
"""
from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AuditLog, Task
from app.services.employees import admin_slack_user_ids
from app.slack_bot import blocks as bk

log = get_logger(__name__)


class _Sender(Protocol):
    def post_message(self, **kwargs) -> dict: ...
    def post_ephemeral(self, **kwargs) -> dict: ...


def post_admin_review(
    session: Session,
    *,
    task: Task,
    sender: _Sender,
    source_channel: str | None = None,
    source_thread_ts: str | None = None,
    source_permalink: str | None = None,
    reasoning: str | None = None,
    admins: set[str] | None = None,
) -> int:
    """Broadcast the admin review widget to every admin. Returns number of
    recipients that were reachable. Always writes an audit row."""

    admin_ids = admins if admins is not None else admin_slack_user_ids()
    if not admin_ids:
        session.add(
            AuditLog(
                category="admin_review",
                action="awaiting_confirmation",
                entity_type="task",
                entity_id=str(task.id),
                payload={"admins": [], "reason": "no admins configured"},
            )
        )
        session.flush()
        return 0

    card = bk.admin_review_card(
        task=task, reasoning=reasoning, source_permalink=source_permalink
    )
    reachable = 0
    delivered_to: list[str] = []
    for admin_id in sorted(admin_ids):
        if admin_id == (task.owner_user_id or ""):
            # Owner's DM already carries the full card via finalize — skip
            # to avoid two widgets in the same conversation.
            continue
        try:
            sender.post_message(
                channel=admin_id,
                blocks=card,
                text=f"Task #{task.id} pending review",
            )
            reachable += 1
            delivered_to.append(admin_id)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "admin_review_dm_failed",
                admin=admin_id,
                task_id=task.id,
                error=str(e),
            )

        # Optional ephemeral comment in the source thread.
        if source_channel and source_thread_ts:
            try:
                sender.post_ephemeral(
                    channel=source_channel,
                    user=admin_id,
                    thread_ts=source_thread_ts,
                    blocks=card,
                    text=f"Task #{task.id} pending review",
                )
            except Exception as e:  # noqa: BLE001
                log.info(
                    "admin_review_ephemeral_failed",
                    admin=admin_id,
                    task_id=task.id,
                    error=str(e),
                )

    session.add(
        AuditLog(
            category="admin_review",
            action="awaiting_confirmation",
            entity_type="task",
            entity_id=str(task.id),
            payload={"admins": delivered_to},
        )
    )
    session.flush()
    return reachable
