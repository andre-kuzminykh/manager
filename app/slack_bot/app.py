"""Bolt app wiring: registers all event, shortcut, action and view handlers."""
from __future__ import annotations

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.config import Settings
from app.context import ContextRetriever
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.orchestrator import Orchestrator
from app.orchestrator.finalize import FinalizeService
from app.slack_bot.blocks import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_IGNORE,
    ACTION_MANAGE_SUBSCRIPTIONS,
    ACTION_MARK_DONE,
    ACTION_OPEN_SOURCE,
    ACTION_SHOW_CONTEXT,
    ACTION_START_WORK,
    ACTION_SUBMIT_REVIEW,
    ACTION_SUBSCRIBE,
    ACTION_UNSUBSCRIBE,
    ACTION_UNSUBSCRIBE_IN_MODAL,
    MODAL_CALLBACK_MEETING,
    MODAL_CALLBACK_TASK,
)
from app.slack_bot.handlers.actions import handle_confirm, handle_edit, handle_ignore
from app.slack_bot.handlers.events import handle_app_mention, handle_message
from app.slack_bot.handlers.shared import Services
from app.slack_bot.handlers.shortcuts import (
    SHORTCUT_CREATE_MEETING,
    SHORTCUT_CREATE_TASK,
    handle_shortcut,
)
from app.slack_bot.handlers.task_actions import (
    handle_manage_subscriptions,
    handle_mark_done,
    handle_open_source,
    handle_show_context,
    handle_start_work,
    handle_submit_review,
    handle_subscribe,
    handle_unsubscribe,
    handle_unsubscribe_in_modal,
)
from app.slack_bot.handlers.views import (
    handle_meeting_modal_submit,
    handle_task_modal_submit,
)
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def build_app(
    *,
    settings: Settings,
    classifier: IntentClassifier,
    finalizer: FinalizeService,
) -> App:
    app = App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret or None,
    )
    sender = RateAwareSlackSender(app.client)
    # Feed the sender into the finalizer so the post-confirm task card can be
    # published (CR-01).
    if getattr(finalizer, "_sender", None) is None:
        finalizer._sender = sender  # type: ignore[attr-defined]
    context_retriever = ContextRetriever(
        app.client, window_before=settings.context_window_before
    )
    orchestrator = Orchestrator(settings)
    services = Services(
        slack=app.client,
        context_retriever=context_retriever,
        classifier=classifier,
        orchestrator=orchestrator,
    )

    # ---- Events ----
    @app.event("app_mention")
    def _on_mention(event, body, client, context, ack):
        handle_app_mention(
            event=event,
            body=body,
            client=client,
            context=context,
            services=services,
            sender=sender,
            ack=ack,
        )

    @app.event("message")
    def _on_message(event, body, client, context, ack):
        handle_message(
            event=event,
            body=body,
            client=client,
            context=context,
            services=services,
            sender=sender,
            ack=ack,
        )

    # ---- Shortcuts ----
    @app.shortcut(SHORTCUT_CREATE_TASK)
    def _on_shortcut_task(shortcut, client, ack):
        handle_shortcut(shortcut=shortcut, client=client, services=services, ack=ack)

    @app.shortcut(SHORTCUT_CREATE_MEETING)
    def _on_shortcut_meeting(shortcut, client, ack):
        handle_shortcut(shortcut=shortcut, client=client, services=services, ack=ack)

    # ---- Button actions on draft cards ----
    @app.action(ACTION_CONFIRM)
    def _on_confirm(body, client, ack):
        handle_confirm(
            body=body,
            client=client,
            services=services,
            finalizer=finalizer,
            sender=sender,
            ack=ack,
        )

    @app.action(ACTION_EDIT)
    def _on_edit(body, client, ack):
        handle_edit(body=body, client=client, ack=ack)

    @app.action(ACTION_IGNORE)
    def _on_ignore(body, ack):
        handle_ignore(body=body, ack=ack, sender=sender)

    # ---- Modal submissions ----
    @app.view(MODAL_CALLBACK_TASK)
    def _on_task_submit(body, ack, view):
        handle_task_modal_submit(
            body=body,
            view=view,
            services=services,
            finalizer=finalizer,
            sender=sender,
            ack=ack,
        )

    @app.view(MODAL_CALLBACK_MEETING)
    def _on_meeting_submit(body, ack, view):
        handle_meeting_modal_submit(
            body=body,
            view=view,
            services=services,
            finalizer=finalizer,
            sender=sender,
            ack=ack,
        )

    # ---- CR-01: Task card actions ----
    @app.action(ACTION_START_WORK)
    def _on_start_work(body, ack):
        handle_start_work(body=body, sender=sender, ack=ack)

    @app.action(ACTION_SUBMIT_REVIEW)
    def _on_submit_review(body, ack):
        handle_submit_review(body=body, sender=sender, ack=ack)

    @app.action(ACTION_MARK_DONE)
    def _on_mark_done(body, ack):
        handle_mark_done(body=body, sender=sender, ack=ack)

    @app.action(ACTION_SUBSCRIBE)
    def _on_subscribe(body, ack):
        handle_subscribe(body=body, sender=sender, ack=ack)

    @app.action(ACTION_UNSUBSCRIBE)
    def _on_unsubscribe(body, ack):
        handle_unsubscribe(body=body, sender=sender, ack=ack)

    @app.action(ACTION_OPEN_SOURCE)
    def _on_open_source(body, ack):
        handle_open_source(body=body, ack=ack)

    @app.action(ACTION_SHOW_CONTEXT)
    def _on_show_context(body, client, ack):
        handle_show_context(body=body, client=client, ack=ack)

    @app.action(ACTION_MANAGE_SUBSCRIPTIONS)
    def _on_manage_subs(body, client, ack):
        handle_manage_subscriptions(body=body, client=client, ack=ack)

    @app.action(ACTION_UNSUBSCRIBE_IN_MODAL)
    def _on_unsubscribe_modal(body, client, ack):
        handle_unsubscribe_in_modal(body=body, client=client, ack=ack)

    return app


def run_socket_mode(app: App, app_token: str) -> None:
    SocketModeHandler(app, app_token).start()
