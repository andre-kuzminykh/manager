"""FR-CR-05-39 — Fireflies meeting-recording pipeline.

End-to-end orchestrator. Given a `FirefliesTranscript`, runs:

  1. Audio download (mp3 → local disk).
  2. Whisper transcription.
  3. Detailed RU summary via LLM (gpt-4o by default).
  4. Detailed summary → Google Doc.
  5. Short summary (≤2000 chars) for Telegram.
  6. Short summary DM to admin recipients.
  7. Task extraction with team_members context; due=today.

Each step writes its result onto the `MeetingRecording` row +
flips a progress flag so a re-run picks up where it crashed.
Idempotent — re-processing a meeting that's already done is a
near-no-op (each step skips when the flag is set).
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.fireflies.client import FirefliesClient, FirefliesTranscript
from app.fireflies.prompts import (
    DETAILED_SUMMARY_SYSTEM,
    SHORT_SUMMARY_SYSTEM,
    TASK_EXTRACTION_SYSTEM,
    TASK_EXTRACTION_TOOL_DESCRIPTION,
    TASK_EXTRACTION_TOOL_NAME,
    TASK_EXTRACTION_TOOL_PARAMETERS,
)
from app.logging_setup import get_logger
from app.models import MeetingRecording, Task, TaskSourceKind

log = get_logger(__name__)


@dataclass
class PipelineReport:
    """Outcome of running the pipeline on a single recording."""

    recording_id: int
    fireflies_id: str
    title: str | None
    transcript_chars: int = 0
    detailed_chars: int = 0
    short_chars: int = 0
    google_doc_url: str | None = None
    tasks_created: int = 0
    short_summary_recipients: int = 0
    skipped_reason: str | None = None
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _ffprobe_duration_seconds(path: str) -> float:
    """FR-CR-05-115 — return audio duration in seconds via
    `ffprobe`. Raises `RuntimeError` if ffprobe isn't on PATH
    or fails to parse the file."""
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not on PATH (install ffmpeg)")
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed: rc={proc.returncode} err={proc.stderr.strip()}"
        )
    try:
        return float(proc.stdout.strip())
    except ValueError as e:
        raise RuntimeError(f"ffprobe output unparseable: {proc.stdout!r}") from e


def _split_audio_into_chunks(path: str, *, max_bytes: int) -> list[str]:
    """FR-CR-05-115 — split `path` (an mp3 file) into chunks
    each ≤ `max_bytes`, using `ffmpeg -c copy` so we don't
    re-encode (preserves the audio bitrate). Returns the list
    of chunk file paths in order. The original file stays
    untouched.

    Strategy: use the duration / bytes ratio to compute a
    target chunk duration that should produce ≤ max_bytes
    chunks, then slice every `chunk_seconds` seconds. Round
    up the chunk count so we never under-split.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not on PATH")
    size = os.path.getsize(path)
    if size <= max_bytes:
        return [path]
    duration = _ffprobe_duration_seconds(path)
    if duration <= 0:
        raise RuntimeError(f"audio duration non-positive: {duration}")
    # +5% safety margin so we don't sit right at max_bytes.
    n_chunks = max(2, math.ceil(size * 1.05 / max_bytes))
    chunk_seconds = duration / n_chunks
    base = path.rsplit(".", 1)[0]
    out: list[str] = []
    for i in range(n_chunks):
        start = i * chunk_seconds
        chunk_path = f"{base}.chunk{i:02d}.mp3"
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{start:.3f}",
                "-t", f"{chunk_seconds:.3f}",
                "-i", path,
                "-c", "copy",
                chunk_path,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg chunk {i} failed: rc={proc.returncode} "
                f"err={proc.stderr.strip()}"
            )
        if not os.path.exists(chunk_path):
            raise RuntimeError(f"ffmpeg produced no output for chunk {i}")
        out.append(chunk_path)
    return out


_AUTO_STAMP_TITLE_RE = __import__("re").compile(
    # FR-CR-05-117 — Fireflies auto-titles meetings
    # «<Month> <DD>, <HH>:<MM> <AM|PM>» / «<Month> <DD> at
    # <HH><AM|PM>». Detect → derive a real topic from
    # transcript / participants instead.
    r"^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\s+\d{1,2}\b",
    flags=__import__("re").IGNORECASE,
)


def _looks_like_auto_stamp_title(title: str | None) -> bool:
    """FR-CR-05-117 — return True when `title` matches
    Fireflies' default auto-timestamp («Apr 30, 03:32 PM»,
    «May 5 at 5pm», etc.). Such titles carry zero semantic
    value — the pipeline derives a real one from the
    transcript later."""
    if not title:
        return True
    return _AUTO_STAMP_TITLE_RE.match(title.strip()) is not None


def _truncate(text: str | None, *, limit: int) -> str:
    """Trim `text` to `limit` chars without breaking mid-word
    when we can avoid it. Used to enforce the 2000-char Telegram
    cap on short summaries."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Trim back to the last whitespace if doing so doesn't lose
    # too much.
    last_ws = max(cut.rfind(" "), cut.rfind("\n"))
    if last_ws > limit - 200:
        cut = cut[:last_ws]
    return cut.rstrip() + "…"


class FirefliesPipeline:
    """Orchestrator wired with all the dependencies the pipeline
    steps need.

    Constructed once at process startup; ``process_one`` is the
    main entry-point and is safe to call repeatedly on the same
    recording (each step short-circuits when its flag is set).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: FirefliesClient,
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
        self, session: Session, t: FirefliesTranscript
    ) -> MeetingRecording:
        row = (
            session.query(MeetingRecording)
            .filter(MeetingRecording.fireflies_id == t.id)
            .first()
        )
        if row is None:
            row = MeetingRecording(
                fireflies_id=t.id,
                title=t.title,
                meeting_date=t.meeting_date,
                duration_seconds=t.duration_seconds,
                participants=list(t.participants),
                audio_url=t.audio_url,
                fireflies_share_url=t.share_url,
            )
            session.add(row)
            session.flush()
        else:
            # Refresh metadata in case Fireflies changed it (rare,
            # but happens for re-encoded recordings).
            if t.title and not row.title:
                row.title = t.title
            if t.meeting_date and not row.meeting_date:
                row.meeting_date = t.meeting_date
            if t.audio_url and not row.audio_url:
                row.audio_url = t.audio_url
            if t.share_url and not row.fireflies_share_url:
                row.fireflies_share_url = t.share_url
        return row

    # --- step 1: download mp3 ---------------------------------

    def _step_download_audio(self, row: MeetingRecording) -> bool:
        if row.audio_downloaded and row.audio_path and os.path.exists(row.audio_path):
            return True
        if not row.audio_url:
            row.last_error = "no audio_url on Fireflies record"
            return False
        dest = os.path.join(
            self._settings.fireflies_audio_dir, f"{row.fireflies_id}.mp3"
        )
        size = self._client.download_audio(
            url=row.audio_url,
            dest_path=dest,
            max_bytes=self._settings.fireflies_audio_max_bytes,
        )
        if size is None:
            row.last_error = "audio download failed or exceeded cap"
            return False
        row.audio_path = dest
        row.audio_downloaded = True
        row.last_error = None
        return True

    # --- step 2: Whisper transcribe ---------------------------

    def _step_transcribe(self, row: MeetingRecording) -> bool:
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
        # FR-CR-05-115 — Whisper hard-limits at 25 MB. Operator:
        # «значит мне надо резать файл по 24 мб, отдельно их
        # прогонять в whisper, а потом склеивать». Chunk via
        # ffmpeg into ≤24 MB pieces, transcribe each, join.
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
                "fireflies_audio_chunked_for_whisper",
                fireflies_id=row.fireflies_id,
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
            chunk_text = transcribe_bytes(
                audio_bytes=audio_bytes,
                mimetype="audio/mpeg",
                filename=os.path.basename(p),
                openai_api_key=api_key,
                model=self._settings.fireflies_whisper_model,
            )
            if not chunk_text:
                row.last_error = (
                    f"Whisper returned empty transcript on chunk {i+1}/"
                    f"{len(audio_paths)}"
                )
                return False
            transcript_parts.append(chunk_text)
        transcript = "\n".join(transcript_parts).strip()
        if not transcript:
            row.last_error = "Whisper returned empty transcript"
            return False
        # Cleanup temp chunks if we made any.
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

    # --- step 3: detailed RU summary --------------------------

    def _step_detailed_summary(self, row: MeetingRecording) -> bool:
        if row.detailed_summarised and row.detailed_summary:
            return True
        if not row.transcript_text:
            row.last_error = "no transcript for detailed summary"
            return False
        # FR-CR-05-117 — replace Fireflies' auto-stamp title
        # («Apr 30, 03:32 PM») with one derived from the
        # transcript before we feed everything into the LLM.
        if _looks_like_auto_stamp_title(row.title):
            try:
                derived = self._derive_topic_title(row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "fireflies_topic_title_derivation_failed",
                    fireflies_id=row.fireflies_id, error=str(e),
                )
                derived = None
            if derived:
                log.info(
                    "fireflies_topic_title_derived",
                    fireflies_id=row.fireflies_id,
                    old=row.title, new=derived,
                )
                row.title = derived
        meta_lines = [
            f"Заголовок: {row.title or '(без названия)'}",
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}",
            (
                f"Продолжительность: {row.duration_seconds // 60} мин"
                if row.duration_seconds
                else "Продолжительность: —"
            ),
            "Участники: " + ", ".join(row.participants or []) or "Участники: —",
        ]
        user_prompt = "\n".join(meta_lines) + "\n\nТранскрипт:\n" + row.transcript_text
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=DETAILED_SUMMARY_SYSTEM,
                user_prompt=user_prompt,
                model=self._settings.fireflies_summary_model,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"detailed summary LLM failed: {e}"
            return False
        if not text:
            row.last_error = "detailed summary LLM returned empty"
            return False
        row.detailed_summary = text
        row.detailed_summarised = True
        row.last_error = None
        return True

    def _derive_topic_title(self, row: MeetingRecording) -> str | None:
        """FR-CR-05-117 — call the LLM to extract a 1-line
        meeting topic suitable as a Google Doc title. Returns
        None on any failure; caller then keeps the original
        auto-stamp."""
        participants = ", ".join(row.participants or [])
        prompt = (
            "Determine a SHORT (≤60 chars) meeting topic in "
            "Russian for the transcript below. Prefer the "
            "external company / client name if any (ADNOC, "
            "Bosch, Goldman Sachs). Otherwise pick the main "
            "subject (раунд, проект, кандидат). Drop fluff. "
            "Output ONLY the topic, no quotes or extra text.\n\n"
            f"Участники: {participants}\n\n"
            f"Транскрипт (первые 6000 chars):\n"
            f"{(row.transcript_text or '')[:6000]}"
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=(
                    "You output ONE short Russian meeting topic "
                    "phrase. ≤60 chars. No quotes."
                ),
                user_prompt=prompt,
                model=self._settings.fireflies_short_summary_model,
                temperature=0.2,
            )
        except Exception:  # noqa: BLE001
            return None
        text = (text or "").strip().strip("«»\"' ").splitlines()[0:1]
        if not text:
            return None
        topic = text[0][:60].rstrip("., ")
        return topic or None

    # --- step 4: Google Doc export ----------------------------

    def _step_doc_export(self, row: MeetingRecording) -> bool:
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
        title = row.title or f"Meeting {row.fireflies_id}"
        try:
            doc_id, url = docs.export_summary(
                title=title,
                body=row.detailed_summary,
                parent_folder_id=self._settings.fireflies_docs_folder_id,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"Docs export failed: {e}"
            return False
        row.google_doc_id = doc_id
        row.google_doc_url = url
        row.doc_exported = True
        row.last_error = None
        return True

    # --- step 5: short summary -------------------------------

    def _step_short_summary(self, row: MeetingRecording) -> bool:
        if row.short_summary:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary as short-summary input"
            return False
        # FR-CR-05-54 — participants get their own block in the
        # user prompt so the LLM doesn't have to re-derive the
        # list from the transcript. One per line, falsy entries
        # dropped.
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
        user_prompt = meta_line + "Подробный отчёт:\n" + row.detailed_summary
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=SHORT_SUMMARY_SYSTEM,
                user_prompt=user_prompt,
                model=self._settings.fireflies_short_summary_model,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"short summary LLM failed: {e}"
            return False
        if not text:
            row.last_error = "short summary LLM returned empty"
            return False
        # FR-CR-05-54 — hard cap raised to 3800 chars (~10%
        # under the 4096-char Telegram per-message limit) so
        # the «Участники» + «Главные обсуждения» blocks added
        # to the prompt actually fit.
        body = _truncate(text, limit=3800)
        # FR-CR-05-117 — append the Google Doc link deterministic-
        # ally so it can never be truncated mid-URL or hallucinated
        # by the LLM. Skipped silently when the doc step didn't
        # produce a URL.
        if row.google_doc_url:
            body = (
                body.rstrip()
                + "\n\n📄 Подробный отчёт: "
                + row.google_doc_url
            )
        row.short_summary = body
        row.last_error = None
        return True

    # --- step 6: send short summary to TG admins --------------

    def _step_send_short_summary(self, row: MeetingRecording) -> int:
        if row.short_summary_sent:
            return 0
        if not row.short_summary or self._sender is None or not getattr(self._sender, "enabled", False):
            return 0
        from app.telegram_bot.handlers import admin_user_ids

        recipients = sorted(admin_user_ids())
        sent = 0
        for uid in recipients:
            try:
                uid_int = int(uid)
            except ValueError:
                continue
            try:
                resp = self._sender.send_message(
                    chat_id=uid_int, text=row.short_summary
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "fireflies_short_summary_send_failed",
                    uid=uid,
                    error=str(e),
                )
                continue
            if resp and resp.get("message_id"):
                sent += 1
        if sent:
            row.short_summary_sent = True
        return sent

    # --- step 7: task extraction -----------------------------

    def _step_extract_tasks(
        self, session: Session, row: MeetingRecording
    ) -> int:
        if row.tasks_extracted:
            return row.tasks_extracted_count or 0
        if not row.transcript_text:
            return 0
        from app.services.team_members import as_known_employees

        try:
            known_employees = as_known_employees(session, prefer_telegram=True)
        except Exception as e:  # noqa: BLE001
            log.info("fireflies_team_registry_unavailable", error=str(e))
            known_employees = []
        meta = (
            f"meeting_title: {row.title or ''}\n"
            f"participants: {', '.join(row.participants or [])}\n"
        )
        user_prompt = (
            "known_employees (pick a slack_user_id from this table):\n"
            + _render_known_employees_table(known_employees)
            + "\n\n"
            + meta
            + "\nТранскрипт встречи:\n"
            + row.transcript_text
        )
        try:
            result = self._llm.call_tool(  # type: ignore[attr-defined]
                system_prompt=TASK_EXTRACTION_SYSTEM,
                user_prompt=user_prompt,
                tool_name=TASK_EXTRACTION_TOOL_NAME,
                tool_description=TASK_EXTRACTION_TOOL_DESCRIPTION,
                tool_parameters=TASK_EXTRACTION_TOOL_PARAMETERS,
                model=self._settings.fireflies_tasks_model,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"task extraction LLM failed: {e}"
            return 0
        tasks = (result or {}).get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        admin_uid = _admin_fallback_owner_id()
        created = 0
        today = date.today()
        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            description = (t.get("description") or "").strip() or None
            if description:
                # FR-CR-05-117 — defensively strip «Name (uid)»
                # leaks from the description. Prompt forbids this
                # but the LLM occasionally still copies a
                # slack_user_id from the known_employees table
                # into prose. Only strip parenthesised values
                # that match an actual employee uid so we don't
                # eat legit «(2025)» / «($300k)» / «(Q2)»
                # parentheses.
                description = _strip_uid_suffixes(
                    description, valid_ids
                )
            priority = t.get("priority") or "medium"
            llm_owner_raw = (t.get("owner") or "").strip() or None
            owner_user_id = llm_owner_raw
            owner_resolution = "llm"
            if owner_user_id and known_employees and owner_user_id not in valid_ids:
                # LLM hallucinated a uid — null it.
                owner_user_id = None
                owner_resolution = "hallucinated_uid_dropped"
            if not owner_user_id and admin_uid:
                owner_user_id = admin_uid
                owner_resolution = (
                    "admin_fallback_null_owner"
                    if llm_owner_raw is None
                    else owner_resolution + "_then_admin_fallback"
                )
            log.info(
                "fireflies_task_owner_resolved",
                fireflies_id=row.fireflies_id,
                title=title[:80],
                llm_owner=llm_owner_raw,
                final_owner=owner_user_id,
                resolution=owner_resolution,
            )
            owner_display_name = None
            if owner_user_id and known_employees:
                for e in known_employees:
                    if e.get("slack_user_id") == owner_user_id:
                        owner_display_name = (
                            e.get("real_name") or e.get("display_name") or owner_user_id
                        )
                        break
            try:
                from app.models import TaskPriority, TaskStatus

                task_status = TaskStatus.todo  # due=today → todo per FR-CR-04
                task = Task(
                    title=title[:10_000],
                    description=description,
                    owner_user_id=owner_user_id,
                    owner_display_name=owner_display_name,
                    priority=TaskPriority(priority) if priority in {p.value for p in TaskPriority} else TaskPriority.medium,
                    due_date=today,  # FR-CR-05-39: meeting tasks default to today
                    due_time=time(18, 0),  # FR-CR-05-63: default 18:00 deadline
                    status=task_status,
                    is_current_week=True,
                    source_kind=TaskSourceKind.fireflies,
                    source_conversation_id=row.fireflies_id,
                    source_message_ts=row.fireflies_id,
                    source_permalink=row.fireflies_share_url,
                    created_by_slack_user_id=admin_uid,
                )
                session.add(task)
                session.flush()
                # Initial history row (None → todo).
                from app.models import TaskStatusHistory

                session.add(
                    TaskStatusHistory(
                        task_id=task.id,
                        from_status=None,
                        to_status=task_status,
                        changed_by_slack_user_id=admin_uid,
                        reason="fireflies_extracted",
                        at=datetime.now(timezone.utc),
                    )
                )
                created += 1
                # Schedule sheet sync.
                from app.sync.task_sync import schedule_sync_task

                schedule_sync_task(session, task.id)
                # FR-CR-05-58 — post a TG card per task so the
                # owner / admins see them in their DM with the
                # bot. Without this, Fireflies tasks lived only
                # in the DB + Sheet — invisible until the next
                # morning digest. The card carries the same
                # interactive keyboard as live cards (Start /
                # Edit / Mark done / Subscribe).
                if self._sender is not None and getattr(self._sender, "enabled", False):
                    try:
                        from app.telegram_bot.cards import post_initial_card

                        post_initial_card(
                            sender=self._sender,
                            session=session,
                            task=task,
                            chat_id=0,  # ignored — DM-only delivery
                            reply_to_message_id=None,
                            author_user_id=admin_uid,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.info(
                            "fireflies_task_card_post_failed",
                            task_id=task.id,
                            error=str(e),
                        )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "fireflies_task_create_failed",
                    title=title[:80],
                    error=str(e),
                )
        row.tasks_extracted_count = created
        row.tasks_extracted = True
        return created

    # --- main entry ------------------------------------------

    def process_one(
        self, session: Session, transcript: FirefliesTranscript
    ) -> PipelineReport:
        """Run every step on `transcript`. Returns a counters
        report; persists per-step artefacts on the
        `MeetingRecording` row."""
        row = self._upsert_recording(session, transcript)
        report = PipelineReport(
            recording_id=row.id,
            fireflies_id=row.fireflies_id,
            title=row.title,
        )
        # FR-CR-05-53 — only short-circuit when EVERY pipeline
        # step succeeded. The earlier `processed_at AND
        # tasks_extracted` check stuck the recording in a
        # «pretend done» state when an upstream step like Google
        # Docs export had failed but the tail of the pipeline
        # (short summary + task extraction) still ran. A retry
        # then skipped the failed step instead of fixing it.
        # Each step is internally idempotent — if its flag is
        # set the work is short-circuited inside the helper —
        # so re-running is cheap.
        if (
            row.processed_at
            and row.audio_downloaded
            and row.transcribed
            and row.detailed_summarised
            and row.doc_exported
            and row.short_summary_sent
            and row.tasks_extracted
        ):
            report.skipped_reason = "already_processed"
            return report
        row.attempts += 1

        if not self._step_download_audio(row):
            session.flush()
            report.errors.append(row.last_error or "download_failed")
            return report
        if not self._step_transcribe(row):
            session.flush()
            report.errors.append(row.last_error or "transcribe_failed")
            return report
        report.transcript_chars = len(row.transcript_text or "")
        if not self._step_detailed_summary(row):
            session.flush()
            report.errors.append(row.last_error or "detailed_summary_failed")
            return report
        report.detailed_chars = len(row.detailed_summary or "")
        # Doc export — if it fails we still send the short
        # summary (sans link) and extract tasks.
        if not self._step_doc_export(row):
            log.warning(
                "fireflies_doc_export_failed",
                recording_id=row.id,
                error=row.last_error,
            )
            report.errors.append(row.last_error or "doc_export_failed")
        else:
            report.google_doc_url = row.google_doc_url
        if not self._step_short_summary(row):
            log.warning(
                "fireflies_short_summary_failed",
                recording_id=row.id,
                error=row.last_error,
            )
            report.errors.append(row.last_error or "short_summary_failed")
        report.short_chars = len(row.short_summary or "")
        report.short_summary_recipients = self._step_send_short_summary(row)
        report.tasks_created = self._step_extract_tasks(session, row)

        row.processed_at = datetime.now(timezone.utc)
        session.flush()
        return report


def _render_known_employees_table(employees: list[dict]) -> str:
    """Same shape as the owner_prompt's `known_employees` block —
    keeps the LLM aligned with what it sees on regular intent
    extraction."""
    lines = [
        "  slack_user_id          | display_name        | real_name                      | role                       | notes"
    ]
    for e in employees:
        sid = (e.get("slack_user_id") or "")[:22]
        dn = (e.get("display_name") or "")[:25]
        rn = (e.get("real_name") or "")[:30]
        role = (e.get("role") or "")[:26]
        notes = (e.get("notes") or "")[:200]
        lines.append(
            f"  {sid:<22} | {dn:<19} | {rn:<30} | {role:<26} | {notes}"
        )
    return "\n".join(lines)


def _strip_uid_suffixes(text: str, valid_ids: set[str | None]) -> str:
    """FR-CR-05-117 — remove «Name (462156243)»-style uid leaks
    from a task description. Only strips parenthesised tokens
    that match a real `slack_user_id` from `known_employees`,
    preserving legitimate parentheses like «(Q2)», «($300k)»,
    «(2025)»."""
    real_ids = {str(v) for v in valid_ids if v}
    if not real_ids or not text:
        return text
    import re

    def _drop(match: __import__("re").Match[str]) -> str:
        token = match.group(1)
        if token in real_ids:
            return ""
        return match.group(0)

    return re.sub(r"\s*\(([A-Za-z0-9_]+)\)", _drop, text).strip()


def _admin_fallback_owner_id() -> str | None:
    """First admin uid from `TELEGRAM_ADMIN_USER_IDS`. Same
    fallback the TG ingest uses (FR-CR-05-09)."""
    try:
        from app.telegram_bot.handlers import admin_user_ids

        admins = sorted(admin_user_ids())
    except Exception:  # noqa: BLE001
        return None
    return admins[0] if admins else None


__all__ = ["FirefliesPipeline", "PipelineReport"]
