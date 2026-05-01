"""FR-CR-05-116 — Zoom cloud-recording pipeline.

Mirror of `app/fireflies/pipeline.py` for Zoom. Reuses the
shared helpers (`_split_audio_into_chunks`, `_truncate`,
`_render_known_employees_table`, `_admin_fallback_owner_id`)
from the Fireflies module so the two pipelines stay
behaviour-identical.

Steps (same as Fireflies, FR-CR-05-39 / -56 / -115):

  1. Upsert `ZoomRecording` row keyed by Zoom UUID.
  2. Download audio via `ZoomClient.download_audio` (bearer-
     auth'd `download_url` from the recording-files list).
  3. Whisper transcribe — chunked at ≤24 MB via shared
     ffmpeg helper when needed.
  4. Detailed RU summary via `complete_text` + the same
     `MEETING_SUMMARY_PROMPT` Fireflies uses.
  5. Google Doc export with anyone-with-link writer.
  6. Short summary DM'd to admins + Tasks extracted via
     gpt-5.5 (same prompt, source_kind=zoom).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.fireflies.pipeline import (
    _admin_fallback_owner_id,
    _render_known_employees_table,
    _split_audio_into_chunks,
    _truncate,
)
from app.fireflies.prompts import (
    DETAILED_SUMMARY_SYSTEM as MEETING_SUMMARY_PROMPT,
    SHORT_SUMMARY_SYSTEM as MEETING_SHORT_SUMMARY_PROMPT,
    TASK_EXTRACTION_SYSTEM as MEETING_TASKS_PROMPT,
)
from app.logging_setup import get_logger
from app.models import Task, TaskSourceKind, ZoomRecording
from app.models.task import TaskPriority, TaskStatus
from app.zoom.client import ZoomClient, ZoomRecordingMeta

log = get_logger(__name__)


@dataclass
class ZoomPipelineReport:
    recording_id: int | None
    zoom_id: str
    title: str | None
    transcript_chars: int = 0
    detailed_chars: int = 0
    short_chars: int = 0
    google_doc_url: str | None = None
    tasks_created: int = 0
    short_summary_recipients: int = 0
    skipped_reason: str | None = None
    errors: list[str] = None  # type: ignore[assignment]


class ZoomPipeline:
    """Zoom counterpart of `FirefliesPipeline`.

    Constructed once at process startup; ``process_one`` is the
    main entry-point and is safe to call repeatedly on the same
    recording (each step short-circuits when its flag is set).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: ZoomClient,
        llm_backend: Any,
        docs_factory=None,
        sender=None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._llm = llm_backend
        self._docs_factory = docs_factory
        self._sender = sender

    # --- step 0: upsert the row -------------------------------

    def _upsert_recording(
        self, session: Session, m: ZoomRecordingMeta
    ) -> ZoomRecording:
        row = (
            session.query(ZoomRecording)
            .filter(ZoomRecording.zoom_id == m.id)
            .first()
        )
        if row is None:
            row = ZoomRecording(
                zoom_id=m.id,
                zoom_meeting_id=m.meeting_id,
                title=m.title,
                meeting_date=m.meeting_date,
                duration_seconds=m.duration_seconds,
                participants=list(m.participants),
                audio_url=m.audio_url,
                zoom_share_url=m.share_url,
            )
            session.add(row)
            session.flush()
        else:
            if m.title and not row.title:
                row.title = m.title
            if m.meeting_date and not row.meeting_date:
                row.meeting_date = m.meeting_date
            if m.audio_url and not row.audio_url:
                row.audio_url = m.audio_url
            if m.share_url and not row.zoom_share_url:
                row.zoom_share_url = m.share_url
        return row

    # --- step 1: download audio -------------------------------

    def _step_download_audio(self, row: ZoomRecording) -> bool:
        if (
            row.audio_downloaded
            and row.audio_path
            and os.path.exists(row.audio_path)
        ):
            return True
        if not row.audio_url:
            row.last_error = "no audio_url on Zoom record"
            return False
        # FR-CR-05-117 — Zoom UUIDs are base64 with `/` and `=`,
        # which create unwanted subdirs / weird filenames when
        # used directly. Sanitize for the on-disk path.
        safe_id = (row.zoom_id or "").replace("/", "_").replace("=", "")
        # Provisional .bin extension; we sniff magic bytes after
        # download and rename to the real container so Whisper +
        # ffmpeg get the right hint. Zoom's URL never contains
        # the file extension, so we can't decide it upfront.
        dest = os.path.join(
            self._settings.zoom_audio_dir, f"{safe_id}.bin"
        )
        size = self._client.download_audio(
            url=row.audio_url,
            dest_path=dest,
            max_bytes=self._settings.zoom_audio_max_bytes,
        )
        if size is None:
            row.last_error = "audio download failed or exceeded cap"
            return False
        # Detect the real container from magic bytes and rename.
        from app.fireflies.pipeline import _sniff_audio_extension

        ext = _sniff_audio_extension(dest) or "mp4"
        final = os.path.join(
            self._settings.zoom_audio_dir, f"{safe_id}.{ext}"
        )
        if final != dest:
            os.replace(dest, final)
        row.audio_path = final
        row.audio_downloaded = True
        row.last_error = None
        return True

    # --- step 2: Whisper transcribe (with FR-CR-05-115 chunking)

    def _step_transcribe(self, row: ZoomRecording) -> bool:
        if row.transcribed and row.transcript_text:
            return True
        if not row.audio_path or not os.path.exists(row.audio_path):
            row.last_error = "audio_path missing for transcription"
            return False
        api_key = self._settings.openai_api_key
        if not api_key:
            row.last_error = "OPENAI_API_KEY not set"
            return False
        from app.services.transcription import transcribe_bytes

        size = os.path.getsize(row.audio_path)
        whisper_max = 24 * 1024 * 1024
        if size <= whisper_max:
            audio_paths = [row.audio_path]
        else:
            try:
                audio_paths = _split_audio_into_chunks(
                    row.audio_path, max_bytes=whisper_max
                )
            except Exception as e:  # noqa: BLE001
                row.last_error = f"audio chunking failed: {e}"
                return False
            log.info(
                "zoom_audio_chunked_for_whisper",
                zoom_id=row.zoom_id,
                size=size,
                chunks=len(audio_paths),
            )
        transcript_parts: list[str] = []
        for i, p in enumerate(audio_paths):
            try:
                with open(p, "rb") as f:
                    audio_bytes = f.read()
            except OSError as e:
                row.last_error = f"audio chunk read failed [{i}]: {e}"
                return False
            mimetype = (
                "audio/mp4" if p.lower().endswith(".m4a") else
                ("video/mp4" if p.lower().endswith(".mp4") else "audio/mpeg")
            )
            chunk_text = transcribe_bytes(
                audio_bytes=audio_bytes,
                mimetype=mimetype,
                filename=os.path.basename(p),
                openai_api_key=api_key,
                model=self._settings.fireflies_whisper_model,
            )
            if not chunk_text:
                row.last_error = (
                    f"Whisper returned empty transcript on chunk "
                    f"{i+1}/{len(audio_paths)}"
                )
                return False
            transcript_parts.append(chunk_text)
        transcript = "\n".join(transcript_parts).strip()
        if not transcript:
            row.last_error = "Whisper returned empty transcript"
            return False
        if len(audio_paths) > 1:
            for p in audio_paths:
                if p != row.audio_path:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        row.transcript_text = transcript
        row.transcribed = True
        row.last_error = None
        return True

    # --- step 3: detailed summary -----------------------------

    def _step_detailed_summary(self, row: ZoomRecording) -> bool:
        if row.detailed_summarised and row.detailed_summary:
            return True
        if not row.transcript_text:
            row.last_error = "no transcript for detailed summary"
            return False
        meta_lines = [
            f"Заголовок: {row.title or '(без названия)'}",
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}",
            (
                f"Продолжительность: {row.duration_seconds // 60} мин"
                if row.duration_seconds
                else "Продолжительность: —"
            ),
        ]
        if row.participants:
            meta_lines.append("Участники: " + ", ".join(row.participants))
        user_prompt = (
            "\n".join(meta_lines) + "\n\nТранскрипт:\n" + row.transcript_text
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=MEETING_SUMMARY_PROMPT,
                user_prompt=user_prompt,
                model=self._settings.fireflies_summary_model,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"detailed summary failed: {e}"
            return False
        if not text:
            row.last_error = "detailed summary returned empty"
            return False
        from app.fireflies.pipeline import _strip_markdown_emphasis

        row.detailed_summary = _strip_markdown_emphasis(text)
        row.detailed_summarised = True
        row.last_error = None
        return True

    # --- step 4: Google Doc export ----------------------------

    def _step_doc_export(
        self, session: Session, row: ZoomRecording
    ) -> bool:
        if row.doc_exported and row.google_doc_url:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary to export"
            return False
        if self._docs_factory is None:
            row.last_error = "Docs factory not configured"
            return False
        try:
            docs = self._docs_factory()
        except Exception as e:  # noqa: BLE001
            row.last_error = f"Docs factory failed: {e}"
            return False
        if docs is None:
            row.last_error = "Docs credentials unavailable"
            return False
        title = row.title or f"Zoom meeting {row.zoom_id}"
        # FR-CR-05-119 follow-up — append the full task list to
        # the doc body. Pipeline order is detailed → tasks → doc
        # so by here the Task rows already exist.
        from app.fireflies.pipeline import (
            _build_full_tasks_section_for_doc,
        )

        body = row.detailed_summary
        tasks_section = _build_full_tasks_section_for_doc(
            session,
            source_kind=TaskSourceKind.zoom,
            source_conversation_id=row.zoom_id,
        )
        if tasks_section:
            body = body.rstrip() + "\n\n" + tasks_section
        try:
            doc_id, url = docs.export_summary(
                title=title,
                body=body,
                parent_folder_id=(
                    self._settings.zoom_docs_folder_id
                    or self._settings.fireflies_docs_folder_id
                ),
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"Docs export failed: {e}"
            return False
        row.google_doc_id = doc_id
        row.google_doc_url = url
        row.doc_exported = True
        row.last_error = None
        return True

    # --- step 5: short summary + Telegram -----------------------

    def _step_short_summary(
        self, session: Session, row: ZoomRecording
    ) -> bool:
        if row.short_summary_sent and row.short_summary:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary for short summary"
            return False
        participants_block = "\n".join(
            f"  - {p}" for p in (row.participants or []) if p
        ) or "  (нет данных)"
        meta_line = (
            f"meeting_title: {row.title or ''}\n"
            f"meeting_date: {row.meeting_date.isoformat() if row.meeting_date else ''}\n"
            f"duration_min: {row.duration_seconds // 60 if row.duration_seconds else ''}\n"
            f"google_doc_url: {row.google_doc_url or ''}\n"
            f"\nparticipants:\n{participants_block}\n\n"
        )
        body_in = meta_line + "Подробный отчёт:\n" + row.detailed_summary
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=MEETING_SHORT_SUMMARY_PROMPT,
                user_prompt=body_in,
                model=self._settings.fireflies_short_summary_model,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"short summary failed: {e}"
            return False
        text = _truncate(text, limit=3800)
        if not text:
            row.last_error = "short summary returned empty"
            return False
        # FR-CR-05-119 — drop any LLM-emitted To-Do section so we
        # can append the deterministic one. Also strip markdown.
        from app.fireflies.pipeline import (
            _build_todo_section,
            _strip_llm_todo_block,
        )

        text = _strip_llm_todo_block(text)
        # FR-CR-05-119 — append To-Do from the actual extracted
        # Task rows so the TG message matches what the operator
        # has in the DB / Sheet / DM cards.
        todo = _build_todo_section(
            session,
            source_kind=TaskSourceKind.zoom,
            source_conversation_id=row.zoom_id,
        )
        if todo:
            text = text.rstrip() + "\n\n" + todo
        # FR-CR-05-117 — Google Doc trailer (deterministic).
        if row.google_doc_url:
            text = (
                text.rstrip()
                + "\n\n📄 Подробный отчёт: "
                + row.google_doc_url
            )
        row.short_summary = text
        row.last_error = None
        if self._sender is not None:
            self._send_short_summary(row)
        else:
            row.short_summary_sent = True
        return True

    def _send_short_summary(self, row: ZoomRecording) -> int:
        """DM the short summary to every admin uid. FR-CR-05-119:
        the deterministic To-Do block can push the body past
        Telegram's 4096-char per-message limit, so we split into
        chunks at paragraph boundaries and send each as a
        separate DM. Returns the number of admins who received
        the FULL set of chunks."""
        from app.fireflies.pipeline import _split_for_telegram
        from app.telegram_bot.handlers import admin_user_ids

        chunks = _split_for_telegram(row.short_summary or "", limit=3800)
        if not chunks:
            return 0
        sent = 0
        for uid in sorted(admin_user_ids()):
            if not uid.lstrip("-").isdigit():
                continue
            uid_chunks = 0
            for chunk in chunks:
                try:
                    resp = self._sender.send_message(
                        chat_id=int(uid), text=chunk,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "zoom_short_summary_dm_failed",
                        uid=uid, error=str(e),
                    )
                    break
                if (resp or {}).get("message_id"):
                    uid_chunks += 1
                else:
                    break
            if uid_chunks == len(chunks):
                sent += 1
        row.short_summary_sent = sent > 0
        return sent

    # --- step 6: extract tasks --------------------------------

    def _step_extract_tasks(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """Run gpt-5.5 over the detailed summary to extract
        action items, materialise as Task rows with
        source_kind=zoom + source_permalink=share_url. Returns
        the count of new Tasks."""
        if row.tasks_extracted:
            return row.tasks_extracted_count or 0
        if not row.detailed_summary:
            row.last_error = "no detailed summary for task extraction"
            return 0

        # Same as Fireflies: feed known_employees so the LLM
        # routes owner_user_id to a real teammate.
        from app.services.team_members import as_known_employees

        try:
            known_employees = as_known_employees(session)
        except Exception:  # noqa: BLE001
            known_employees = []

        admin_uid = _admin_fallback_owner_id()
        emp_table = _render_known_employees_table(known_employees)
        prompt_user = (
            f"Заголовок: {row.title or '(без названия)'}\n"
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}\n"
            "\nИзвестные сотрудники:\n"
            f"{emp_table}\n\nПодробный отчёт:\n{row.detailed_summary}"
        )
        try:
            data = self._llm.call_tool(  # type: ignore[attr-defined]
                system_prompt=MEETING_TASKS_PROMPT,
                user_prompt=prompt_user,
                tool_name="record_meeting_tasks",
                tool_description="Extract action items from a meeting.",
                tool_parameters={
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "owner": {"type": ["string", "null"]},
                                    "priority": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high", "urgent"],
                                    },
                                },
                                "required": ["title"],
                            },
                        }
                    },
                    "required": ["tasks"],
                },
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
            ) or {}
        except Exception as e:  # noqa: BLE001
            row.last_error = f"task extraction failed: {e}"
            log.warning(
                "zoom_task_extraction_llm_failed",
                zoom_id=row.zoom_id,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=self._settings.fireflies_tasks_reasoning_effort,
                error=str(e),
            )
            return 0
        tasks = (data or {}).get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        # FR-CR-05-120 follow-up — log when we got 0 tasks back
        # so the operator can tell «meeting was procedural, no
        # actions» from «model rejected the call» / «prompt
        # broke». Includes the model name for fast triage.
        if not tasks:
            log.info(
                "zoom_task_extraction_returned_empty",
                zoom_id=row.zoom_id,
                model=self._settings.fireflies_tasks_model,
                detailed_chars=len(row.detailed_summary or ""),
                hint=(
                    "either the meeting was procedural (no "
                    "actionable items) or the LLM call returned "
                    "an empty array — check `last_error` and the "
                    "model name"
                ),
            )
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        created = 0
        from app.persistence.tasks import normalize_task_title

        today = datetime.now(timezone.utc).date()
        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            try:
                title = normalize_task_title(title)
            except ValueError:
                continue
            owner_uid = (t.get("owner") or "").strip() or None
            if owner_uid and owner_uid not in valid_ids:
                owner_uid = None
            if owner_uid is None and admin_uid:
                owner_uid = admin_uid
            try:
                priority = TaskPriority(t.get("priority") or "medium")
            except ValueError:
                priority = TaskPriority.medium
            owner_display_name = None
            if owner_uid and known_employees:
                for e in known_employees:
                    if e.get("slack_user_id") == owner_uid:
                        owner_display_name = (
                            e.get("real_name")
                            or e.get("display_name")
                            or owner_uid
                        )
                        break
            try:
                task = Task(
                    title=title[:10_000],
                    description=(t.get("description") or "").strip() or None,
                    priority=priority,
                    status=TaskStatus.todo,
                    owner_user_id=owner_uid,
                    owner_display_name=owner_display_name,
                    due_date=today,
                    due_time=time(18, 0),  # FR-CR-05-63
                    is_current_week=True,
                    source_kind=TaskSourceKind.zoom,
                    # FR-CR-05-118 — wire join key so
                    # `JOIN zoom_recordings ON
                    #   z.zoom_id = t.source_conversation_id`
                    # finds the originating meeting. Mirrors
                    # the Fireflies path (FR-CR-05-39).
                    source_conversation_id=row.zoom_id,
                    source_message_ts=row.zoom_id,
                    source_permalink=row.zoom_share_url
                    or row.google_doc_url,
                    created_by_slack_user_id=admin_uid,
                )
                session.add(task)
                session.flush()
                # Initial status history row (None → todo).
                from app.models import TaskStatusHistory

                session.add(
                    TaskStatusHistory(
                        task_id=task.id,
                        from_status=None,
                        to_status=TaskStatus.todo,
                        changed_by_slack_user_id=admin_uid,
                        reason="zoom_extracted",
                        at=datetime.now(timezone.utc),
                    )
                )
                created += 1
                # Schedule Sheets / Google Tasks sync after the
                # outer commit lands.
                from app.sync.task_sync import schedule_sync_task

                schedule_sync_task(session, task.id)
                # FR-CR-05-120 follow-up — DM card posting moved
                # to a separate `_step_post_task_cards` step that
                # runs AFTER the short summary is sent. Operator
                # pinned: get the meeting overview first (Суть +
                # To-Do in one message), then dive into per-task
                # cards. Tasks created here just sit in the
                # session waiting for the post step.
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_task_create_failed",
                    title=title[:80],
                    error=str(e),
                )
        row.tasks_extracted = True
        row.tasks_extracted_count = created
        row.last_error = None
        return created

    def _step_verify_tasks(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-121 — second LLM pass to catch tasks missed
        by `_step_extract_tasks`. Symmetric to the Fireflies
        verifier; reuses the same prompt + tool schema."""
        from app.fireflies.prompts import (
            TASK_EXTRACTION_TOOL_DESCRIPTION as _DESC,
            TASK_EXTRACTION_TOOL_NAME as _NAME,
            TASK_EXTRACTION_TOOL_PARAMETERS as _PARAMS,
            TASK_VERIFICATION_SYSTEM,
        )
        from app.models import (
            Task,
            TaskPriority,
            TaskSourceKind,
            TaskStatus,
            TaskStatusHistory,
        )
        from app.persistence.tasks import normalize_task_title
        from app.services.team_members import as_known_employees
        from app.sync.task_sync import schedule_sync_task

        if not row.transcript_text or not row.detailed_summary:
            return 0
        existing = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        existing_block = "\n".join(
            f"- {t.title}: {(t.description or '')[:300]} "
            f"[owner={t.owner_display_name or '—'}]"
            for t in existing
        ) or "  (no tasks were extracted on the first pass)"
        try:
            known_employees = as_known_employees(session)
        except Exception:  # noqa: BLE001
            known_employees = []
        emp_table = _render_known_employees_table(known_employees)
        prompt_user = (
            f"Заголовок: {row.title or '(без названия)'}\n"
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}\n"
            "\nИзвестные сотрудники:\n"
            f"{emp_table}\n\n"
            "Already-extracted tasks (DO NOT duplicate these):\n"
            + existing_block + "\n\n"
            "Транскрипт встречи:\n"
            + row.transcript_text
        )
        try:
            data = self._llm.call_tool(  # type: ignore[attr-defined]
                system_prompt=TASK_VERIFICATION_SYSTEM,
                user_prompt=prompt_user,
                tool_name=_NAME,
                tool_description=_DESC,
                tool_parameters=_PARAMS,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
            ) or {}
        except Exception as e:  # noqa: BLE001
            log.info(
                "zoom_task_verification_failed",
                zoom_id=row.zoom_id, error=str(e),
            )
            return 0
        new_tasks = (data or {}).get("tasks") or []
        if not isinstance(new_tasks, list):
            new_tasks = []
        log.info(
            "zoom_task_verification_done",
            zoom_id=row.zoom_id,
            existing_count=len(existing),
            newly_added=len(new_tasks),
        )
        if not new_tasks:
            return 0
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        admin_uid = _admin_fallback_owner_id()
        today = datetime.now(timezone.utc).date()
        added = 0
        for t in new_tasks:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            try:
                title = normalize_task_title(title)
            except ValueError:
                continue
            owner_uid = (t.get("owner") or "").strip() or None
            if owner_uid and owner_uid not in valid_ids:
                owner_uid = None
            if not owner_uid and admin_uid:
                owner_uid = admin_uid
            owner_display_name = None
            if owner_uid and known_employees:
                for e in known_employees:
                    if e.get("slack_user_id") == owner_uid:
                        owner_display_name = (
                            e.get("real_name")
                            or e.get("display_name")
                            or owner_uid
                        )
                        break
            try:
                priority = TaskPriority(t.get("priority") or "medium")
            except ValueError:
                priority = TaskPriority.medium
            try:
                task = Task(
                    title=title[:10_000],
                    description=(t.get("description") or "").strip() or None,
                    priority=priority,
                    status=TaskStatus.todo,
                    owner_user_id=owner_uid,
                    owner_display_name=owner_display_name,
                    due_date=today,
                    due_time=time(18, 0),
                    is_current_week=True,
                    source_kind=TaskSourceKind.zoom,
                    source_conversation_id=row.zoom_id,
                    source_message_ts=row.zoom_id,
                    source_permalink=(
                        row.zoom_share_url or row.google_doc_url
                    ),
                    created_by_slack_user_id=admin_uid,
                )
                session.add(task)
                session.flush()
                session.add(
                    TaskStatusHistory(
                        task_id=task.id,
                        from_status=None,
                        to_status=TaskStatus.todo,
                        changed_by_slack_user_id=admin_uid,
                        reason="zoom_verified",
                        at=datetime.now(timezone.utc),
                    )
                )
                schedule_sync_task(session, task.id)
                added += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_task_verify_create_failed",
                    title=title[:80], error=str(e),
                )
        if added:
            row.tasks_extracted_count = (row.tasks_extracted_count or 0) + added
        return added

    def _step_post_task_cards(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-120 follow-up — post a DM card per extracted
        Task ROW to admin/owner. Runs AFTER `_step_send_short_
        summary` so the operator gets the meeting overview first
        (Суть + To-Do in one message) and then per-task cards
        cascade in. Returns number of cards posted."""
        if (
            self._sender is None
            or not getattr(self._sender, "enabled", False)
        ):
            return 0
        from app.models import Task as _Task
        from app.telegram_bot.cards import post_initial_card

        admin_uid = _admin_fallback_owner_id()
        tasks = (
            session.query(_Task)
            .filter(_Task.source_kind == TaskSourceKind.zoom)
            .filter(_Task.source_conversation_id == row.zoom_id)
            .filter(_Task.deleted_at.is_(None))
            .order_by(_Task.id.asc())
            .all()
        )
        posted = 0
        for task in tasks:
            try:
                post_initial_card(
                    sender=self._sender,
                    session=session,
                    task=task,
                    chat_id=0,  # ignored — DM-only delivery
                    reply_to_message_id=None,
                    author_user_id=admin_uid,
                )
                posted += 1
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_task_card_post_failed",
                    task_id=task.id,
                    error=str(e),
                )
        return posted

    # --- main entry-point -------------------------------------

    def process_one(
        self, session: Session, m: ZoomRecordingMeta
    ) -> ZoomPipelineReport:
        """Run all 6 steps for the given meeting. Idempotent —
        each step short-circuits when its flag is set on the
        DB row."""
        row = self._upsert_recording(session, m)
        report = ZoomPipelineReport(
            recording_id=row.id, zoom_id=row.zoom_id, title=row.title,
            errors=[],
        )
        row.attempts = (row.attempts or 0) + 1
        row.processed_at = datetime.now(timezone.utc)

        # FR-CR-05-122 — every step bracketed by `_trace_step`
        # so `grep zoom_step_(started|done|failed)` over the
        # listener log walks through a single recording's run
        # with `duration_ms` per step.
        from app.fireflies.pipeline import _trace_step
        ctx = {"zoom_id": row.zoom_id}

        # FR-CR-05-119 follow-up — order: detailed → tasks →
        # verify → doc → short → post_cards.
        for label, fn in (
            ("download", self._step_download_audio),
            ("transcribe", self._step_transcribe),
            ("detailed_summary", self._step_detailed_summary),
        ):
            with _trace_step("zoom", label, **ctx):
                ok = fn(row)
                if not ok:
                    if row.last_error:
                        report.errors.append(row.last_error)
                    log.info(
                        "zoom_recording_step_failed",
                        zoom_id=row.zoom_id, step=label,
                        err=row.last_error,
                    )
                    break

        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "extract_tasks", **ctx):
                    report.tasks_created = self._step_extract_tasks(
                        session, row
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_extract_tasks_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )

        # FR-CR-05-121 — verifier pass.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "verify_tasks", **ctx):
                    self._step_verify_tasks(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_task_verification_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )
        report.tasks_created = (
            row.tasks_extracted_count or report.tasks_created
        )

        if row.detailed_summarised:
            with _trace_step("zoom", "doc_export", **ctx):
                ok = self._step_doc_export(session, row)
                if not ok and row.last_error:
                    report.errors.append(row.last_error)

        if row.detailed_summarised:
            with _trace_step("zoom", "short_summary", **ctx):
                ok = self._step_short_summary(session, row)
                if not ok and row.last_error:
                    report.errors.append(row.last_error)

        # FR-CR-05-120 follow-up — DM cards posted LAST.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "post_task_cards", **ctx):
                    self._step_post_task_cards(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_post_task_cards_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )

        report.transcript_chars = len(row.transcript_text or "")
        report.detailed_chars = len(row.detailed_summary or "")
        report.short_chars = len(row.short_summary or "")
        report.google_doc_url = row.google_doc_url
        return report


__all__ = ["ZoomPipeline", "ZoomPipelineReport"]
