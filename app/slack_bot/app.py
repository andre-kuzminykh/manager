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
        handle_ignore(body=body, ack=ack)

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

    return app


def run_socket_mode(app: App, app_token: str) -> None:
    SocketModeHandler(app, app_token).start()
