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
from datetime import datetime, timezone
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

    def _step_doc_export(self, row: ZoomRecording) -> bool:
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
        try:
            doc_id, url = docs.export_summary(
                title=title,
                body=row.detailed_summary,
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

    def _step_short_summary(self, row: ZoomRecording) -> bool:
        if row.short_summary_sent and row.short_summary:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary for short summary"
            return False
        # FR-CR-05-117 — feed the prompt the same field shape as
        # the Fireflies path so the canonical header/participants
        # layout stays consistent across sources.
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
        body = meta_line + "Подробный отчёт:\n" + row.detailed_summary
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=MEETING_SHORT_SUMMARY_PROMPT,
                user_prompt=body,
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
        # FR-CR-05-117 — same deterministic doc-link append as
        # Fireflies path so both pipelines emit the identical
        # «📄 Подробный отчёт: …» trailer.
        if row.google_doc_url:
            text = (
                text.rstrip()
                + "\n\n📄 Подробный отчёт: "
                + row.google_doc_url
            )
        row.short_summary = text
        row.last_error = None
        # Sender DMs handled by `_send_short_summary` if available.
        if self._sender is not None:
            self._send_short_summary(row)
        else:
            row.short_summary_sent = True
        return True

    def _send_short_summary(self, row: ZoomRecording) -> int:
        """DM the short summary to every admin uid. Returns
        number of successful sends."""
        from app.telegram_bot.handlers import admin_user_ids

        sent = 0
        for uid in sorted(admin_user_ids()):
            if not uid.lstrip("-").isdigit():
                continue
            try:
                resp = self._sender.send_message(
                    chat_id=int(uid), text=row.short_summary or ""
                )
                if (resp or {}).get("message_id"):
                    sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_short_summary_dm_failed",
                    uid=uid, error=str(e),
                )
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
            ) or {}
        except Exception as e:  # noqa: BLE001
            row.last_error = f"task extraction failed: {e}"
            return 0
        tasks = (data or {}).get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
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
            session.add(
                Task(
                    title=title,
                    description=(t.get("description") or "").strip() or None,
                    priority=priority,
                    status=TaskStatus.todo,
                    owner_user_id=owner_uid,
                    due_date=today,
                    is_current_week=True,
                    source_kind=TaskSourceKind.zoom,
                    source_permalink=row.zoom_share_url or row.google_doc_url,
                    created_by_slack_user_id=admin_uid,
                )
            )
            created += 1
        row.tasks_extracted = True
        row.tasks_extracted_count = created
        row.last_error = None
        return created

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

        steps = (
            ("download", self._step_download_audio),
            ("transcribe", self._step_transcribe),
            ("detailed", self._step_detailed_summary),
            ("doc", self._step_doc_export),
            ("short", self._step_short_summary),
        )
        for label, fn in steps:
            ok = fn(row)
            if not ok:
                if row.last_error:
                    report.errors.append(row.last_error)
                log.info(
                    "zoom_recording_step_failed",
                    zoom_id=row.zoom_id, step=label, err=row.last_error,
                )
                break

        # Tasks step — runs even if short summary failed (we
        # still have a detailed summary to extract from).
        if row.detailed_summarised:
            try:
                report.tasks_created = self._step_extract_tasks(session, row)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_extract_tasks_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )

        report.transcript_chars = len(row.transcript_text or "")
        report.detailed_chars = len(row.detailed_summary or "")
        report.short_chars = len(row.short_summary or "")
        report.google_doc_url = row.google_doc_url
        return report


__all__ = ["ZoomPipeline", "ZoomPipelineReport"]
