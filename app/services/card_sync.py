"""Keep the in-channel task card and the owner's DM mirror in sync with the
latest DB state via chat.update."""
from __future__ import annotations

from typing import Any, Protocol

from app.logging_setup import get_logger
from app.models import Task
from app.slack_bot import blocks as bk

log = get_logger(__name__)


class _Sender(Protocol):
    def update_message(self, **kwargs) -> dict: ...


def refresh_task_card(sender: _Sender, task: Task) -> None:
    """Update both the channel widget (task.card_*) and the DM mirror
    (task.dm_*). Failures are swallowed — the card is UX, the DB is source
    of truth."""
    card = bk.task_card(task=task, viewer_slack_user_id=task.owner_user_id)
    text = f":clipboard: Task #{task.id}: {task.title}"

    for channel, ts in (
        (task.card_channel, task.card_ts),
        (task.dm_channel, task.dm_ts),
    ):
        if not channel or not ts:
            continue
        try:
            sender.update_message(
                channel=channel, ts=ts, blocks=card, text=text
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "task_card_refresh_failed",
                task_id=task.id,
                channel=channel,
                error=str(e),
            )
