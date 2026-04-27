"""Slack event handlers: message.im, message.mpim, message.channels/groups, app_mention."""
from __future__ import annotations

from typing import Any

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ActionDraft, ActionDraftState, Task
from app.schemas.intent import IntentClassification, IntentType, InvocationType
from app.services import parse_reply, pick_next_missing, prompt_for
from app.services.followup import llm_extract_reply_fields
from app.slack_bot import blocks as bk
from app.slack_bot.dedup import claim_event
from app.slack_bot.handlers.shared import (
    Services,
    archive_event,
    classify_and_persist,
    draft_private_metadata,
    fetch_permalink,
    strip_bot_mentions,
)
from app.slack_bot.rate_limiter import RateAwareSlackSender

log = get_logger(__name__)


def _kind_from_channel_type(channel_type: str | None, channel_id: str | None) -> str:
    if channel_type in ("im", "mpim"):
        return channel_type
    if channel_id and channel_id.startswith("G"):
        return "group"
    return "channel"


def _is_ignorable(event: dict[str, Any], bot_user_id: str | None) -> bool:
    subtype = event.get("subtype")
    if subtype in ("message_changed", "message_deleted", "bot_message", "channel_join"):
        return True
    if event.get("bot_id") and event.get("user") == bot_user_id:
        return True
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    text = event.get("text") or ""
    # Text-less events are usually system noise (joins, channel changes),
    # EXCEPT voice notes which arrive as a message with empty text and
    # an audio file attached. Keep those.
    if not text.strip():
        from app.services.transcription import extract_audio_files

        if not extract_audio_files(event):
            return True
    # Slack fires both `app_mention` and `message.*` for messages that mention
    # the bot. The explicit mention handler already takes care of those — skip
    # them here to avoid duplicate drafts and races on shared rows.
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return True
    return False


def _classification_from_draft(draft: ActionDraft):
    """Rebuild an IntentClassification from the persisted draft row so we
    can feed it back into Block-Kit builders."""
    from app.schemas.intent import (
        IntentClassification,
        IntentType,
        MeetingDraft,
        TaskDraft,
    )

    intent_map = {
        "create_task": IntentType.create_task,
        "update_task": IntentType.update_task,
        "create_meeting": IntentType.create_meeting,
        "update_meeting": IntentType.update_meeting,
    }
    intent = intent_map.get(draft.intent.value, IntentType.no_action)
    payload = draft.payload or {}
    if intent in (IntentType.create_task, IntentType.update_task):
        return IntentClassification(
            intent=intent,
            confidence=0.9,
            task=TaskDraft.model_validate(payload),
        )
    if intent in (IntentType.create_meeting, IntentType.update_meeting):
        return IntentClassification(
            intent=intent,
            confidence=0.9,
            meeting=MeetingDraft.model_validate(payload),
        )
    return IntentClassification(intent=intent, confidence=0.9)


def _draft_missing_fields(classification) -> list[str]:
    return _missing_fields(classification)


def _build_card_from_payload(draft: ActionDraft) -> list[dict[str, Any]]:
    """Rebuild the draft card from the current payload on the draft row.

    After the user's follow-up fills a field, we chat.update the card so it
    reflects the new data.
    """
    classification = _classification_from_draft(draft)
    return bk.draft_card(
        classification=classification,
        draft_id=draft.id,
        confidence_bucket="high",
        missing_fields=_draft_missing_fields(classification),
    )


def _handle_followup_reply(
    *,
    session,
    thread_ts: str,
    user_id: str | None,
    reply_text: str,
    sender: RateAwareSlackSender,
    client: WebClient,
    services: "Services",
) -> bool:
    """Try to interpret a thread reply as the answer to a pending follow-up.

    Returns True if it was consumed as a follow-up (caller should stop), or
    False if it should fall through to the regular passive path.
    """
    # Draft is awaiting a field reply — either still in the old
    # proposed-waiting-for-Confirm state, OR already confirmed into a
    # real Task (CR-03 always-create flow). We match either case.
    draft = (
        session.query(ActionDraft)
        .filter(
            ActionDraft.slack_message_ts == thread_ts,
            ActionDraft.awaiting_field.isnot(None),
            ActionDraft.state.in_(
                (ActionDraftState.proposed, ActionDraftState.confirmed)
            ),
        )
        .order_by(ActionDraft.id.desc())
        .first()
    )
    if draft is None:
        return False

    field = draft.awaiting_field
    settings = get_settings()

    # First try the LLM multi-field extractor so a single reply like
    # "на пашу до завтра" fills both owner AND due_date.
    backend = getattr(services.classifier, "backend", None)
    parsed: dict[str, Any] = {}
    if backend is not None:
        parsed = llm_extract_reply_fields(
            backend=backend,
            reply_text=reply_text,
            awaiting_field=field,
            allowed_owners=settings.allowed_owners(),
        )

    # Fallback / augmentation: run the deterministic per-field parser for
    # the currently-awaited field if the LLM didn't give us a value for it.
    per_field = parse_reply(
        field=field,
        reply_text=reply_text,
        settings=settings,
    )
    if per_field:
        for k, v in per_field.items():
            parsed.setdefault(k, v)

    if not parsed:
        resp = sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=(
                f":question: Не распарсил ответ. "
                f"{prompt_for(field, payload=draft.payload or {}, allowed_owners=settings.allowed_owners())} "
                "Или нажми *Edit* на карточке."
            ),
        )
        _record_followup_ts(session, draft, resp)
        return True

    # Merge into draft payload.
    payload = dict(draft.payload or {})
    payload.update(parsed)
    draft.payload = payload
    flag_modified(draft, "payload")

    # If the draft already materialised into a Task (CR-03 always-create
    # path — typical for @mention and high-confidence passive), apply the
    # same patch to the Task row and refresh both cards. Otherwise update
    # the still-pending draft widget as before.
    task = None
    if draft.task_id is not None:
        task = session.get(Task, draft.task_id)
        if task is not None:
            _apply_payload_to_task(task, parsed)

    # Decide what to ask next based on whatever is authoritative.
    if task is not None:
        next_field = pick_next_missing(draft.intent.value, _task_payload(task))
    else:
        next_field = pick_next_missing(draft.intent.value, payload)
    draft.awaiting_field = next_field
    session.flush()

    # Update the card in place.
    if task is not None:
        from app.services.card_sync import refresh_task_card as _rtc

        _rtc(sender, task)
    elif draft.card_channel and draft.card_ts:
        try:
            sender.update_message(
                channel=draft.card_channel,
                ts=draft.card_ts,
                blocks=_build_card_from_payload(draft),
                text="Action draft",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("card_update_failed", error=str(e), draft_id=draft.id)

    # Tidy: delete the bot's previous follow-up question(s) so the
    # thread stays clean — just the source message, the task/draft
    # card, the user's answer, and the new ack.
    _delete_prior_followups(sender, draft)

    # Ack in thread — plus next question if there is one.
    prompt_payload = _task_payload(task) if task is not None else payload
    if next_field:
        resp = sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=(
                ":ok_hand: Записал. "
                f"{prompt_for(next_field, payload=prompt_payload, allowed_owners=settings.allowed_owners())}"
            ),
        )
    elif task is not None:
        resp = sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=":white_check_mark: Всё заполнил. Задача обновлена.",
        )
    else:
        resp = sender.post_message(
            channel=draft.card_channel or "",
            thread_ts=thread_ts,
            text=":white_check_mark: Все поля собрал. Жми *Accept* на карточке.",
        )
    _record_followup_ts(session, draft, resp)
    return True


def _apply_payload_to_task(task: Task, patch: dict) -> None:
    """Apply parsed follow-up fields to an already-created Task row."""
    from datetime import date as _date
    from datetime import datetime as _dt

    for field, value in patch.items():
        if value in (None, "", []):
            continue
        if field == "title":
            task.title = value
        elif field == "description":
            task.description = value
        elif field == "owner_user_id":
            task.owner_user_id = value
            # An explicit assignment clears the 'assumed' label.
            extra = dict(task.extra or {})
            if extra.pop("owner_assumed", None) is not None:
                task.extra = extra or None
        elif field == "owner_display_name":
            task.owner_display_name = value
        elif field == "priority":
            from app.models.task import TaskPriority

            try:
                task.priority = TaskPriority(value)
            except (ValueError, TypeError):
                pass
        elif field == "due_date":
            if isinstance(value, str):
                try:
                    task.due_date = _date.fromisoformat(value)
                except ValueError:
                    pass
            elif isinstance(value, _date):
                task.due_date = value
        elif field == "datetime_at":
            # Meetings (not typical for task follow-ups); leave a hook for
            # future intent extensions.
            pass
        elif field == "participants":
            pass
        elif field == "estimated_minutes":
            try:
                task.estimated_minutes = int(value)
            except (ValueError, TypeError):
                pass


def _record_followup_ts(session, draft: ActionDraft, resp: Any) -> None:
    """Append the ts of a bot-posted follow-up so we can delete it later."""
    ts: str | None = None
    if isinstance(resp, dict):
        ts = resp.get("ts")
    if not ts:
        return
    current = list(draft.follow_up_message_ts or [])
    current.append(ts)
    draft.follow_up_message_ts = current
    flag_modified(draft, "follow_up_message_ts")
    session.flush()


def _delete_prior_followups(sender, draft: ActionDraft) -> None:
    """Delete every bot follow-up message previously tracked on the
    draft. Called right before we post a new ack so the thread shows
    only the latest exchange, identical on @mention and passive paths.
    """
    channel = draft.card_channel
    tss = list(draft.follow_up_message_ts or [])
    if not channel or not tss or not hasattr(sender, "delete_message"):
        return
    for ts in tss:
        try:
            sender.delete_message(channel=channel, ts=ts)
        except Exception as e:  # noqa: BLE001
            log.warning("followup_cleanup_delete_failed", ts=ts, error=str(e))
    draft.follow_up_message_ts = []
    flag_modified(draft, "follow_up_message_ts")


def _missing_fields(classification: IntentClassification) -> list[str]:
    """Return required fields that are still empty — for inline prompts."""
    missing: list[str] = []
    if classification.intent in (IntentType.create_task, IntentType.update_task):
        t = classification.task
        if t is None or not t.title:
            missing.append("title")
        if t is None or not t.owner_display_name and not t.owner_user_id:
            missing.append("owner")
        if t is None or t.due_date is None:
            missing.append("due date")
    elif classification.intent in (IntentType.create_meeting, IntentType.update_meeting):
        m = classification.meeting
        if m is None or not m.title:
            missing.append("title")
        if m is None or not m.participants:
            missing.append("participants")
        if m is None or m.datetime_at is None:
            missing.append("date/time")
    return missing


def handle_message(
    *,
    event: dict[str, Any],
    body: dict[str, Any],
    client: WebClient,
    context: BoltContext,
    services: Services,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    """Handle message.im / message.mpim / message.channels events (passive mode)."""
    ack()

    event_id = body.get("event_id") or ""
    bot_user_id = context.bot_user_id

    # Archive every event verbatim BEFORE filtering so the audit trail
    # captures edits, deletes, system subtypes too. Best-effort — a
    # failure here must not abort the handler.
    try:
        with session_scope() as session:
            archive_event(session, event=event, body=body)
    except Exception as e:  # noqa: BLE001
        log.warning("archive_event_failed", error=str(e))

    if _is_ignorable(event, bot_user_id):
        return

    channel = event.get("channel")
    if not channel:
        return

    with session_scope() as session:
        if not claim_event(session, event_id):
            log.info("duplicate_event_skipped", event_id=event_id)
            return

        # If this is a reply inside a thread where we have a pending draft
        # awaiting the user's answer, route it into the follow-up flow.
        thread_ts = event.get("thread_ts")
        reply_text = strip_bot_mentions(event.get("text", ""), bot_user_id)
        if thread_ts and reply_text and event.get("user") != bot_user_id:
            if _handle_followup_reply(
                session=session,
                thread_ts=thread_ts,
                user_id=event.get("user"),
                reply_text=reply_text,
                sender=sender,
                client=client,
                services=services,
            ):
                return

        kind = _kind_from_channel_type(event.get("channel_type"), channel)
        text, transcripts = _enrich_text_with_audio(event, reply_text)
        transcript_blob = "\n".join(t for t in transcripts if t) or None
        has_audio = bool(transcripts) or any(
            (f.get("mimetype") or "").startswith("audio/")
            for f in (event.get("files") or [])
            if isinstance(f, dict)
        )

        source_message = {
            "ts": event["ts"],
            "thread_ts": thread_ts,
            "user": event.get("user"),
            "subtype": event.get("subtype"),
            "text": text,
        }

        classification, draft, snapshot = classify_and_persist(
            session,
            services=services,
            conversation_id=channel,
            kind=kind,
            source_message=source_message,
            invocation_type=InvocationType.passive,
            slack_user_id=event.get("user"),
            raw_event=event,
            transcript=transcript_blob,
            has_audio=has_audio,
        )

        decision = services.orchestrator.decide_passive(
            classification=classification, draft_id=draft.id if draft else None
        )

        if decision.action == "silent" or draft is None:
            return

        # Snapshot everything we need outside the session BEFORE closing —
        # downstream calls open their own session_scope() and can't see
        # uncommitted rows otherwise (async finalize used to blow up with
        # "Draft N not found").
        permalink = fetch_permalink(client, channel=channel, ts=event["ts"])
        draft_id = draft.id
        snapshot_id = snapshot.id
        # Snapshot a fresh IntentClassification rebuild for the draft card.
        draft_classification = _classification_from_draft(draft)

    # ── outside session_scope: the draft row is now committed ────────────
    # Passive path only offers — never auto-creates (product decision
    # 2026-04-24). Auto-create is reserved for @mention.
    metadata = draft_private_metadata(
        conversation_id=channel,
        message_ts=event["ts"],
        thread_ts=event.get("thread_ts"),
        draft_id=draft_id,
        context_snapshot_id=snapshot_id,
        source_user_id=event.get("user"),
        permalink=permalink,
    )
    missing = _draft_missing_fields(draft_classification)
    resp = sender.post_message(
        channel=channel,
        thread_ts=event.get("thread_ts") or event["ts"],
        blocks=bk.draft_card(
            classification=draft_classification,
            draft_id=draft_id,
            confidence_bucket="medium",
            missing_fields=missing,
        ),
        text="Task draft — Accept / Edit / Reject",
        metadata={"event_type": "draft", "event_payload": {"metadata": metadata}},
    )
    # Remember where the widget lives so finalize_draft can morph it into
    # a task card in place when the user clicks Accept.
    card_ts = None
    if isinstance(resp, dict):
        card_ts = resp.get("ts")
    with session_scope() as session:
        draft = session.get(ActionDraft, draft_id)
        if draft is None:
            return
        if card_ts:
            draft.card_channel = channel
            draft.card_ts = card_ts
        # Ask for the first missing field in the thread so the user can
        # answer in chat without opening the Edit modal. The reply will
        # be consumed by _handle_followup_reply, which updates the
        # draft.payload and refreshes the card in place.
        from app.services import pick_next_missing, prompt_for
        from app.config import get_settings

        next_field = pick_next_missing(
            draft.intent.value, draft.payload or {}
        )
        draft.awaiting_field = next_field
        session.flush()
        if next_field:
            title_preview = (draft.payload or {}).get("title") or "задачу"
            intro = (
                f":memo: Записал: *{title_preview}*.\n"
                + prompt_for(
                    next_field,
                    payload=draft.payload or {},
                    allowed_owners=get_settings().allowed_owners(),
                )
            )
            try:
                fu_resp = sender.post_message(
                    channel=channel,
                    thread_ts=event.get("thread_ts") or event["ts"],
                    text=intro,
                )
                _record_followup_ts(session, draft, fu_resp)
            except Exception as e:  # noqa: BLE001
                log.warning("passive_followup_post_failed", error=str(e))


def _always_create_and_admin_review(
    *,
    draft_id: int,
    source_conversation_id: str,
    source_message_ts: str,
    source_thread_ts: str | None,
    source_user_id: str | None,
    context_snapshot_id: int | None,
    permalink: str | None,
    reasoning: str | None,
    sender: RateAwareSlackSender,
) -> None:
    """CR-03 always-create: finalize the draft into a real Task (status
    backlog / todo) and broadcast the admin review card."""
    from app.config import get_settings
    from app.models import Task
    from app.orchestrator.finalize import FinalizeService
    from app.services import post_admin_review
    from app.services.employees import admin_slack_user_ids

    settings = get_settings()
    fin = FinalizeService(settings=settings, sender=sender)
    source_metadata = {
        "conversation_id": source_conversation_id,
        "message_ts": source_message_ts,
        "thread_ts": source_thread_ts,
        "permalink": permalink,
        "context_snapshot_id": context_snapshot_id,
        "source_user_id": source_user_id,
    }
    try:
        entity_type, entity_id, _ = fin.finalize_draft(
            draft_id=draft_id, source_metadata=source_metadata
        )
    except Exception as e:  # noqa: BLE001
        log.error("always_create_finalize_failed", error=str(e), draft_id=draft_id)
        return

    if entity_type != "task":
        return

    with session_scope() as session:
        task = session.get(Task, entity_id)
        if task is None:
            return
        post_admin_review(
            session,
            task=task,
            sender=sender,
            source_channel=source_conversation_id,
            source_thread_ts=source_thread_ts or source_message_ts,
            source_permalink=permalink,
            reasoning=reasoning,
            admins=admin_slack_user_ids(settings),
        )


def handle_app_mention(
    *,
    event: dict[str, Any],
    body: dict[str, Any],
    client: WebClient,
    context: BoltContext,
    services: Services,
    sender: RateAwareSlackSender,
    ack: Ack,
) -> None:
    """Explicit @mention flow: always return a user-visible reply.

    Contract: the bot MUST post something back to the channel for every
    mention. Either a draft card (with a "missing fields" hint when relevant)
    or an error/fallback message — never silence.
    """
    ack()

    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    def _reply(text: str) -> None:
        if channel:
            try:
                sender.post_message(channel=channel, thread_ts=thread_ts, text=text)
            except Exception as e:  # noqa: BLE001
                log.error("mention_fallback_reply_failed", error=str(e))

    if not channel:
        log.warning("mention_event_without_channel")
        return

    event_id = body.get("event_id") or ""
    bot_user_id = context.bot_user_id

    # Verbatim audit row, even if dedup later short-circuits. Best-effort.
    try:
        with session_scope() as session:
            archive_event(session, event=event, body=body)
    except Exception as e:  # noqa: BLE001
        log.warning("archive_event_failed", error=str(e))

    try:
        with session_scope() as session:
            if not claim_event(session, event_id):
                log.info("duplicate_event_skipped", event_id=event_id)
                return

            text, transcripts = _enrich_text_with_audio(
                event, strip_bot_mentions(event.get("text", ""), bot_user_id)
            )
            transcript_blob = "\n".join(t for t in transcripts if t) or None
            has_audio = bool(transcripts) or any(
                (f.get("mimetype") or "").startswith("audio/")
                for f in (event.get("files") or [])
                if isinstance(f, dict)
            )
            source_message = {
                "ts": event["ts"],
                "thread_ts": event.get("thread_ts"),
                "user": event.get("user"),
                "subtype": event.get("subtype"),
                "text": text,
            }

            classification, draft, snapshot = classify_and_persist(
                session,
                services=services,
                conversation_id=channel,
                kind=_kind_from_channel_type(event.get("channel_type"), channel),
                source_message=source_message,
                invocation_type=InvocationType.mention,
                slack_user_id=event.get("user"),
                raw_event=event,
                transcript=transcript_blob,
                has_audio=has_audio,
            )

            permalink = fetch_permalink(client, channel=channel, ts=event["ts"])

            if draft is None:
                # Fallback: an explicit @mention is always a "do something with
                # this text" signal. Even when the LLM couldn't tease out a
                # clean structure, synthesise a minimal create_task draft from
                # the cleaned source text so the user gets a widget + follow-up
                # questions (instead of a "не понял" dead end).
                if not text.strip():
                    _reply(
                        ":thinking_face: Я не вижу текста в твоём сообщении. "
                        "Напиши рядом с @bot, что нужно сделать."
                    )
                    return

                from app.intent.date_resolver import resolve_due_date
                from app.schemas.intent import (
                    IntentClassification,
                    IntentType,
                    TaskDraft,
                )

                # The resolver still owns dates in the fallback path: an
                # LLM that couldn't classify the message shouldn't cost us
                # the obvious "к пятнице"/"ко вторнику" information.
                from datetime import date as _date

                fallback_due = resolve_due_date(text, _date.today())
                fallback = IntentClassification(
                    intent=IntentType.create_task,
                    confidence=0.6,
                    task=TaskDraft(title=text[:200], due_date=fallback_due),
                    reasoning="fallback: explicit mention without extractable structure",
                )
                inference = services.orchestrator.persist_inference(
                    session,
                    context_snapshot=snapshot,
                    classification=fallback,
                    invocation_type=InvocationType.mention,
                )
                draft = services.orchestrator.create_draft(
                    session,
                    inference=inference,
                    classification=fallback,
                    created_by_slack_user_id=event.get("user"),
                    slack_message_ts=event["ts"],
                )
                classification = fallback

            # Remember where the source lives so follow-up replies can edit
            # the task and the card in place.
            draft.card_channel = channel
            session.flush()
            draft_id = draft.id
            snap_id = snapshot.id
            user_id = event.get("user")
            msg_ts = event["ts"]
            ev_thread_ts = event.get("thread_ts")
            classification_intent_value = classification.intent.value

        # Materialise the task immediately — @mention is an explicit signal,
        # no admin review needed (CR-03 split: admin review is reserved for
        # the background/passive path).
        _auto_finalize(
            draft_id=draft_id,
            source_conversation_id=channel,
            source_message_ts=msg_ts,
            source_thread_ts=ev_thread_ts,
            source_user_id=user_id,
            context_snapshot_id=snap_id,
            permalink=permalink,
            sender=sender,
        )

        # Ask for the first missing field (if any) so the user can answer
        # in the thread. The reply handler will update the Task + refresh
        # the task-card in place.
        with session_scope() as session:
            draft = session.get(ActionDraft, draft_id)
            if draft is None:
                return
            task = session.get(Task, draft.task_id) if draft.task_id else None
            next_field = _next_missing_for_task(
                task, intent=classification_intent_value
            )
            draft.awaiting_field = next_field
            session.flush()

            if next_field:
                title_preview = (task.title if task else "задачу") or "задачу"
                intro = (
                    f":memo: Записал: *{title_preview}*.\n"
                    + prompt_for(
                        next_field,
                        payload=_task_payload(task),
                        allowed_owners=get_settings().allowed_owners(),
                    )
                )
                resp = sender.post_message(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=intro,
                )
                _record_followup_ts(session, draft, resp)
    except Exception as e:  # noqa: BLE001 — mention must never go silent
        log.exception("mention_handler_failed", error=str(e))
        _reply(f":warning: что-то сломалось при обработке: `{e!s}` — посмотри логи бота.")


def _auto_finalize(
    *,
    draft_id: int,
    source_conversation_id: str,
    source_message_ts: str,
    source_thread_ts: str | None,
    source_user_id: str | None,
    context_snapshot_id: int | None,
    permalink: str | None,
    sender: RateAwareSlackSender,
) -> int | None:
    """Finalize a draft into a Task without posting admin review."""
    from app.config import get_settings as _gs
    from app.orchestrator.finalize import FinalizeService

    settings = _gs()
    fin = FinalizeService(settings=settings, sender=sender)
    source_metadata = {
        "conversation_id": source_conversation_id,
        "message_ts": source_message_ts,
        "thread_ts": source_thread_ts,
        "permalink": permalink,
        "context_snapshot_id": context_snapshot_id,
        "source_user_id": source_user_id,
    }
    try:
        entity_type, entity_id, _ = fin.finalize_draft(
            draft_id=draft_id, source_metadata=source_metadata
        )
    except Exception as e:  # noqa: BLE001
        log.error("auto_finalize_failed", error=str(e), draft_id=draft_id)
        return None
    return entity_id if entity_type == "task" else None


def _enrich_text_with_audio(
    event: dict[str, Any], base_text: str
) -> tuple[str, list[str]]:
    """If the event carries audio attachments, transcribe them and
    merge the transcripts into the text the pipeline will see.

    Returns a (text, transcripts) tuple. The transcripts list is the
    raw per-file output (joined separately so the caller can persist
    it on slack_messages.transcript). Returns (base_text, []) when
    there are no audio files, Whisper is not configured, or
    transcription fails.
    """
    from app.services.transcription import (
        extract_audio_files,
        merge_transcripts_into_text,
        transcribe_audio_files,
    )

    audio_files = extract_audio_files(event)
    if not audio_files:
        return base_text, []

    settings = get_settings()
    transcripts = transcribe_audio_files(
        audio_files,
        bot_token=settings.slack_bot_token,
        openai_api_key=settings.openai_api_key,
    )
    if not transcripts:
        return base_text, []
    return merge_transcripts_into_text(base_text, transcripts), transcripts


def _task_payload(task: Task | None) -> dict:
    if task is None:
        return {}
    # The owner_assumed flag signals that owner_user_id was a fallback to
    # the source-message author — not a real assignment. Expose it so the
    # follow-up machinery keeps asking "кому назначаем?" until the human
    # answers. pick_next_missing treats assumed owners as empty.
    return {
        "title": task.title,
        "description": task.description,
        "owner_user_id": task.owner_user_id,
        "owner_display_name": task.owner_display_name,
        "owner_assumed": bool((task.extra or {}).get("owner_assumed")),
        "priority": task.priority.value if task.priority else None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
    }


def _next_missing_for_task(task: Task | None, *, intent: str) -> str | None:
    if task is None:
        return None
    return pick_next_missing(intent, _task_payload(task))
