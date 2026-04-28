"""Reply-conversation state for the Telegram listener (FR-CR-04-29).

Telegram has no modals — the equivalent of "Mark done with artifact"
or "Edit task" is a follow-up reply. The listener posts a prompt
message and waits for the user to **reply to that prompt** with the
answer; we then parse and apply it.

Scope:

- Single-process state. Lives in memory inside the listener; if the
  worker restarts, in-flight pendings are lost — the user just
  needs to click the button again. Acceptable for a single-listener
  deployment.
- TTL bound (default 10 min). Older pendings are evicted on the
  next access so the registry doesn't grow unbounded.
- Keyed by ``(chat_id, user_id, prompt_message_id)``. The strict
  match consumes a reply only when its
  ``reply_to_message.message_id`` equals the prompt's id, so
  unrelated chat traffic doesn't accidentally trigger a handler.
  Telegram's force-reply isn't binding — the user can just type
  into the main composer — so a *lenient* fallback also matches a
  message when the (chat, user) pair has exactly one unexpired
  pending. More than one open prompt ⇒ ambiguous and we bail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class PendingQuestion:
    """A question the bot is waiting for the user to answer."""

    action: str  # "artifact" | "edit"
    task_id: int
    chat_id: int
    user_id: int
    prompt_message_id: int
    expires_at: datetime


class PendingRegistry:
    """In-memory store of pending questions, keyed by
    ``(chat_id, user_id, prompt_message_id)``.

    The listener calls ``register`` after posting a prompt and
    ``take`` (which both fetches and removes) when a reply arrives.
    """

    def __init__(self, *, ttl_seconds: int = 600) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._items: dict[tuple[int, int, int], PendingQuestion] = {}

    def register(
        self,
        *,
        action: str,
        task_id: int,
        chat_id: int,
        user_id: int,
        prompt_message_id: int,
    ) -> PendingQuestion:
        q = PendingQuestion(
            action=action,
            task_id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            prompt_message_id=prompt_message_id,
            expires_at=datetime.now(timezone.utc) + self._ttl,
        )
        self._items[(chat_id, user_id, prompt_message_id)] = q
        return q

    def take(
        self,
        *,
        chat_id: int,
        user_id: int,
        reply_to_message_id: int | None,
    ) -> PendingQuestion | None:
        """Match a reply to a registered prompt and remove it.

        Strict path: ``reply_to_message_id`` matches a prompt id.
        Lenient path (Telegram force-reply isn't binding — the user
        can ignore it and just type into the main composer): if the
        chat/user has exactly **one** unexpired pending right now,
        consume it. This rescues the common "I just typed `завтра`
        instead of replying" case without opening the registry up to
        cross-flow ambiguity.
        """
        now = datetime.now(timezone.utc)
        if reply_to_message_id is not None:
            # Strict path: the user IS replying to a specific message.
            # Either it's our prompt (consume) or it isn't (bail —
            # don't grab an unrelated pending behind their back).
            key = (chat_id, user_id, reply_to_message_id)
            q = self._items.pop(key, None)
            if q is None or q.expires_at < now:
                return None
            return q

        # Lenient fallback: no `reply_to` at all. The user typed
        # straight into the composer, ignoring force-reply (very
        # common). If they have exactly one unexpired pending, take
        # it; more than one ⇒ ambiguous, bail.
        candidates = [
            (k, v) for k, v in self._items.items()
            if v.chat_id == chat_id
            and v.user_id == user_id
            and v.expires_at >= now
        ]
        if len(candidates) != 1:
            return None
        k, q = candidates[0]
        self._items.pop(k, None)
        return q

    def evict_expired(self) -> int:
        """Drop expired entries; called occasionally by the listener
        to keep the dict small. Returns the number of entries
        removed."""
        now = datetime.now(timezone.utc)
        dead = [k for k, v in self._items.items() if v.expires_at < now]
        for k in dead:
            self._items.pop(k, None)
        return len(dead)

    def __len__(self) -> int:
        return len(self._items)
