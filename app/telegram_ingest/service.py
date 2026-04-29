"""Process Telegram source messages through the existing intent
pipeline and persist the resulting tasks in our local DB."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.context.retriever import ContextWindow
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.models import (
    ActionDraft,
    ActionDraftState,
    ProcessedTelegramMessage,
    Task,
    TaskSourceKind,
)
from app.orchestrator import Orchestrator
from app.persistence import create_task_from_draft
from app.schemas.intent import InvocationType, IntentType
from app.telegram_ingest.reader import TelegramSourceMessage

log = get_logger(__name__)


@dataclass
class IngestReport:
    """Counters returned from a batch run."""

    seen: int = 0
    skipped_already_processed: int = 0
    skipped_empty_text: int = 0
    no_action: int = 0
    tasks_created: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def _build_window(msg: TelegramSourceMessage) -> ContextWindow:
    """Wrap a TelegramSourceMessage in a Slack-shaped ContextWindow.

    The intent pipeline reads ``conversation_id`` / ``source_ts`` /
    ``source_message`` — we hand it Telegram identifiers in the same
    fields so no pipeline code changes.
    """
    return ContextWindow(
        conversation_id=str(msg.chat_id),
        source_ts=str(msg.message_id),
        thread_ts=str(msg.reply_to) if msg.reply_to else None,
        source_message={
            "ts": str(msg.message_id),
            "thread_ts": str(msg.reply_to) if msg.reply_to else None,
            "user": str(msg.user_id) if msg.user_id else (msg.user_name or "tg_unknown"),
            "text": msg.text,
            "subtype": None,
        },
    )


def _telegram_permalink(msg: TelegramSourceMessage) -> str | None:
    """Best-effort shareable link to the original Telegram message.

    The ``t.me/c/<id>/<msg>`` URL scheme **only works for supergroups
    and channels** — those carry chat_ids with the ``-100`` prefix
    (so ``|id| > 10**12``). Basic groups have small negative ids and
    no public URL form: a generated link would 404 with «no access»
    even for members. Private chats: same — no shareable URL.
    """
    if msg.chat_id >= 0:
        return None
    public = abs(msg.chat_id)
    if public <= 1000000000000:
        # Basic group — no shareable URL.
        return None
    public -= 1000000000000
    return f"https://t.me/c/{public}/{msg.message_id}"


class TelegramIngestService:
    """Pulls a batch of Telegram messages through the intent pipeline
    and writes confirmed tasks into the local DB.

    The service is driven externally — by a one-shot CLI
    (``ops/telegram_ingest.py``) or the history migration script.
    Each message either yields a Task (status = create_task) or is
    recorded as "no_action" so the next ingest pass skips it.
    """

    def __init__(
        self,
        *,
        classifier: IntentClassifier,
        orchestrator: Orchestrator,
    ) -> None:
        self._classifier = classifier
        self._orchestrator = orchestrator

    def process_one(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ) -> Task | None:
        """Back-compat wrapper around :meth:`process_all`. Returns the
        FIRST created Task or ``None`` when nothing was created. Most
        callers should switch to :meth:`process_all` to support
        multi-task messages (FR-CR-05-05)."""
        tasks = self.process_all(session, message)
        return tasks[0] if tasks else None

    def process_all(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ) -> list[Task]:
        """FR-CR-05-05: process a Telegram message and return EVERY
        Task it produced. A single message can carry multiple tasks
        («сделать презу к завтра и отчёт к пятнице» → 2 tasks). Each
        ``classification.tasks`` entry becomes its own Task row;
        they share the same ``processed_telegram_messages`` bookmark
        (pointed at the first Task — back-compat with single-task
        callers and the FR-CR-04-26 schema).
        """
        existing = session.get(
            ProcessedTelegramMessage, (message.chat_id, message.message_id)
        )
        if existing is not None:
            return []
        if not message.is_textual:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        window = _build_window(message)
        classification = self._classifier.classify(
            context=window,
            invocation_type=InvocationType.passive,
            known_employees=None,
        )

        if classification.intent != IntentType.create_task or not classification.tasks:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        # FR-CR-04-30 — fill the owner from the sender on each draft
        # when the LLM didn't extract one. Same rule applies to every
        # task in a multi-task message.
        for td in classification.tasks:
            if not td.owner_user_id and message.user_id:
                td.owner_user_id = str(message.user_id)
            if not td.owner_display_name and message.user_name:
                td.owner_display_name = message.user_name

        snapshot = self._orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )
        author_id = window.source_message["user"]
        author_str = str(author_id) if author_id else None
        source = {
            "kind": TaskSourceKind.telegram.value,
            "conversation_id": str(message.chat_id),
            "message_ts": str(message.message_id),
            "thread_ts": str(message.reply_to) if message.reply_to else None,
            "permalink": _telegram_permalink(message),
        }

        out: list[Task] = []
        for td in classification.tasks:
            # Dedup against the last 20 open tasks. Skip the candidate
            # silently when the LLM says it duplicates an existing one
            # — the source-message bookmark below ensures we don't
            # re-classify it on the next pass.
            from app.services.task_dedup import check_duplicate

            dup = check_duplicate(
                session,
                candidate=td.model_dump(mode="json"),
                llm_backend=getattr(self._classifier, "backend", None),
            )
            if dup.is_duplicate:
                log.info(
                    "telegram_ingest_skipped_duplicate",
                    title=td.title,
                    duplicate_of=dup.duplicate_of_task_id,
                    reason=dup.reason,
                )
                continue

            # Each per-chunk inference + draft is its own row. The
            # IntentInference table doesn't carry the task draft body
            # so we just persist N copies — cheap, and keeps the
            # one-inference-per-Task invariant.
            single = type(classification)(
                intent=classification.intent,
                confidence=classification.confidence,
                reasoning=classification.reasoning,
                task=td,
            )
            inference = self._orchestrator.persist_inference(
                session,
                context_snapshot=snapshot,
                classification=single,
                invocation_type=InvocationType.passive,
            )
            draft = self._orchestrator.create_draft(
                session,
                inference=inference,
                classification=single,
                created_by_slack_user_id=author_str,
                slack_message_ts=str(message.message_id),
            )
            task = create_task_from_draft(
                session,
                draft=draft,
                source=source,
                context_snapshot_id=snapshot.id,
                fallback_author_slack_id=author_str,
            )
            out.append(task)

        session.add(
            ProcessedTelegramMessage(
                chat_id=message.chat_id,
                message_id=message.message_id,
                processed_at=datetime.now(timezone.utc),
                task_id=out[0].id if out else None,
            )
        )
        return out

    def prepare_draft(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ):
        """Confirm-first variant of `process_one` (FR-CR-04-32).

        Same up-front classification as `process_one`, but stops at the
        ActionDraft and skips Task creation. Used when a task-shaped
        message arrives in a group / supergroup / channel — we DM a
        confirmation widget to the author + admins and only finalise
        into a Task on Accept.

        Returns the persisted ``ActionDraft`` (state = ``proposed``)
        or ``None`` when the message was already processed, has no
        usable text, or didn't classify as a task.

        Idempotent: a second call with the same (chat_id, message_id)
        returns ``None`` (the bookmark in
        ``processed_telegram_messages`` short-circuits us).
        """
        drafts = self.prepare_drafts(session, message)
        return drafts[0] if drafts else None

    def prepare_drafts(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ) -> list:
        """FR-CR-05-05 + FR-CR-04-32: confirm-first variant of
        :meth:`process_all`. One ``ActionDraft`` per detected task,
        each carrying its own ``_pending`` block so the Accept
        handler can finalise it independently. The listener posts
        one widget per draft.
        """
        existing = session.get(
            ProcessedTelegramMessage, (message.chat_id, message.message_id)
        )
        if existing is not None:
            return []
        if not message.is_textual:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        window = _build_window(message)
        classification = self._classifier.classify(
            context=window,
            invocation_type=InvocationType.passive,
            known_employees=None,
        )

        if classification.intent != IntentType.create_task or not classification.tasks:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        for td in classification.tasks:
            if not td.owner_user_id and message.user_id:
                td.owner_user_id = str(message.user_id)
            if not td.owner_display_name and message.user_name:
                td.owner_display_name = message.user_name

        snapshot = self._orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )
        author_id = window.source_message["user"]
        author_str = str(author_id) if author_id else None

        out: list = []
        for td in classification.tasks:
            # Same dedup gate as `process_all`: skip the draft +
            # widget when the LLM thinks the candidate duplicates an
            # already-existing open Task. Source-message bookmark
            # below still gets written so we don't re-classify.
            from app.services.task_dedup import check_duplicate

            dup = check_duplicate(
                session,
                candidate=td.model_dump(mode="json"),
                llm_backend=getattr(self._classifier, "backend", None),
            )
            if dup.is_duplicate:
                log.info(
                    "telegram_prepare_drafts_skipped_duplicate",
                    title=td.title,
                    duplicate_of=dup.duplicate_of_task_id,
                    reason=dup.reason,
                )
                continue

            single = type(classification)(
                intent=classification.intent,
                confidence=classification.confidence,
                reasoning=classification.reasoning,
                task=td,
            )
            inference = self._orchestrator.persist_inference(
                session,
                context_snapshot=snapshot,
                classification=single,
                invocation_type=InvocationType.passive,
            )
            draft = self._orchestrator.create_draft(
                session,
                inference=inference,
                classification=single,
                created_by_slack_user_id=author_str,
                slack_message_ts=str(message.message_id),
            )
            payload = dict(draft.payload or {})
            payload["_pending"] = {
                "source_kind": "telegram",
                "conversation_id": str(message.chat_id),
                "message_ts": str(message.message_id),
                "thread_ts": str(message.reply_to) if message.reply_to else None,
                "permalink": _telegram_permalink(message),
                "fallback_author": author_str,
                "context_snapshot_id": snapshot.id,
                "source_chat_id": message.chat_id,
                "source_message_id": message.message_id,
            }
            draft.payload = payload
            out.append(draft)

        session.flush()
        session.add(
            ProcessedTelegramMessage(
                chat_id=message.chat_id,
                message_id=message.message_id,
                processed_at=datetime.now(timezone.utc),
                task_id=None,
            )
        )
        return out

    def process_batch(
        self,
        session: Session,
        messages: list[TelegramSourceMessage],
    ) -> IngestReport:
        """Process a slice of messages, returning a counters report.

        Errors per-message are caught so one bad row doesn't abort
        the whole batch — we record the chat/msg id in the report
        and move on. The caller decides whether to commit or roll
        back the transaction.
        """
        report = IngestReport()
        for m in messages:
            report.seen += 1
            try:
                # Idempotency check goes BEFORE the is_textual branch
                # so a re-run of the same batch (e.g. after a partial
                # backfill) doesn't trip on
                # `processed_telegram_messages_pkey` for messages
                # whose bookmark was already written by a prior run.
                existing = session.get(
                    ProcessedTelegramMessage, (m.chat_id, m.message_id)
                )
                if existing is not None:
                    report.skipped_already_processed += 1
                    continue
                if not m.is_textual:
                    report.skipped_empty_text += 1
                    session.add(
                        ProcessedTelegramMessage(
                            chat_id=m.chat_id,
                            message_id=m.message_id,
                            processed_at=datetime.now(timezone.utc),
                            task_id=None,
                        )
                    )
                    continue
                task = self.process_one(session, m)
                if task is None:
                    report.no_action += 1
                else:
                    report.tasks_created += 1
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                if len(report.error_samples) < 5:
                    report.error_samples.append(
                        f"chat={m.chat_id} msg={m.message_id}: {e!s}"
                    )
                log.warning(
                    "telegram_ingest_message_failed",
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    error=str(e),
                )
                # Try to record this as processed so we don't loop on it forever.
                # If even this fails (e.g. transaction is broken), the
                # surrounding session_scope rollback will still leave the
                # row unprocessed — that's acceptable for an MVP.
                try:
                    session.add(
                        ProcessedTelegramMessage(
                            chat_id=m.chat_id,
                            message_id=m.message_id,
                            processed_at=datetime.now(timezone.utc),
                            task_id=None,
                        )
                    )
                    session.flush()
                except Exception:
                    session.rollback()
        return report
