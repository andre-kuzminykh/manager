"""FR-CR-05-162 — Slack message → task ingestion, TG-only output.

Listens to Slack channels via Socket Mode (where the bot is added),
extracts tasks via LLM (using existing classify_and_persist pipeline),
persists Task with source_kind='slack', and ships TG-карточки to
owner + admins.

NO Slack-side output: bot does NOT reply, ack with emoji, or DM
back. Operator-pinned: «не выводить ни в диалогах ни в самом слаке,
только в телеграме».

Feature-flagged: requires SLACK_INGEST_ENABLED=true + SLACK_APP_TOKEN
(xapp-...) for Socket Mode.
"""
from __future__ import annotations

from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from app.config import Settings
from app.context.retriever import ContextRetriever
from app.db import session_scope
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.persistence.tasks import create_task_from_draft
from app.schemas.intent import InvocationType
from app.services import EmployeeDirectory
from app.slack_bot.handlers.shared import Services, classify_and_persist
from app.telegram_bot.cards import post_initial_card
from app.telegram_bot.sender import TelegramSender

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

    # FR-CR-05-162 — finalizer / orchestrator NOT needed (no Slack
    # output). Set to None placeholders compatible with Services
    # dataclass.
    services = Services(
        slack=app.client,
        context_retriever=context_retriever,
        classifier=classifier,
        orchestrator=None,  # type: ignore[arg-type]
        employees=employees,
    )

    @app.event("message")
    def _on_message(event: dict[str, Any], body: dict, client: WebClient, context, ack):  # noqa: ANN001
        ack()
        # 1. Skip self / bot / service messages.
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


def _process(
    *,
    services: Services,
    event: dict[str, Any],
    author_slack_uid: str,
    channel_id: str,
    message_ts: str,
    tg_sender: TelegramSender | None,
) -> None:
    """One-shot: classify → draft → persist Task → send TG card."""
    source_message = {
        "ts": message_ts,
        "thread_ts": event.get("thread_ts"),
        "user": author_slack_uid,
        "text": event.get("text") or "",
        "channel": channel_id,
        "subtype": event.get("subtype"),
    }

    with session_scope() as session:
        try:
            _classification, draft, _snapshot = classify_and_persist(
                session,
                services=services,
                conversation_id=channel_id,
                kind="channel",  # public + private + im все идут как "channel"
                source_message=source_message,
                invocation_type=InvocationType.PASSIVE,
                slack_user_id=author_slack_uid,
                raw_event=event,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_ingest_classify_failed",
                channel=channel_id, ts=message_ts, error=str(e),
            )
            return

        if draft is None:
            log.info(
                "slack_ingest_no_draft",
                channel=channel_id, ts=message_ts,
                hint="non-task intent or duplicate",
            )
            return

        # FR-CR-05-162 — поскольку invocation=PASSIVE, classify_and_persist
        # сохраняет draft в state=proposed. Чтобы запустить TG-карточку
        # как для подтверждённой задачи, надо превратить в Task сразу.
        # operator-pinned: «вычленяет задачи и публикует в телеграме» —
        # без human-in-the-loop на Slack-стороне.
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
        try:
            task = create_task_from_draft(
                session,
                draft=draft,
                source=source_dict,
                context_snapshot_id=None,
                fallback_author_slack_id=author_slack_uid,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_ingest_persist_task_failed",
                channel=channel_id, ts=message_ts, error=str(e),
            )
            return

        log.info(
            "slack_ingest_task_created",
            task_id=task.id, title=task.title, owner=task.owner_display_name,
            channel=channel_id, ts=message_ts,
        )

        # FR-CR-05-162 — TG-карточка владельцу + админам.
        if tg_sender is None or not getattr(tg_sender, "enabled", False):
            log.info(
                "slack_ingest_tg_sender_disabled",
                task_id=task.id,
                hint="TELEGRAM_BOT_TOKEN missing — task created, no card sent",
            )
            return
        try:
            post_initial_card(
                sender=tg_sender,
                session=session,
                task=task,
                chat_id=0,  # ignored — privacy-by-default DM path
                reply_to_message_id=None,
                author_user_id=author_slack_uid,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_ingest_tg_card_post_failed",
                task_id=task.id, error=str(e),
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
