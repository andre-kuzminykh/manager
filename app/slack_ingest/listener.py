"""FR-CR-05-162 — Slack message → task ingestion, TG-only output.

Listens to Slack channels via Socket Mode (where the bot is added),
extracts tasks via LLM, persists Tasks with source_kind='slack', and
ships TG-карточки to author + owner + admins.

NO Slack-side output: bot does NOT reply, ack with emoji, or DM
back. Operator-pinned: «не выводить ни в диалогах ни в самом слаке,
только в телеграме».

Pipeline is feature-parity with the Telegram ingest path
(`app/telegram_ingest/service.py`):

  1. Skip self / bot / service messages.
  2. Build context window via ContextRetriever (history before +
     thread messages).
  3. Classify the message via the LLM graph (returns
     ``classification.tasks: list[TaskDraft]`` — a single message
     can carry multiple tasks).
  4. For each candidate task:
       a. Resolve owner via the shared ``_resolve_owner`` chain
          (registry → LLM-picked uid → sender fallback → admin).
       b. Intra-message dedup — skip exact-title repeats inside
          one message.
       c. Cross-DB dedup via ``check_duplicate(...)`` — skip when
          the LLM says the candidate duplicates an open task.
       d. Persist context snapshot + inference + draft + Task row
          (source_kind=slack).
       e. Post TG card (author + owner + admins) via the
          privacy-by-default DM path.

Feature-flagged: requires SLACK_INGEST_ENABLED=true + SLACK_APP_TOKEN
(xapp-...) for Socket Mode.
"""
from __future__ import annotations

import re
from typing import Any

# Slack mention token «<@U0123ABCD>» (optionally «<@U…|label>»).
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def _mention_uids(text: str, *, exclude: str | None = None) -> list[str]:
    """FR-CR-05-232 — ordered, de-duplicated @mentioned user ids in the
    message (the delegation addressees), minus the author."""
    out: list[str] = []
    for uid in _MENTION_RE.findall(text or ""):
        if uid == exclude or uid in out:
            continue
        out.append(uid)
    return out

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from app.config import Settings
from app.context.retriever import ContextRetriever
from app.db import session_scope
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.models import Employee
from app.orchestrator.service import Orchestrator
from app.persistence.tasks import create_task_from_draft
from app.schemas.intent import IntentType, InvocationType
from app.services import EmployeeDirectory
from app.services.task_dedup import check_duplicate
from app.slack_bot.handlers.shared import (
    Services,
    upsert_conversation,
    upsert_message,
)
from app.telegram_bot.cards import post_initial_card
from app.telegram_bot.sender import TelegramSender
from app.telegram_ingest.service import (
    _admin_fallback_owner_id,
    _resolve_owner,
)

log = get_logger(__name__)


_SKIPPED_SUBTYPES = {
    "bot_message",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "message_changed",
    "message_deleted",
    "thread_broadcast",  # parent already processed
}


def make_slack_ingest_app(
    *,
    settings: Settings,
    classifier: IntentClassifier,
    tg_sender: TelegramSender | None,
) -> App:
    """Construct the Slack Bolt App with a single `message` handler.

    Args:
      settings: loaded Settings (needs slack_bot_token, slack_app_token,
        context_window_before).
      classifier: pre-built IntentClassifier (LLM-backed).
      tg_sender: TelegramSender for posting card DMs. None disables
        TG side (testing).
    """
    app = App(token=settings.slack_bot_token)
    bot_user_id: str | None = None
    try:
        auth_resp = app.client.auth_test()
        bot_user_id = auth_resp.get("user_id")
    except Exception as e:  # noqa: BLE001
        log.warning("slack_ingest_auth_test_failed", error=str(e))

    context_retriever = ContextRetriever(
        app.client, window_before=settings.context_window_before
    )
    employees = EmployeeDirectory(client=app.client, settings=settings)

    services = Services(
        slack=app.client,
        context_retriever=context_retriever,
        classifier=classifier,
        orchestrator=Orchestrator(settings=settings),
        employees=employees,
    )

    @app.event("message")
    def _on_message(event: dict[str, Any], body: dict, client: WebClient, context, ack):  # noqa: ANN001
        ack()
        if bot_user_id and event.get("user") == bot_user_id:
            return
        if event.get("bot_id"):
            return
        subtype = event.get("subtype")
        if subtype in _SKIPPED_SUBTYPES:
            return
        text = (event.get("text") or "").strip()
        if not text:
            return
        author_slack_uid = event.get("user")
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        if not (author_slack_uid and channel_id and message_ts):
            return

        log.info(
            "slack_ingest_message_received",
            channel=channel_id, ts=message_ts, user=author_slack_uid,
            text_preview=text[:120],
        )

        try:
            _process(
                services=services,
                event=event,
                author_slack_uid=author_slack_uid,
                channel_id=channel_id,
                message_ts=message_ts,
                tg_sender=tg_sender,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_ingest_message_processing_failed",
                channel=channel_id, ts=message_ts, error=str(e),
            )

    @app.event("app_mention")
    def _on_app_mention(event, body, client, context, ack):  # noqa: ANN001
        # FR-CR-05-162 — mention события приходят дополнительно к
        # message событиям. Чтобы не дублировать обработку — игнорим
        # mentions (они же будут как обычные message events).
        ack()

    return app


def _known_employees_from_db(session) -> list[dict[str, Any]]:
    """Same shape as `_known_members_for` in TG-ingest — pulled from
    the Employees table (synced from Slack via EmployeeDirectory)."""
    return [
        {
            "slack_user_id": e.slack_user_id,
            "display_name": e.display_name or e.real_name or e.slack_user_id,
            "real_name": e.real_name,
        }
        for e in (
            session.query(Employee)
            .filter(Employee.is_bot.is_(False))
            .all()
        )
    ]


def _author_display_from_registry(
    slack_uid: str, known_employees: list[dict[str, Any]]
) -> str | None:
    for e in known_employees or []:
        if e.get("slack_user_id") == slack_uid:
            return e.get("display_name") or e.get("real_name")
    return None


def _process(
    *,
    services: Services,
    event: dict[str, Any],
    author_slack_uid: str,
    channel_id: str,
    message_ts: str,
    tg_sender: TelegramSender | None,
) -> None:
    """End-to-end multi-task pipeline matching TG-ingest semantics.

    A single Slack message can carry multiple tasks. Each surviving
    candidate (after intra+cross dedup) becomes its own Task row;
    each Task gets its own TG card.
    """
    source_message = {
        "ts": message_ts,
        "thread_ts": event.get("thread_ts"),
        "user": author_slack_uid,
        "text": event.get("text") or "",
        "channel": channel_id,
        "subtype": event.get("subtype"),
    }

    with session_scope() as session:
        # --- 1. Conversation + raw message upsert -------------------
        conversation = upsert_conversation(
            session, channel_id=channel_id, kind="channel",
        )
        upsert_message(
            session,
            conversation=conversation,
            message=source_message,
            raw=event,
            subtype=event.get("subtype"),
        )

        # --- 2. Keep Employees directory fresh ----------------------
        if services.employees is not None:
            try:
                services.employees.observed(
                    session, slack_user_id=author_slack_uid
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                services.employees.ensure_channel_synced(
                    session, channel_id=channel_id
                )
            except Exception:  # noqa: BLE001
                pass

        # --- 3. Context window + classify ---------------------------
        try:
            window = services.context_retriever.build(
                conversation_id=channel_id,
                source_message=source_message,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_ingest_context_failed",
                channel=channel_id, ts=message_ts, error=str(e),
            )
            return

        known_employees = _known_employees_from_db(session)
        try:
            classification = services.classifier.classify(
                context=window,
                invocation_type=InvocationType.passive,
                known_employees=known_employees,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_ingest_classify_failed",
                channel=channel_id, ts=message_ts, error=str(e),
            )
            return

        if (
            classification.intent != IntentType.create_task
            or not classification.tasks
        ):
            log.info(
                "slack_ingest_no_tasks",
                channel=channel_id, ts=message_ts,
                intent=classification.intent.value,
                hint="non-task intent or empty extraction",
            )
            return

        # --- 4. Resolve owner per candidate -------------------------
        admin_uid = _admin_fallback_owner_id()
        sender_name = _author_display_from_registry(
            author_slack_uid, known_employees
        )
        # FR-CR-05-232 — addressees of the message (minus author) so the
        # owner chain prefers «please review @X» over blaming the sender.
        mention_uids = _mention_uids(
            event.get("text") or "", exclude=author_slack_uid
        )
        for td in classification.tasks:
            _resolve_owner(
                td,
                known_employees=known_employees,
                sender_user_id=author_slack_uid,
                sender_user_name=sender_name,
                admin_uid=admin_uid,
                mention_uids=mention_uids,
            )

        # --- 5. Permalink (best-effort) -----------------------------
        permalink: str | None = None
        try:
            permalink_resp = services.slack.chat_getPermalink(
                channel=channel_id, message_ts=message_ts,
            )
            if permalink_resp.get("ok"):
                permalink = permalink_resp.get("permalink")
        except Exception:  # noqa: BLE001
            pass

        source_dict = {
            "kind": "slack",
            "conversation_id": channel_id,
            "message_ts": message_ts,
            "thread_ts": event.get("thread_ts"),
            "permalink": permalink,
        }

        # --- 6. Persist snapshot once, then loop tasks --------------
        snapshot = services.orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )

        created_tasks: list[Any] = []
        seen_titles: set[str] = set()
        for td in classification.tasks:
            t_lower = (td.title or "").strip().lower()
            if t_lower and t_lower in seen_titles:
                log.info(
                    "slack_ingest_skipped_intra_message_duplicate",
                    title=td.title, channel=channel_id, ts=message_ts,
                )
                continue
            seen_titles.add(t_lower)

            try:
                dup = check_duplicate(
                    session,
                    candidate=td.model_dump(mode="json"),
                    llm_backend=getattr(services.classifier, "backend", None),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "slack_ingest_dedup_check_failed",
                    title=td.title, error=str(e),
                )
                dup = None
            if dup is not None and dup.is_duplicate:
                log.info(
                    "slack_ingest_skipped_duplicate",
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
            try:
                inference = services.orchestrator.persist_inference(
                    session,
                    context_snapshot=snapshot,
                    classification=single,
                    invocation_type=InvocationType.passive,
                )
                draft = services.orchestrator.create_draft(
                    session,
                    inference=inference,
                    classification=single,
                    created_by_slack_user_id=author_slack_uid,
                    slack_message_ts=message_ts,
                )
                task = create_task_from_draft(
                    session,
                    draft=draft,
                    source=source_dict,
                    context_snapshot_id=snapshot.id,
                    fallback_author_slack_id=author_slack_uid,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "slack_ingest_persist_task_failed",
                    title=td.title, channel=channel_id, ts=message_ts,
                    error=str(e),
                )
                continue

            log.info(
                "slack_ingest_task_created",
                task_id=task.id, title=task.title,
                owner=task.owner_display_name,
                owner_uid=task.owner_user_id,
                channel=channel_id, ts=message_ts,
            )
            created_tasks.append(task)

            # TG card per task — author + owner + admins
            if tg_sender is None or not getattr(tg_sender, "enabled", False):
                log.info(
                    "slack_ingest_tg_sender_disabled",
                    task_id=task.id,
                    hint="TELEGRAM_BOT_TOKEN missing — task created, no card sent",
                )
                continue
            try:
                post_initial_card(
                    sender=tg_sender,
                    session=session,
                    task=task,
                    chat_id=0,  # ignored — privacy-by-default DM path
                    reply_to_message_id=None,
                    author_user_id=author_slack_uid,
                    for_slack_ingest=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "slack_ingest_tg_card_post_failed",
                    task_id=task.id, error=str(e),
                )

        if not created_tasks:
            log.info(
                "slack_ingest_no_tasks_persisted",
                channel=channel_id, ts=message_ts,
                hint="all candidates were duplicates or failed to persist",
            )


def run_socket_mode(
    *,
    settings: Settings,
    classifier: IntentClassifier,
    tg_sender: TelegramSender | None,
) -> None:
    """Block until killed. Uses Slack App-Level Token (xapp-...)
    for Socket Mode."""
    if not settings.slack_bot_token:
        log.error("slack_ingest_no_bot_token")
        return
    if not settings.slack_app_token:
        log.error("slack_ingest_no_app_token")
        return
    bolt_app = make_slack_ingest_app(
        settings=settings, classifier=classifier, tg_sender=tg_sender,
    )
    log.info("slack_ingest_starting")
    SocketModeHandler(bolt_app, settings.slack_app_token).start()


__all__ = ["make_slack_ingest_app", "run_socket_mode"]
