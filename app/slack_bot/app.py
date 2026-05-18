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
    ACTION_CANCEL_TASK,
    ACTION_CONFIRM,
    ACTION_DELETE_TASK,
    ACTION_EDIT,
    ACTION_IGNORE,
    ACTION_MANAGE_SUBSCRIPTIONS,
    ACTION_MARK_DONE,
    ACTION_OPEN_SOURCE,
    ACTION_SHOW_CONTEXT,
    ACTION_START_WORK,
    ACTION_SUBSCRIBE,
    ACTION_UNSUBSCRIBE,
    ACTION_UNSUBSCRIBE_IN_MODAL,
    ACTION_ADMIN_CONFIRM_TASK,
    ACTION_ADMIN_EDIT_TASK,
    ACTION_ADMIN_REJECT_TASK,
    ACTION_EDIT_TASK,
    ACTION_WEEKLY_ACCEPT,
    ACTION_WEEKLY_DEFER,
    ACTION_PLAN_APPROVE,
    ACTION_PLAN_SKIP,
    MODAL_CALLBACK_ADMIN_EDIT,
    MODAL_CALLBACK_COMPLETE_TASK,
    MODAL_CALLBACK_DELETE_TASK,
    MODAL_CALLBACK_EDIT_TASK,
    MODAL_CALLBACK_MEETING,
    MODAL_CALLBACK_TASK,
)
from app.slack_bot.handlers.actions import handle_confirm, handle_edit, handle_ignore
from app.slack_bot.handlers.admin_review import (
    handle_admin_confirm,
    handle_admin_edit_open,
    handle_admin_edit_submit,
    handle_admin_reject,
)
from app.slack_bot.handlers.events import handle_app_mention, handle_message
from app.slack_bot.handlers.shared import Services
from app.slack_bot.handlers.shortcuts import (
    SHORTCUT_CREATE_MEETING,
    SHORTCUT_CREATE_TASK,
    handle_shortcut,
)
from app.slack_bot.handlers.task_actions import (
    handle_cancel_task,
    handle_complete_task_submit,
    handle_delete_task_open,
    handle_delete_task_submit,
    handle_manage_subscriptions,
    handle_mark_done,
    handle_open_source,
    handle_show_context,
    handle_start_work,
    handle_subscribe,
    handle_task_edit_open,
    handle_task_edit_submit,
    handle_unsubscribe,
    handle_unsubscribe_in_modal,
)
from app.slack_bot.handlers.weekly_plan import (
    handle_weekly_accept,
    handle_weekly_defer,
)
from app.slack_bot.handlers.daily_plan import (
    handle_plan_approve,
    handle_plan_skip,
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
    from app.services import EmployeeDirectory

    employees = EmployeeDirectory(client=app.client, settings=settings)
    services = Services(
        slack=app.client,
        context_retriever=context_retriever,
        classifier=classifier,
        orchestrator=orchestrator,
        employees=employees,
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

    # ---- Task card actions ----
    @app.action(ACTION_START_WORK)
    def _on_start_work(body, ack):
        handle_start_work(body=body, sender=sender, ack=ack)

    @app.action(ACTION_MARK_DONE)
    def _on_mark_done(body, client, ack):
        handle_mark_done(body=body, sender=sender, client=client, ack=ack)

    @app.view(MODAL_CALLBACK_COMPLETE_TASK)
    def _on_complete_task_submit(body, ack, view):
        handle_complete_task_submit(body=body, view=view, sender=sender, ack=ack)

    @app.action(ACTION_EDIT_TASK)
    def _on_task_edit_open(body, client, ack):
        handle_task_edit_open(body=body, client=client, sender=sender, ack=ack)

    @app.view(MODAL_CALLBACK_EDIT_TASK)
    def _on_task_edit_submit(body, ack, view):
        handle_task_edit_submit(body=body, view=view, sender=sender, ack=ack)

    @app.action(ACTION_CANCEL_TASK)
    def _on_cancel_task(body, ack):
        handle_cancel_task(body=body, sender=sender, ack=ack)

    @app.action(ACTION_DELETE_TASK)
    def _on_delete_task_open(body, client, ack):
        handle_delete_task_open(body=body, client=client, sender=sender, ack=ack)

    @app.view(MODAL_CALLBACK_DELETE_TASK)
    def _on_delete_task_submit(body, ack, view):
        handle_delete_task_submit(body=body, view=view, sender=sender, ack=ack)

    @app.action(ACTION_SUBSCRIBE)
    def _on_subscribe(body, client, ack):
        handle_subscribe(body=body, sender=sender, client=client, ack=ack)

    @app.action(ACTION_UNSUBSCRIBE)
    def _on_unsubscribe(body, client, ack):
        handle_unsubscribe(body=body, sender=sender, client=client, ack=ack)

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

    # CR-03 admin review
    @app.action(ACTION_ADMIN_CONFIRM_TASK)
    def _on_admin_confirm(body, ack):
        handle_admin_confirm(body=body, sender=sender, ack=ack)

    @app.action(ACTION_ADMIN_REJECT_TASK)
    def _on_admin_reject(body, ack):
        handle_admin_reject(body=body, sender=sender, ack=ack)

    @app.action(ACTION_ADMIN_EDIT_TASK)
    def _on_admin_edit_open(body, client, ack):
        handle_admin_edit_open(body=body, client=client, sender=sender, ack=ack)

    @app.view(MODAL_CALLBACK_ADMIN_EDIT)
    def _on_admin_edit_submit(body, ack, view):
        handle_admin_edit_submit(body=body, view=view, sender=sender, ack=ack)

    # CR-03 weekly plan
    @app.action(ACTION_WEEKLY_ACCEPT)
    def _on_weekly_accept(body, ack):
        handle_weekly_accept(body=body, sender=sender, ack=ack)

    @app.action(ACTION_WEEKLY_DEFER)
    def _on_weekly_defer(body, ack):
        handle_weekly_defer(body=body, sender=sender, ack=ack)

    # CR-04 daily plan
    @app.action(ACTION_PLAN_SKIP)
    def _on_plan_skip(body, ack):
        handle_plan_skip(body=body, sender=sender, ack=ack)

    @app.action(ACTION_PLAN_APPROVE)
    def _on_plan_approve(body, ack):
        handle_plan_approve(body=body, sender=sender, ack=ack)

    # Keep the Employees directory fresh as workspace membership
    # changes — these events let us learn about people without
    # waiting for them to post.
    employees = services.employees

    @app.event("team_join")
    def _on_team_join(event, ack):
        ack()
        if employees is None:
            return
        user = (event or {}).get("user") or {}
        slack_user_id = user.get("id")
        if not slack_user_id:
            return
        from app.db import session_scope

        with session_scope() as session:
            employees.observed(session, slack_user_id=slack_user_id)

    @app.event("member_joined_channel")
    def _on_member_joined_channel(event, ack, context):
        ack()
        if employees is None:
            return
        slack_user_id = (event or {}).get("user")
        channel = (event or {}).get("channel")
        if not slack_user_id or not channel:
            return
        from app.db import session_scope

        with session_scope() as session:
            # If the bot itself just joined, pull the entire channel
            # roster so we know who's in the room from minute one.
            if slack_user_id == context.bot_user_id:
                employees.sync_channel_members(session, channel_id=channel)
            else:
                employees.observed(session, slack_user_id=slack_user_id)

    # FR-CB2-200 — CEO Brain Bot rides on the same App instance.
    # Registers its own `message` / `app_mention` handlers; both
    # the task bot and CEO Brain see every event. The call is a
    # no-op when CEO_BRAIN_ENABLED=false.
    try:
        from app.ceo_brain.slack_handler import register_ceo_brain_handlers

        register_ceo_brain_handlers(app, settings=settings)
    except Exception as e:  # noqa: BLE001
        # Don't take the task bot down if CEO Brain wire-up fails.
        from app.logging_setup import get_logger as _glog
        _glog(__name__).warning(
            "ceo_brain_wire_up_failed", error=str(e),
        )

    return app


def run_socket_mode(app: App, app_token: str) -> None:
    SocketModeHandler(app, app_token).start()
