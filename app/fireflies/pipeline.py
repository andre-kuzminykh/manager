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

import html
import math
import os
import re
import shutil
import subprocess
import time as _trace_time
from contextlib import contextmanager
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


# FR-CR-05-196 — runaway retry cap (mirror of zoom.pipeline constant).
MAX_ATTEMPTS_BEFORE_GIVE_UP = 20


def _log_and_trace(
    source: str, recording_id: str | None, event: str, **fields: Any,
) -> None:
    """FR-CR-05-128 — emit ONE structlog `<source>_<event>` line
    AND ONE per-recording trace JSONL row in lockstep, so the
    operator can grep docker logs OR cat the per-recording
    trace file and see the same events.

    `event` is the bare event name (without source prefix); the
    structlog log key becomes `<source>_<event>`. `recording_id`
    is `MeetingRecording.fireflies_id` / `ZoomRecording.zoom_id`.
    """
    from app.services.trace_log import trace_event

    ctx_for_log = dict(fields)
    if source == "fireflies":
        ctx_for_log.setdefault("fireflies_id", recording_id)
    elif source == "zoom":
        ctx_for_log.setdefault("zoom_id", recording_id)
    log.info(f"{source}_{event}", **ctx_for_log)
    trace_event(source=source, recording_id=recording_id,
                event=event, **fields)


@contextmanager
def _trace_step(source: str, step: str, **ctx):
    """FR-CR-05-122 / FR-CR-05-128 — every pipeline step is
    bracketed by a started/done log line so the operator can
    walk through a rerun by `grep step_started|step_done`. Same
    events also append a JSONL row to
    `/app/traces/<source>-<recording-id>.jsonl` via
    `trace_event` (operator-pinned: «мне под каждый вызов надо
    в трейсы складывать с датой и временем»).
    """
    from app.services.trace_log import trace_event

    started = _trace_time.monotonic()
    rec_id = ctx.get("fireflies_id") or ctx.get("zoom_id") or ctx.get("recording_id")
    log.info(f"{source}_step_started", step=step, **ctx)
    trace_event(source=source, recording_id=rec_id,
                event="step_started", step=step, **ctx)
    try:
        yield
    except Exception as e:  # noqa: BLE001
        elapsed = int((_trace_time.monotonic() - started) * 1000)
        log.warning(
            f"{source}_step_failed",
            step=step, duration_ms=elapsed, error=str(e), **ctx,
        )
        trace_event(source=source, recording_id=rec_id,
                    event="step_failed", step=step,
                    duration_ms=elapsed, error=str(e))
        raise
    else:
        elapsed = int((_trace_time.monotonic() - started) * 1000)
        log.info(
            f"{source}_step_done",
            step=step, duration_ms=elapsed, **ctx,
        )
        trace_event(source=source, recording_id=rec_id,
                    event="step_done", step=step, duration_ms=elapsed)


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


def _sniff_audio_extension(path: str) -> str | None:
    """FR-CR-05-117 — peek at the first 16 bytes and decide the
    real container. Returns the extension WITHOUT a dot, or
    None when the bytes don't match anything we know.

    Zoom's `download_url` never carries the file extension, so
    we used to guess based on the URL string and got it wrong
    when M4A/MP4 collisions happened. Sniffing is reliable.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if len(head) < 12:
        return None
    # ISO-BMFF (MP4 / M4A): bytes 4-7 are «ftyp», 8-11 are the
    # major brand. M4A uses brand «M4A », MP4 video uses «mp42»
    # / «isom» / «iso2».
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"mp41", b"mp42") and (
            head[8:11] in (b"M4A", b"M4B")
        ):
            return "m4a"
        return "mp4"
    # MP3 ID3 header «ID3» or MP3 frame sync «0xFFE…» / «0xFFF…».
    if head[:3] == b"ID3":
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    # WAV «RIFF….WAVE».
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    # Ogg «OggS».
    if head[:4] == b"OggS":
        return "ogg"
    # FLAC «fLaC».
    if head[:4] == b"fLaC":
        return "flac"
    # WebM / Matroska «1a 45 df a3».
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return None


def _split_audio_into_chunks(
    path: str,
    *,
    max_bytes: int,
    max_duration_seconds: float | None = None,
) -> list[str]:
    """FR-CR-05-115 — split `path` (an mp3 file) into chunks
    each ≤ `max_bytes`, using `ffmpeg -c copy` so we don't
    re-encode (preserves the audio bitrate). Returns the list
    of chunk file paths in order. The original file stays
    untouched.

    Strategy: use the duration / bytes ratio to compute a
    target chunk duration that should produce ≤ max_bytes
    chunks, then slice every `chunk_seconds` seconds. Round
    up the chunk count so we never under-split.

    FR-CR-05-177 — `max_duration_seconds` is an upper bound on
    chunk duration. OpenAI's diarization models cap input at
    1400 s/chunk regardless of file size; pass a safety margin
    (~1300 s) to keep chunks under the limit. When None the
    chunker only respects the byte cap (original behaviour).
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not on PATH")
    size = os.path.getsize(path)
    duration = _ffprobe_duration_seconds(path)
    needs_byte_split = size > max_bytes
    needs_duration_split = (
        max_duration_seconds is not None
        and duration > max_duration_seconds
    )
    if not needs_byte_split and not needs_duration_split:
        return [path]
    if duration <= 0:
        raise RuntimeError(f"audio duration non-positive: {duration}")
    # +5% safety margin so we don't sit right at max_bytes.
    n_chunks_bytes = (
        math.ceil(size * 1.05 / max_bytes) if needs_byte_split else 1
    )
    n_chunks_dur = (
        math.ceil(duration / max_duration_seconds)
        if max_duration_seconds is not None
        else 1
    )
    n_chunks = max(2, n_chunks_bytes, n_chunks_dur)
    chunk_seconds = duration / n_chunks
    base, _, in_ext = path.rpartition(".")
    if not base:
        base, in_ext = path, "mp3"
    # FR-CR-05-117 — chunker preserves the input container so
    # `-c copy` works (AAC into M4A, MP3 into MP3, etc.). Forcing
    # `.mp3` while the source is M4A/AAC trips ffmpeg with
    # «Exactly one MP3 audio stream is required».
    out_ext = (in_ext or "mp3").lower()
    out: list[str] = []
    for i in range(n_chunks):
        start = i * chunk_seconds
        chunk_path = f"{base}.chunk{i:02d}.{out_ext}"
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


def _build_counterparties_section_for_doc(
    session: "Session",
    *,
    source_kind: str,
    source_id: str,
) -> str:
    """FR-CR-05-125 — render the matched-counterparties block
    for the Google Doc. One-line-per-counterparty: «<name> —
    <type>». Returns "" when nothing matched (Doc body stays
    clean)."""
    from app.models import Counterparty, CounterpartyMention

    rows = (
        session.query(Counterparty)
        .join(
            CounterpartyMention,
            CounterpartyMention.counterparty_id == Counterparty.id,
        )
        .filter(CounterpartyMention.source_kind == source_kind)
        .filter(CounterpartyMention.source_id == source_id)
        .order_by(CounterpartyMention.id.asc())
        .all()
    )
    if not rows:
        return ""
    # FR-CR-05-132 — `type` removed from the hub; doc section is
    # just the canonical name list.
    lines = ["", "🔗 КОНТРАГЕНТЫ", ""]
    for cp in rows:
        lines.append(f"• {cp.name}")
    return "\n".join(lines).rstrip() + "\n"


def _build_counterparties_section_for_short_summary(
    session: "Session",
    *,
    source_kind: str,
    source_id: str,
) -> str:
    """FR-CR-05-125 — single-line counterparty block for the
    short TG summary. Comma-separated names (no types — keeps
    the message scannable). Returns "" when no matches."""
    from app.models import Counterparty, CounterpartyMention

    rows = (
        session.query(Counterparty)
        .join(
            CounterpartyMention,
            CounterpartyMention.counterparty_id == Counterparty.id,
        )
        .filter(CounterpartyMention.source_kind == source_kind)
        .filter(CounterpartyMention.source_id == source_id)
        .order_by(CounterpartyMention.id.asc())
        .all()
    )
    if not rows:
        return ""
    return "🔗 Контрагенты: " + ", ".join(cp.name for cp in rows)


def _build_full_tasks_section_for_doc(
    session: "Session",
    *,
    source_kind: "TaskSourceKind",
    source_conversation_id: str,
) -> str:
    """FR-CR-05-119 follow-up — full task list for the Google
    Doc body. Each task gets the verbatim multi-sentence
    description + owner + due-date + priority. Operator wants
    the doc to be the single archived reference; the short TG
    summary keeps a compressed one-sentence variant via
    `_build_todo_section`. Returns "" when no tasks."""
    from app.models import Task as _Task

    tasks = (
        session.query(_Task)
        .filter(_Task.source_kind == source_kind)
        .filter(_Task.source_conversation_id == source_conversation_id)
        .filter(_Task.deleted_at.is_(None))
        .order_by(_Task.id.asc())
        .all()
    )
    if not tasks:
        return ""
    lines = ["", "📌 ЗАДАЧИ", ""]
    for i, t in enumerate(tasks, 1):
        body = (t.description or "").strip() or (t.title or "").strip()
        lines.append(f"{i}. {body}")
        meta_bits = []
        owner = (t.owner_display_name or "").strip()
        if owner:
            meta_bits.append(f"Ответственный: {owner}")
        if t.due_date:
            due = t.due_date.strftime("%d.%m.%Y")
            if t.due_time:
                due += f" {t.due_time.strftime('%H:%M')}"
            meta_bits.append(f"Срок: {due}")
        priority = (
            t.priority.value if t.priority is not None else None
        )
        if priority and priority != "medium":
            meta_bits.append(f"Приоритет: {priority}")
        if meta_bits:
            lines.append("   " + " · ".join(meta_bits))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# FR-CR-05-128 — Telegram's per-message hard limit is 4096
# chars. We aim for 4000 to leave room for HTML wrapping (the
# `<a href>` block adds ~70-100 chars). Operator-pinned: the
# «Header + Участники + Суть + To-Do» overview MUST land in
# ONE message. When the verbose To-Do would push past this
# limit, the short-summary step falls back to a compact title-
# only To-Do — full descriptions still ship via the Doc + per-
# task DM cards.
_SHORT_SUMMARY_ONE_MESSAGE_LIMIT = 4000


def _build_todo_section(
    session: "Session",
    *,
    source_kind: "TaskSourceKind",
    source_conversation_id: str,
    compact: bool = False,
    filter_by_direction: bool = True,
) -> str:
    """FR-CR-05-119 — render the To-Do block from the actual
    `Task` rows extracted for this recording. Items are
    compressed to ONE sentence (FR-CR-05-119 follow-up): the
    short TG summary stays scannable while the full multi-
    sentence description lives on the per-task DM card the
    operator gets via `post_initial_card`. Sorted by creation
    order so the operator sees the same sequence as the DM
    cards arriving in TG.

    `compact=True` (FR-CR-05-128) renders just «N) Title (Owner)»
    — used as a fallback when the full descriptions would push
    the overview body past Telegram's 4096-char per-message cap.
    Operator-pinned: «Header + Участники + Суть + To-Do» MUST
    land in ONE message; verbatim descriptions still ship via
    the Doc + per-task DM cards.

    Returns "" when no tasks were extracted (operator pinned:
    drop the section entirely instead of an empty header).
    """
    from app.models import Task as _Task

    tasks = (
        session.query(_Task)
        .filter(_Task.source_kind == source_kind)
        .filter(_Task.source_conversation_id == source_conversation_id)
        .filter(_Task.deleted_at.is_(None))
        .order_by(_Task.id.asc())
        .all()
    )
    if not tasks:
        return ""
    # FR-CR-05-163 follow-up — operator-pinned: «надо убрать эмодзи
    # и выводить в саммери только то что относится к бейджам этим,
    # но сами бейджи не выводи». Фильтруем по important directions,
    # рендерим без префикса.
    from app.services.task_direction import DIRECTIONS_IMPORTANT

    def _format_deadline(task) -> str:  # noqa: ANN001
        """DD.MM.YYYY HH:MM. Default = today 18:00 if both empty."""
        from datetime import date, time as _time

        d = task.due_date if getattr(task, "due_date", None) else date.today()
        t = task.due_time if getattr(task, "due_time", None) else _time(23, 59)
        return f"{d.strftime('%d.%m.%Y')} {t.strftime('%H:%M')}"

    items: list[str] = []
    idx = 0
    for t in tasks:
        # FR-CR-05-163 follow-up — фильтр по important direction.
        direction = None
        try:
            extra = t.extra or {}
            if isinstance(extra, dict):
                direction = extra.get("direction")
        except Exception:  # noqa: BLE001
            direction = None
        # FR-CR-05-199 — фильтр применяется только если flag установлен
        # (default True для backwards-compat с legacy auto-publish).
        # V2 publish зовёт с filter_by_direction=False — ВСЕ tasks
        # попадают в thread reply.
        if filter_by_direction and direction not in DIRECTIONS_IMPORTANT:
            continue
        idx += 1
        owner = (t.owner_display_name or "").strip()
        if compact:
            raw = (t.title or "").strip() or (t.description or "").strip()
        else:
            raw = (t.description or "").strip() or (t.title or "").strip()
            if len(raw) > 350:
                cut = raw.rfind(" ", 0, 350)
                raw = (raw[: cut if cut > 200 else 350]).rstrip(",;:- ") + "…"
        suffix_parts: list[str] = []
        if owner:
            suffix_parts.append(owner)
        suffix_parts.append(_format_deadline(t))
        suffix = " • ".join(suffix_parts)
        items.append(f"{idx}) {raw} — {suffix}")
    if not items:
        # Нет ни одной important задачи — раздел не рендерим.
        return ""
    # FR-CR-05-128 follow-up — operator regression: splitter was
    # cutting mid-task because the entire To-Do block was a
    # single paragraph («\n» between items). Use «\n\n» between
    # the «To-Do:» header and items, and between items, so
    # `_paragraph_greedy_split` treats each task as its own
    # paragraph and splits BETWEEN tasks rather than mid-text.
    return "To-Do:\n\n" + "\n\n".join(items)


def _first_sentence(text: str, *, limit: int = 240) -> str:
    """FR-CR-05-119 follow-up — return the first sentence of
    `text`, capped at `limit` chars. Used to compress task
    descriptions for the short-summary «To-Do» block while the
    full multi-sentence description still ships on the per-task
    DM card.

    Strategy:
      1. Find the first `.`, `!`, `?` followed by whitespace/EOL.
         If at least 30 chars in (avoids «И. Иванов» false
         positives), take everything up to it.
      2. Otherwise cap at `limit` chars on a word boundary,
         appending an ellipsis.
    """
    if not text:
        return ""
    text = text.strip()
    import re

    head = text[: limit + 60]  # small lookahead for late period
    m = re.search(r"[.!?](?:\s|$)", head)
    if m and m.start() >= 30 and m.start() <= limit:
        sentence = text[: m.start() + 1].rstrip()
        return sentence
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut < int(limit * 0.6):
        cut = limit
    return text[:cut].rstrip(",;:- ") + "…"


_TODO_SECTION_HEADERS_RE = __import__("re").compile(
    # FR-CR-05-119 — match a heading line for an LLM-emitted
    # To-Do / next-steps block + everything after it up to
    # end-of-string. The To-Do block is ALWAYS the last block in
    # the short summary, so we greedily strip to end.
    #
    # FR-CR-05-184 — operator-pinned 2026-05-21: previous regex
    # used a `\n{2,}\S` lookahead that bailed out at the blank
    # line BEFORE the first task — stripping ONLY the «To-Do:»
    # header and leaking the tasks themselves into the Slack
    # parent post (visible: «18) Benjamin... — Alina Kolpakova»
    # in the parent body). Replace the alternation with `\Z`-only
    # so the entire trailer including every numbered task is
    # removed.
    r"\n{1,2}(?:to[\s\-]?do|to do list|следующие\s+шаги|"
    r"next\s+steps|action\s+items|action\s+list|"
    r"задачи|to[-\s]do list)\s*:[\s\S]*\Z",
    flags=__import__("re").IGNORECASE,
)


def _strip_llm_todo_block(text: str) -> str:
    """FR-CR-05-119 — strip any LLM-emitted To-Do / Next-steps
    section so we can append the deterministic one without
    duplication. Tolerant of multiple labels and of the LLM
    occasionally emitting the section despite the prompt
    forbidding it (FR-CR-05-119)."""
    if not text:
        return text
    return _TODO_SECTION_HEADERS_RE.sub("", text).rstrip()


_RE_LEADS_WITH_DATE = re.compile(r"^\d{1,2}/\d{1,2}(\s|/|-)")


def _force_meeting_title_first_line(
    body: str,
    title: str,
    meeting_date: datetime | None = None,
) -> str:
    """FR-CR-05-156 — operator-pinned: «первая строка summary должна
    быть точным названием встречи (не LLM-переписанным)». Substitute
    the raw `row.title` for whatever the LLM emitted on line 1.

    FR-CR-05-156 follow-up — prepend `DD/MM - ` from meeting_date
    so the title reads as «05/05 - Genia Xasis <> Humanoid …». If
    meeting_date is None, the date prefix is omitted (back-compat).
    If the title already starts with `DD/MM`, the prefix is skipped.
    Empty title or empty body → returned as-is."""
    if not (body or "").strip() or not (title or "").strip():
        return body
    parts = body.split("\n", 1)
    rest = parts[1] if len(parts) > 1 else ""
    raw_title = title.strip()
    if meeting_date is not None and not _RE_LEADS_WITH_DATE.match(raw_title):
        prefix = meeting_date.strftime("%d/%m")
        first_line = f"{prefix} - {raw_title}"
    else:
        first_line = raw_title
    return f"{first_line}\n{rest}"


def _wrap_short_summary_with_doc_link(body: str, doc_url: str) -> str:
    """FR-CR-05-127 — replace the «📄 Подробный отчёт: <url>»
    trailer with an HTML hyperlink wrapping the FIRST line of
    `body` (operator-pinned: the meeting title «DD/MM - <Topic>»
    becomes a clickable link to the Google Doc, no trailer
    line). The rest of the body is HTML-escaped so Telegram
    `parse_mode=HTML` accepts it (free-text owner names with
    `&`, deal numbers with `<`/`>` survive intact).

    Empty / no-URL → body returned as-is (unescaped); we only
    escape when we're emitting an HTML-wrapped first line so
    that the existing plain-text path stays unchanged.
    """
    if not body:
        return body
    if not doc_url:
        return body
    sep_idx = body.find("\n")
    if sep_idx == -1:
        first, rest = body, ""
    else:
        first, rest = body[:sep_idx], body[sep_idx:]
    safe_url = html.escape(doc_url, quote=True)
    safe_first = html.escape(first.strip())
    safe_rest = html.escape(rest)
    return f'<a href="{safe_url}">{safe_first}</a>{safe_rest}'


def _dedupe_meeting_tasks(
    session: "Session",
    *,
    source_kind: "TaskSourceKind",
    conversation_id: str,
) -> list[tuple[int, int, str]]:
    """FR-CR-05-128 — soft-delete near-duplicate Tasks the
    extract+verify passes produce for one meeting. Operator
    regression: 50-min fundraising sync emitted 48 tasks with
    «Сегментация инвесторов» 3×, «BauerDart» 2×, «Felix Capital
    и Supernova» 2×, etc.

    Two heuristics, applied in order:

    1. **Topic-prefix exact match.** Each task description is in
       the operator-pinned `<topic> - <verb action>` shape. The
       substring before the first « - » is the topic; identical
       topics (case-insensitive, NFKD-folded) ⇒ duplicate.

    2. **Title fuzzy match.** `SequenceMatcher.ratio()` on
       lower-cased titles ≥ 0.85 ⇒ duplicate. Catches
       «BauerDart» vs «Bauer/Dart» style variants.

    Keeps the EARLIER task (lower id from extract pass —
    usually richer descriptions), soft-deletes the LATER.
    Returns the deletion plan `[(kept_id, dropped_id, reason)]`.
    """
    import difflib
    import unicodedata
    from datetime import datetime, timezone

    from app.models import Task as _Task

    tasks = (
        session.query(_Task)
        .filter(_Task.source_kind == source_kind)
        .filter(_Task.source_conversation_id == conversation_id)
        .filter(_Task.deleted_at.is_(None))
        .order_by(_Task.id.asc())
        .all()
    )
    if len(tasks) < 2:
        return []

    def _fold(s: str) -> str:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        return s.lower().strip()

    def _topic_prefix(t: "_Task") -> str:
        body = (t.description or t.title or "").strip()
        if " - " in body:
            head = body.split(" - ", 1)[0]
        else:
            head = body
        return _fold(head)

    plan: list[tuple[int, int, str]] = []
    dropped_ids: set[int] = set()
    # FR-CR-05-128 — operator-pinned: tasks share a topic prefix
    # if and only if they reference the same counterparty /
    # subject. Title fuzzy was too aggressive («Задача про
    # Felix» vs «Задача про NVIDIA» share «Задача про» prefix
    # and tripped 0.85 ratio); restrict to topic-prefix exact
    # match. Falls back to full-description ratio ≥ 0.92 when
    # topic prefix is missing (no « - » in either) — catches
    # legacy tasks not in the operator-pinned shape.
    # FR-CR-05-129 follow-up — operator-pinned «мне всегда надо
    # максимум информации». Dedup must be CONSERVATIVE: same
    # topic-prefix is NOT enough («Felix Capital — отправить
    # апдейт» and «Felix Capital — назначить звонок» share the
    # prefix but are two distinct actions). Require BOTH:
    #   - topic-prefix exact match, AND
    #   - description ratio ≥ 0.85 (full body similarity)
    # Or fallback when no topic prefix:
    #   - description ratio ≥ 0.90 alone
    # FR-CR-05-129 follow-up — operator wants pairs like
    # «tether - email» + «tether - WhatsApp» (same task,
    # different channel) collapsed; same for «Felix Capital -
    # call» + «Felix Capital - check size», «Nvidia -
    # write doc» + «Nvidia - edit doc». These have same topic
    # prefix but description ratio ~0.5-0.7. Threshold lowered
    # 0.85 → 0.55 so related-action pairs merge but truly
    # unrelated topic-prefix collisions don't.
    for i, kept in enumerate(tasks):
        if kept.id in dropped_ids:
            continue
        kept_prefix = _topic_prefix(kept)
        kept_desc = _fold((kept.description or "").strip())
        for cand in tasks[i + 1:]:
            if cand.id in dropped_ids:
                continue
            cand_prefix = _topic_prefix(cand)
            cand_desc = _fold((cand.description or "").strip())
            reason = ""
            desc_ratio = (
                difflib.SequenceMatcher(None, kept_desc, cand_desc).ratio()
                if kept_desc and cand_desc else 0.0
            )
            if (
                kept_prefix and cand_prefix
                and kept_prefix == cand_prefix
                and desc_ratio >= 0.55
            ):
                reason = "topic_prefix_and_desc_match"
            elif (
                not kept_prefix
                and not cand_prefix
                and desc_ratio >= 0.90
            ):
                reason = "description_fuzzy_match"
            if reason:
                cand.deleted_at = datetime.now(timezone.utc)
                dropped_ids.add(cand.id)
                plan.append((kept.id, cand.id, reason))
    if plan:
        session.flush()
        kind_str = (
            source_kind.value if hasattr(source_kind, "value") else str(source_kind)
        )
        log.info(
            "meeting_task_dedupe_done",
            source_kind=kind_str,
            conversation_id=conversation_id,
            dropped=len(plan),
            kept=len(tasks) - len(plan),
            sample=[
                {"kept_id": k, "dropped_id": d, "reason": r}
                for k, d, r in plan[:10]
            ],
        )
        try:
            from app.services.trace_log import trace_event
            trace_event(
                source=kind_str, recording_id=conversation_id,
                event="task_dedupe_done",
                dropped=len(plan),
                kept=len(tasks) - len(plan),
                plan=[
                    {"kept_id": k, "dropped_id": d, "reason": r}
                    for k, d, r in plan
                ],
            )
        except Exception:  # noqa: BLE001
            pass
    return plan


def _split_for_telegram(text: str, *, limit: int = 3800) -> list[str]:
    """FR-CR-05-119 — split a long short-summary body into
    Telegram-sized chunks (≤4096 chars per message).

    FR-CR-05-126 follow-up — operator-pinned section-aware
    behaviour: «Header + Участники + Суть + To-Do» MUST land
    in ONE message (the overview block). Optional «🔗
    Контрагенты» and «📄 Подробный отчёт» trailers can spill to
    a second message. We find the first reference / trailer
    section marker and try to use that as the split point. Only
    when the overview itself exceeds `limit` do we fall back to
    plain paragraph-greedy packing.

    Returns at least one chunk; empty input → []."""
    if not text:
        return []
    text = text.strip()
    if len(text) <= limit:
        return [text]
    # FR-CR-05-126 — section-aware split. Find the first marker
    # among the optional trailers; everything before goes to
    # chunk 1 (overview), the rest joins chunk 2.
    overview_end_idx: int | None = None
    for marker in ("\n\n🔗 Контрагенты", "\n\n📄 Подробный отчёт"):
        idx = text.find(marker)
        if idx == -1:
            continue
        if overview_end_idx is None or idx < overview_end_idx:
            overview_end_idx = idx
    if overview_end_idx is not None:
        head = text[:overview_end_idx].rstrip()
        tail = text[overview_end_idx:].lstrip()
        if len(head) <= limit:
            chunks = [head]
            # Tail itself may also need splitting if it's long
            # (rare — tail = «🔗 + 📄» is ~300 chars typically).
            if len(tail) <= limit:
                chunks.append(tail)
            else:
                chunks.extend(_paragraph_greedy_split(tail, limit))
            return chunks
        # Overview itself exceeds the limit — fall through to
        # the plain greedy packer below. Operator's pinned
        # contract is best-effort: huge meetings (~50 tasks)
        # still split inside the overview, but those are rare.
    return _paragraph_greedy_split(text, limit)


def _paragraph_greedy_split(text: str, limit: int) -> list[str]:
    """Plain paragraph-boundary greedy packer. Used when the
    overview alone overflows `limit` (very long To-Do blocks)."""
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        candidate = (current + "\n\n" + p) if current else p
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            if cut < int(limit * 0.6):
                cut = limit
            chunks.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        current = p
    if current:
        chunks.append(current)
    return chunks


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


def build_meta_block_for_summary(row: MeetingRecording) -> str:
    """FR-CR-05-176 — Fireflies analogue of
    ``app.zoom.pipeline.build_meta_block_for_summary``. Renders the
    Заголовок / Дата / Продолжительность / Участники block prepended
    to the transcript for the detailed-summary LLM call.

    Participant-line precedence:
      1. ``row.calendar_attendees`` (non-empty) — rendered in event
         order using each attendee's ``resolved_name``.
      2. else ``row.participants`` (Fireflies API fallback).
      3. else the line is omitted.
    """
    meta_lines = [
        f"Заголовок: {row.title or '(без названия)'}",
        (
            f"Дата: {row.meeting_date.isoformat()}"
            if row.meeting_date
            else "Дата: —"
        ),
        (
            f"Продолжительность: {row.duration_seconds // 60} мин"
            if row.duration_seconds
            else "Продолжительность: —"
        ),
    ]
    cal = row.calendar_attendees or []
    if cal:
        names = [
            (a.get("resolved_name") or a.get("display_name")
             or a.get("email") or "").strip()
            for a in cal
            if isinstance(a, dict)
        ]
        names = [n for n in names if n]
        if names:
            meta_lines.append("Участники: " + ", ".join(names))
    elif row.participants:
        meta_lines.append("Участники: " + ", ".join(row.participants))
    return "\n".join(meta_lines)


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
        calendar_factory: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._llm = llm_backend
        self._docs_factory = docs_factory
        self._sender = sender
        # FR-CR-05-176 — optional callable returning a refreshed
        # Google Calendar credentials object. When None, the
        # `_populate_calendar_attendees` step is silently skipped
        # and the meta block falls back to the raw Fireflies
        # `participants` list (existing behaviour).
        self._calendar_factory = calendar_factory

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
            # Refresh a stale/expired audio_url. Fireflies signs the
            # download URL with a short-lived token; re-using the
            # stored one 404s on retry, so the record burns attempts
            # until permanent_failure. Mirror the Zoom fix: take the
            # fresh URL whenever it changed and we haven't downloaded
            # yet (a downloaded row skips the step, so no need then).
            if (
                t.audio_url
                and not row.audio_downloaded
                and t.audio_url != row.audio_url
            ):
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

    def _step_transcribe(
        self, row: MeetingRecording, session: Session | None = None
    ) -> bool:
        if row.transcribed and row.transcript_text:
            return True
        if not row.audio_path or not os.path.exists(row.audio_path):
            row.last_error = "audio_path missing for transcription"
            return False
        api_key = self._settings.openai_api_key
        if not api_key:
            row.last_error = "OPENAI_API_KEY not set"
            return False
        from app.services.transcription import (
            build_whisper_bias_prompt, transcribe_bytes,
            transcribe_chunks_parallel,
        )

        # FR-CR-05-127 — bias Whisper toward the operator's
        # canonical name registries (counterparties + team) so
        # brand names don't mutate in transcription («Tether» →
        # «teaser»). Empty registry → no prompt sent.
        try:
            whisper_prompt = build_whisper_bias_prompt(
                session,
                meeting_title=row.title,
                participants=row.participants,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_whisper_bias_prompt_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            whisper_prompt = None
        if whisper_prompt:
            from app.services.trace_log import trace_event

            log.info(
                "fireflies_whisper_bias_prompt_built",
                fireflies_id=row.fireflies_id,
                prompt_chars=len(whisper_prompt),
            )
            trace_event(
                source="fireflies", recording_id=row.fireflies_id,
                event="whisper_bias_prompt_built",
                prompt_chars=len(whisper_prompt),
                prompt_preview=whisper_prompt[:240],
            )

        size = os.path.getsize(row.audio_path)
        # FR-CR-05-115 — Whisper hard-limits at 25 MB. Operator:
        # «значит мне надо резать файл по 24 мб, отдельно их
        # прогонять в whisper, а потом склеивать». Chunk via
        # ffmpeg into ≤24 MB pieces, transcribe each, join.
        whisper_max = 24 * 1024 * 1024
        # FR-CR-05-177 — gpt-4o-transcribe (and -diarize) cap audio
        # at 1400 s. Only legacy whisper-1 is uncapped.
        whisper_model = (
            self._settings.fireflies_whisper_model or ""
        ).strip().lower()
        needs_dur_cap = whisper_model and whisper_model != "whisper-1"
        max_dur = 1300.0 if needs_dur_cap else None
        if size <= whisper_max and not needs_dur_cap:
            audio_paths = [row.audio_path]
        else:
            try:
                audio_paths = _split_audio_into_chunks(
                    row.audio_path,
                    max_bytes=whisper_max,
                    max_duration_seconds=max_dur,
                )
            except Exception as e:  # noqa: BLE001
                row.last_error = f"audio chunking failed: {e}"
                return False
            log.info(
                "fireflies_audio_chunked_for_whisper",
                fireflies_id=row.fireflies_id,
                size=size,
                chunks=len(audio_paths),
                max_duration_seconds=max_dur,
            )
        # FR-CR-05-146a — parallel Whisper across all chunks
        # (was sequential — operator-pinned «Whisper-чанки
        # параллельно»). Order preserved by `pool.map`, so the
        # joined transcript is still chronological.
        # FR-CR-05-177 — diarize models serialized to 1 worker.
        is_diarize_for_workers = "diarize" in (
            self._settings.fireflies_whisper_model or ""
        ).lower()
        transcript_parts = transcribe_chunks_parallel(
            audio_paths,
            openai_api_key=api_key,
            model=self._settings.fireflies_whisper_model,
            prompt=whisper_prompt,
            max_workers=1 if is_diarize_for_workers else 3,
        )
        for i, t in enumerate(transcript_parts):
            if not t:
                row.last_error = (
                    f"Whisper returned empty transcript on chunk {i+1}/"
                    f"{len(audio_paths)}"
                )
                return False
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

    # --- step 2.5: calendar-driven attendees (FR-CR-05-176) ---

    def _populate_calendar_attendees(
        self, row: MeetingRecording, session: Session,
    ) -> None:
        """FR-CR-05-176 — Fireflies counterpart of the Zoom
        FR-CR-05-169 step. Fetches the matching Google Calendar event
        for ``row.meeting_date`` and resolves its attendees against
        TeamMember / Employee / Counterparty tables. Persists on
        ``row.calendar_attendees``.

        No-op when:
          * ``row.calendar_attendees`` is already set (idempotent);
          * no Calendar credentials are configured;
          * ``row.meeting_date`` is missing;
          * Calendar API returns no events / nothing matches.

        Unlike Zoom there is NO reconcile-with-participants step —
        Fireflies doesn't expose a join-time participants endpoint.
        We trust Calendar invitees as-is.

        Never raises: caller wraps in try/except.
        """
        if row.calendar_attendees:
            return
        if self._calendar_factory is None:
            log.info(
                "fireflies_calendar_attendees_skipped_no_factory",
                fireflies_id=row.fireflies_id,
            )
            return
        if row.meeting_date is None:
            return
        from app.services.calendar_attendees import (
            resolve_calendar_attendees_for_fireflies,
        )
        from app.services.calendar_match import (
            fetch_calendar_events_via_api,
        )

        try:
            events = fetch_calendar_events_via_api(
                meeting_dt=row.meeting_date,
                window_minutes=120,
                credentials_factory=self._calendar_factory,
                calendar_id=self._settings.google_calendar_id,
            ) or []
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_calendar_events_fetch_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return
        # FR-CR-05-208 — Fireflies renames meetings, so we match the
        # calendar event by TIME proximity (closest event with attendees),
        # not title. MeetingRecording has no zoom_meeting_id → URL match is
        # skipped; title fuzzy is tried first, then the time-only fallback.
        resolved = resolve_calendar_attendees_for_fireflies(
            row, session, calendar_events=events,
        )
        attendees = (resolved or {}).get("attendees") or []
        if not attendees:
            return
        row.calendar_attendees = attendees

    # --- step 3: detailed RU summary --------------------------

    def _step_detailed_summary(
        self, row: MeetingRecording, session: Session | None = None,
    ) -> bool:
        if row.detailed_summarised and row.detailed_summary:
            return True
        if not row.transcript_text:
            row.last_error = "no transcript for detailed summary"
            return False
        # FR-CR-05-157 — same thin-transcript guard as Zoom: stop
        # before we burn LLM tokens and post «содержательная часть
        # отсутствует» to Slack/TG. Mark done so listener doesn't
        # retry.
        from app.services.transcription import is_transcript_unsummarizable

        unsumm, reason = is_transcript_unsummarizable(row.transcript_text)
        if unsumm:
            row.tasks_extracted = True
            row.last_error = None
            log.info(
                "fireflies_pipeline_skipped_thin_transcript",
                fireflies_id=row.fireflies_id, title=row.title,
                transcript_chars=len(row.transcript_text or ""),
                reason=reason,
            )
            return False
        # FR-CR-05-158 — operator-pinned: «как в зуме сделаем
        # участников». Fireflies API gives unreliable participants
        # list (often just one host email like `1@thehumanoid.ai`).
        # Run the same LLM extractor Zoom uses to recover canonical
        # team-member real_names from the transcript itself.
        try:
            from app.db import session_scope
            from app.services.team_members import as_known_employees
            from app.services.zoom_participants import (
                extract_zoom_participants_via_llm,
            )

            with session_scope() as _ps_sess:
                tm_rows = as_known_employees(_ps_sess, prefer_telegram=True)
            if tm_rows:
                llm_parts = extract_zoom_participants_via_llm(
                    transcript=row.transcript_text or "",
                    team_members=tm_rows,
                    llm_backend=self._llm,
                    model=self._settings.fireflies_summary_model,
                    reasoning_effort=(
                        self._settings.fireflies_tasks_reasoning_effort or None
                    ),
                    meeting_title=row.title,
                    trace_source="fireflies",
                    trace_recording_id=row.fireflies_id,
                )
                if llm_parts:
                    # Merge LLM-extracted real_names with the email-
                    # only participants the API returned (e.g.
                    # external attendees who aren't team members).
                    api_external = [
                        p for p in (row.participants or [])
                        if isinstance(p, str) and "@" in p
                        and not any(
                            tm.get("real_name") and tm.get("real_name") in p
                            for tm in tm_rows
                        )
                    ]
                    row.participants = llm_parts + api_external
                    log.info(
                        "fireflies_participants_resolved_via_llm",
                        fireflies_id=row.fireflies_id,
                        team_real_names=llm_parts,
                        external_kept=len(api_external),
                    )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_participants_resolve_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
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
        # FR-CR-05-176 — populate calendar_attendees before building
        # the meta block so the «Участники: …» line resolves real
        # names from People/Counterparty. Best-effort: failures fall
        # back to the raw Fireflies participants list. `session` is
        # threaded down from `process_one`; tests that exercise the
        # step in isolation can pass None and skip this enrichment.
        if session is not None:
            try:
                self._populate_calendar_attendees(row, session)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "fireflies_calendar_attendees_step_failed",
                    fireflies_id=row.fireflies_id, error=str(e),
                )
        user_prompt = (
            build_meta_block_for_summary(row)
            + "\n\nТранскрипт:\n"
            + row.transcript_text
        )
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
        row.detailed_summary = _strip_markdown_emphasis(text)
        # FR-CR-05-191 — canonicalize names (TeamMember + Counterparty)
        try:
            from app.services.summary_canonicalize import (
                canonicalize_summary_text,
            )
            new_text, applied = canonicalize_summary_text(
                row.detailed_summary,
                session=session, llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                trace_source="ff_detailed",
                trace_recording_id=row.fireflies_id,
            )
            if applied:
                row.detailed_summary = new_text
                log.info(
                    "fireflies_detailed_summary_canonicalized",
                    fireflies_id=row.fireflies_id, rewrites=applied,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_detailed_summary_canonicalize_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
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

    def _step_match_calendar_title(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-136 — replace Fireflies' auto-generated
        title with the matching Google Calendar event title
        («30/04 - US Innovative Technology / TWG Global»).

        Fetches Calendar events ±N min around `row.meeting_date`
        via the Apps Script proxy, asks an LLM to pick the best
        match against the detailed summary, then writes the
        canonical title back to `row.title`. Returns 1 on
        update, 0 on no-match / disabled / failure.
        """
        if not self._settings.calendar_match_enabled:
            return 0
        if not row.meeting_date or not row.detailed_summary:
            return 0
        # FR-CR-05-144 — prefer direct Google Calendar API when
        # `GOOGLE_CALENDAR_CLIENT_ID` is configured (operator's
        # new path); fall back to Apps Script proxy of FR-CR-05-136
        # when only that's set.
        api_factory = None
        if self._settings.google_calendar_client_id:
            from app.sync.factories import build_calendar_credentials_factory

            api_factory = build_calendar_credentials_factory(self._settings)
        if api_factory is None and not self._settings.calendar_apps_script_url:
            return 0
        from app.services.calendar_match import match_and_format_title

        try:
            new_title = match_and_format_title(
                meeting_dt=row.meeting_date,
                agenda=row.detailed_summary or "",
                window_minutes=self._settings.calendar_match_window_minutes,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                api_credentials_factory=api_factory,
                api_calendar_id=self._settings.google_calendar_id,
                apps_script_url=self._settings.calendar_apps_script_url,
                shared_token=self._settings.calendar_apps_script_shared_token,
                trace_source="fireflies",
                trace_recording_id=row.fireflies_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_calendar_match_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        if not new_title:
            log.info(
                "fireflies_calendar_match_no_match",
                fireflies_id=row.fireflies_id,
                original_title=row.title,
            )
            return 0
        # FR-CR-05-154 — push title to Fireflies' UI even if our
        # DB already has the canonical form. Operator regression:
        # a `--rerun` after a previous successful match has DB
        # title == new_title, so the early-return below WOULD
        # skip the push and Fireflies' UI stays out of sync.
        # The mutation is idempotent — doing it on every match
        # keeps Fireflies in lockstep with our DB.
        try:
            pushed = self._client.update_transcript_title(
                row.fireflies_id, new_title,
            )
            log.info(
                "fireflies_title_pushed_to_remote",
                fireflies_id=row.fireflies_id,
                title=new_title, success=pushed,
            )
            from app.services.trace_log import trace_event as _te_p
            _te_p(
                source="fireflies", recording_id=row.fireflies_id,
                event="title_pushed_to_remote",
                title=new_title, success=pushed,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_title_push_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )

        if new_title == row.title:
            return 0
        old_title = row.title
        row.title = new_title
        session.flush()
        log.info(
            "fireflies_calendar_match_title_updated",
            fireflies_id=row.fireflies_id,
            old_title=old_title, new_title=new_title,
        )
        from app.services.trace_log import trace_event as _te
        _te(source="fireflies", recording_id=row.fireflies_id,
            event="calendar_match_title_updated",
            old_title=old_title, new_title=new_title)
        return 1

    def _step_match_counterparties(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-125 / FR-CR-05-129 — TWO-PASS canonical
        counterparty resolution.

        Pass 1 (`extract_counterparty_mentions`) reads the
        transcript and lists every distinct counterparty
        mention surface form verbatim («Тезер», «Bauer/Dart»,
        «Шафлер»…) — no directory in the prompt.

        Pass 2 (`resolve_mentions_to_directory`) takes those
        mentions + the full directory and returns a
        `mention → directory_id|None` mapping. Multiple
        phonetic forms can resolve to the same id.

        The mapping is also stashed on the MeetingRecording row
        so the post-extract `_step_canonicalize_task_names`
        step can rewrite Task descriptions / titles using the
        canonical names from the directory.

        Idempotency on rerun: deletes existing mention rows for
        this `(source_kind, source_id)` first.
        """
        from app.models import Counterparty, CounterpartyMention
        from app.services.counterparty_match import (
            extract_counterparty_mentions,
            resolve_mentions_to_directory,
        )

        if not row.transcript_text:
            return 0
        directory = (
            session.query(Counterparty)
            .order_by(Counterparty.name)
            .all()
        )
        if not directory:
            log.info(
                "fireflies_counterparty_match_skipped_empty_directory",
                fireflies_id=row.fireflies_id,
            )
            return 0
        # Pass 1.
        try:
            mentions = extract_counterparty_mentions(
                row.transcript_text,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                trace_source="fireflies",
                trace_recording_id=row.fireflies_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_counterparty_extract_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        if not mentions:
            log.info(
                "fireflies_counterparty_no_mentions_in_transcript",
                fireflies_id=row.fireflies_id,
            )
            return 0
        # Pass 2.
        try:
            mention_to_id = resolve_mentions_to_directory(
                mentions,
                directory,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                batch_size=self._settings.counterparty_resolve_batch_size,
                max_workers=self._settings.counterparty_resolve_max_workers,
                trace_source="fireflies",
                trace_recording_id=row.fireflies_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_counterparty_resolve_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        # Persist mention→canonical-NAME mapping on the
        # recording (the canonicalize step rewrites by name,
        # never id, so it survives directory wipe-and-replace).
        by_id = {cp.id: cp for cp in directory}
        mention_to_canonical: dict[str, str] = {}
        canonical_norms: set[str] = set()
        for mention, cid in mention_to_id.items():
            if cid is not None and cid in by_id:
                cp = by_id[cid]
                mention_to_canonical[mention] = cp.name
                if cp.name_normalised:
                    canonical_norms.add(cp.name_normalised)
        if not hasattr(row, "_fr_canonical_map"):
            row.__dict__["_fr_canonical_map"] = mention_to_canonical
        # FR-CR-05-133 — stash unresolved mentions on the row so
        # the post-match `_step_enroll_unresolved` can post the
        # «Track this entity?» widget without re-running Pass 1
        # / Pass 2.
        unresolved = [
            mention for mention, cid in mention_to_id.items()
            if cid is None
        ]
        row.__dict__["_fr_unresolved_mentions"] = unresolved

        # FR-CR-05-129 follow-up — RACE FIX: Pass-2 may take
        # several minutes (high reasoning + 502 retries). The
        # listener's auto-pull (every 5 min) wipes-and-replaces
        # the directory mid-flight, invalidating the snapshot's
        # ids. We re-fetch FRESH ids by `name_normalised` right
        # before insert and skip any whose hub no longer exists.
        from app.models import Counterparty as _Counterparty
        fresh = (
            session.query(_Counterparty.id, _Counterparty.name_normalised)
            .filter(_Counterparty.name_normalised.in_(canonical_norms))
            .all()
        )
        norm_to_fresh_id: dict[str, int] = {n: i for i, n in fresh}

        # Replace existing CounterpartyMention rows (idempotent rerun).
        session.query(CounterpartyMention).filter(
            CounterpartyMention.source_kind == "fireflies",
            CounterpartyMention.source_id == row.fireflies_id,
        ).delete()
        session.flush()
        unique_ids: set[int] = set()
        skipped_stale = 0
        for mention, cid in mention_to_id.items():
            if cid is None or cid not in by_id:
                continue
            cp = by_id[cid]
            fresh_id = norm_to_fresh_id.get(cp.name_normalised or "")
            if fresh_id is None:
                skipped_stale += 1
                continue
            if fresh_id in unique_ids:
                continue
            unique_ids.add(fresh_id)
            session.add(
                CounterpartyMention(
                    counterparty_id=fresh_id,
                    source_kind="fireflies",
                    source_id=row.fireflies_id,
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.flush()
        log.info(
            "fireflies_counterparty_match_done",
            fireflies_id=row.fireflies_id,
            mentions=len(mentions),
            matched=len(unique_ids),
            skipped_stale_after_pull_race=skipped_stale,
        )
        return len(unique_ids)

    def _step_enroll_unresolved(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-133 — for every unresolved counterparty
        mention surfaced by `_step_match_counterparties` (Pass 2
        returned `directory_id is None`), post a stage-1 widget
        «Track «<name>»? [Yes] [No]» to each admin recipient's
        Telegram DM. Idempotent on rerun via UNIQUE constraint
        on `(source_kind, source_id, mention_normalised,
        user_id)`.

        Returns the number of widgets actually posted (excludes
        skipped-existing + send failures).
        """
        unresolved = (
            row.__dict__.get("_fr_unresolved_mentions") or []
        )
        if not unresolved or self._sender is None or not getattr(
            self._sender, "enabled", False
        ):
            return 0
        from app.telegram_bot.handlers import admin_user_ids

        recipients_raw = sorted(admin_user_ids())
        recipient_ids: list[int] = []
        for uid in recipients_raw:
            try:
                recipient_ids.append(int(uid))
            except (TypeError, ValueError):
                continue
        if not recipient_ids:
            return 0

        # FR-CR-05-138 — switched from per-entity yes/no
        # widgets to a single batch multi-select widget.
        from app.services.counterparty_enrollment_batch import (
            post_enrollment_batch,
        )

        result = post_enrollment_batch(
            session,
            sender=self._sender,
            source_kind="fireflies",
            source_id=row.fireflies_id,
            meeting_title=row.title or "",
            unresolved_mentions=unresolved,
            recipient_user_ids=recipient_ids,
        )
        return result.batches_created

    def _step_canonicalize_task_names(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-129 / FR-CR-05-130 — rewrite Task
        title/description so every counterparty mention uses
        the canonical name from the directory.

        FR-CR-05-130 — operator-pinned: «мне надо без regexp
        это делать, а как универсальное решение». The previous
        regex+SequenceMatcher fuzzy fallback couldn't handle
        every phonetic variant universally («Felix CapitalG»,
        «Jamal/Jabal» composite tokens, multi-word names). We
        now run a 3rd LLM pass: feed the task list + directory,
        get back canonicalised title/description per task. The
        LLM with reasoning handles every form the prior passes
        missed without per-case regex band-aids.
        """
        from app.models import Counterparty
        from app.services.counterparty_match import (
            canonicalize_task_content_via_llm,
        )

        tasks = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .filter(Task.deleted_at.is_(None))
            .all()
        )
        if not tasks:
            return 0
        directory = session.query(Counterparty).all()
        if not directory:
            return 0
        task_dicts = [
            {"id": t.id, "title": t.title, "description": t.description}
            for t in tasks
        ]
        try:
            rewrites_map = canonicalize_task_content_via_llm(
                task_dicts,
                directory,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                trace_source="fireflies",
                trace_recording_id=row.fireflies_id,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_canonicalize_tasks_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        applied = 0
        by_id = {t.id: t for t in tasks}
        for tid, ch in rewrites_map.items():
            t = by_id.get(tid)
            if not t:
                continue
            if "title" in ch:
                t.title = ch["title"]
            if "description" in ch:
                t.description = ch["description"]
            applied += 1
        if applied:
            session.flush()
            log.info(
                "fireflies_task_canonical_rewrite_applied",
                fireflies_id=row.fireflies_id,
                applied=applied,
            )
        return applied

    def _step_consolidate_tasks(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-131 — LLM consolidation pass: merges
        sequential phases of one action and splits composite
        topics. Returns count of tasks AFTER consolidation
        (input_count - merged + split). Soft-deletes merged-
        away rows; updates kept rows; inserts new rows from
        splits.

        Operator-pinned (FR-CR-05-131 — «без regexp,
        универсально»). Replaces the per-case dedupe band-aids
        with a single LLM pass that the operator can refine
        via prompt instead of code-touching.
        """
        from app.services.counterparty_match import (
            consolidate_tasks_via_llm,
        )

        from datetime import datetime as _dt, timezone as _tz

        tasks = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        if len(tasks) < 2:
            return len(tasks)
        task_dicts = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "owner": t.owner_user_id,
                "owner_display_name": t.owner_display_name,
                "priority": t.priority.value if t.priority else "medium",
            }
            for t in tasks
        ]
        try:
            result = consolidate_tasks_via_llm(
                task_dicts,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                trace_source="fireflies",
                trace_recording_id=row.fireflies_id,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_consolidate_tasks_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return len(tasks)
        if not result:
            return len(tasks)
        by_id = {t.id: t for t in tasks}
        kept_ids: set[int] = set()
        from app.models import TaskPriority
        for entry in result:
            merged_from = entry.get("merged_from") or []
            new_id = entry.get("id")
            if new_id is not None and new_id in by_id and len(merged_from) <= 1:
                # Single in-place update.
                t = by_id[new_id]
                if entry.get("title"):
                    t.title = entry["title"][:10_000]
                if entry.get("description"):
                    t.description = entry["description"]
                kept_ids.add(t.id)
                continue
            # Merge or split — keep the FIRST source row, update
            # it, soft-delete the rest.
            primary_id = merged_from[0] if merged_from else None
            if primary_id and primary_id in by_id:
                t = by_id[primary_id]
                if entry.get("title"):
                    t.title = entry["title"][:10_000]
                if entry.get("description"):
                    t.description = entry["description"]
                try:
                    t.priority = TaskPriority(entry.get("priority") or "medium")
                except ValueError:
                    pass
                kept_ids.add(t.id)
            else:
                # Pure split with no anchor — copy fields from
                # FIRST original task that gave context.
                # Skipped — LLM should always set merged_from.
                continue
        # Soft-delete tasks NOT in kept_ids (merged away).
        soft_deleted = 0
        now = _dt.now(_tz.utc)
        for t in tasks:
            if t.id not in kept_ids:
                t.deleted_at = now
                soft_deleted += 1
        if kept_ids or soft_deleted:
            session.flush()
            log.info(
                "fireflies_task_consolidate_applied",
                fireflies_id=row.fireflies_id,
                input=len(tasks),
                kept=len(kept_ids),
                soft_deleted=soft_deleted,
            )
        return len(kept_ids)

    def _step_doc_export(
        self, session: Session, row: MeetingRecording
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
        title = row.title or f"Meeting {row.fireflies_id}"
        # FR-CR-05-119 follow-up — append the full task list to
        # the doc body. FR-CR-05-125 — also append the matched
        # counterparties block. Tasks first (operator's primary
        # action items), counterparties below (reference).
        body = row.detailed_summary
        cp_section = _build_counterparties_section_for_doc(
            session,
            source_kind="fireflies",
            source_id=row.fireflies_id,
        )
        if cp_section:
            body = body.rstrip() + "\n\n" + cp_section
        tasks_section = _build_full_tasks_section_for_doc(
            session,
            source_kind=TaskSourceKind.fireflies,
            source_conversation_id=row.fireflies_id,
        )
        if tasks_section:
            body = body.rstrip() + "\n\n" + tasks_section
        try:
            doc_id, url = docs.export_summary(
                title=title,
                body=body,
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

    def _step_short_summary(
        self, session: Session, row: MeetingRecording
    ) -> bool:
        if row.short_summary:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary as short-summary input"
            return False
        # FR-CR-05-183 — calendar_attendees authoritative for the
        # «Участники:» line. Falls back to Fireflies' raw API
        # participants only when no calendar match (or empty
        # attendees) — never to LLM-from-transcript guesses, which
        # hallucinate teammates merely mentioned in speech.
        cal_attendees_names: list[str] = []
        for a in (row.calendar_attendees or []):
            if not isinstance(a, dict):
                continue
            nm = (
                a.get("resolved_name") or a.get("display_name")
                or a.get("email") or ""
            ).strip()
            if nm:
                cal_attendees_names.append(nm)
        if cal_attendees_names:
            effective_participants = cal_attendees_names
        else:
            effective_participants = list(row.participants or [])
        # FR-CR-05-200 — нормализуем участников: резолвим email-адреса в имена
        # тиммейтов и дедупим. Сырой Fireflies-список часто несёт и display-name,
        # и account-email одного человека (напр. «Артем Соколов» + «1@thehumanoid.ai»).
        from app.agenda.service import _build_email_to_name_map

        _e2n = _build_email_to_name_map(session)
        _seen: set[str] = set()
        _norm: list[str] = []
        for p in effective_participants:
            pp = (p or "").strip()
            if not pp:
                continue
            if "@" in pp:
                pp = _e2n.get(pp.lower(), pp)
            key = pp.lower()
            if key in _seen:
                continue
            _seen.add(key)
            _norm.append(pp)
        effective_participants = _norm
        participants_block = "\n".join(
            f"  - {p}" for p in effective_participants if p
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
        # FR-CR-05-157 follow-up — same no-content guard as Zoom.
        from app.services.transcription import is_summary_no_content

        no_content, hit_phrase = is_summary_no_content(text)
        if no_content:
            row.tasks_extracted = True
            row.last_error = None
            log.info(
                "fireflies_pipeline_skipped_no_content_summary",
                fireflies_id=row.fireflies_id, title=row.title,
                hit_phrase=hit_phrase, summary_chars=len(text),
            )
            return False
        body = _truncate(text, limit=3800)
        # FR-CR-05-191 — canonicalize names against TeamMember +
        # Counterparty directories before the post-processing chain.
        try:
            from app.services.summary_canonicalize import (
                canonicalize_summary_text,
            )
            new_body, applied = canonicalize_summary_text(
                body,
                session=session, llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                trace_source="ff_short",
                trace_recording_id=row.fireflies_id,
            )
            if applied:
                body = new_body
                log.info(
                    "fireflies_short_summary_canonicalized",
                    fireflies_id=row.fireflies_id, rewrites=applied,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_short_summary_canonicalize_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-119 — strip any «To-Do» / «Следующие шаги»
        # block the LLM still emits despite the prompt forbidding
        # it. We rebuild the section deterministically from the
        # actual extracted tasks below.
        body = _strip_llm_todo_block(body)
        # FR-CR-05-119 — append To-Do built from the just-extracted
        # Task rows. Description (verbatim what the LLM wrote on
        # the Task row) + owner_display_name in parens. If no
        # tasks were extracted we drop the section.
        # FR-CR-05-128 follow-up — operator pinned: «не надо всё
        # вмещать в одно сообщение, если не вмещается, то след
        # сообщение». Verbose To-Do always; the splitter chunks
        # at paragraph boundaries (Header+Участники+Суть → chunk
        # 1, To-Do → chunk 2 when it overflows).
        todo = _build_todo_section(
            session, source_kind=TaskSourceKind.fireflies,
            source_conversation_id=row.fireflies_id,
        )
        if todo:
            body = body.rstrip() + "\n\n" + todo
        # FR-CR-05-129 follow-up — operator-pinned: «не пиши
        # 'контрагенты' в коротком сообщении». The 🔗
        # Контрагенты row is removed; the canonical names are
        # already in the task topic-prefixes via
        # `_step_canonicalize_task_names`, so listing them
        # again is duplication.
        # FR-CR-05-127 — operator-pinned: the «DD/MM - <Topic>»
        # header becomes an HTML hyperlink to the Google Doc.
        # Replaces the old «📄 Подробный отчёт: <url>» trailer
        # line so the doc-link is on the title itself and the
        # body looks cleaner. Sent with parse_mode=HTML
        # (sender's default).
        # FR-CR-05-156 — first line MUST be the raw meeting title
        # (operator: «такие же названия тайтлов как в самих встречах»),
        # prefixed with the `DD/MM` of the meeting date.
        body = _force_meeting_title_first_line(
            body, row.title or "", row.meeting_date,
        )
        if row.google_doc_url:
            body = _wrap_short_summary_with_doc_link(
                body.rstrip(), row.google_doc_url,
            )
        row.short_summary = body
        row.last_error = None
        return True

    # --- step 6: send short summary to TG admins --------------

    def _step_send_short_summary(self, row: MeetingRecording) -> int:
        if row.short_summary_sent:
            return 0
        # FR-CR-05-167 polish 2026-05-15: send-time host filter.
        # `meeting_recordings` doesn't carry an explicit host column,
        # so we infer host from `participants[0]` (Fireflies API
        # returns participants ordered with the host first). When
        # the strict-host env-flag is on and the operator email is
        # NOT the first participant, skip the summary delivery —
        # the recording was hosted by somebody else and shouldn't
        # surface in the operator's DM.
        strict = bool(
            getattr(self._settings, "zoom_required_email_strict_host", False)
        )
        required = (
            getattr(self._settings, "zoom_required_email", "") or ""
        ).strip().lower()
        if strict and required:
            emails: list[str] = []
            for p in row.participants or []:
                if isinstance(p, str):
                    emails.append(p.strip().lower())
                elif isinstance(p, dict):
                    e = (p.get("email") or "").strip().lower()
                    if e:
                        emails.append(e)
            host_email = emails[0] if emails else ""
            if host_email and host_email != required:
                log.info(
                    "fireflies_step_send_short_summary_skipped_other_host",
                    fireflies_id=row.fireflies_id,
                    host=host_email, required=required, title=row.title,
                    hint=(
                        "strict host filter (FR-CR-05-167) — first "
                        "participant is not the operator"
                    ),
                )
                row.short_summary_sent = True
                return 0
        if not row.short_summary or self._sender is None or not getattr(self._sender, "enabled", False):
            return 0
        from app.telegram_bot.handlers import admin_user_ids

        recipients = sorted(admin_user_ids())
        # FR-CR-05-119 — split into Telegram-sized chunks because
        # the deterministic To-Do section can grow past the
        # 4096-char per-message limit (operator regression: 25
        # tasks → 10 KB body). Each chunk goes as a separate DM.
        chunks = _split_for_telegram(row.short_summary, limit=4096)
        sent = 0
        for uid in recipients:
            try:
                uid_int = int(uid)
            except ValueError:
                continue
            uid_sent = 0
            for chunk in chunks:
                try:
                    resp = self._sender.send_message(
                        chat_id=uid_int, text=chunk,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "fireflies_short_summary_send_failed",
                        uid=uid, error=str(e),
                    )
                    break
                if resp and resp.get("message_id"):
                    uid_sent += 1
                else:
                    break
            if uid_sent == len(chunks):
                sent += 1
        if sent:
            row.short_summary_sent = True
        # FR-CR-05-158 — operator-pinned: «в слаке я не вижу
        # firefiles». Fireflies pipeline never mirrored to Slack
        # (Zoom did via FR-CR-05-137). Add the same call here.
        # Failures MUST NOT break the pipeline — wrapped in try/except.
        try:
            from app.services.slack_mirror import (
                post_meeting_summary_to_slack,
            )
            channel = self._settings.slack_meeting_channel_id
            token = self._settings.slack_bot_token
            if channel and token and (row.short_summary or "").strip():
                resps = post_meeting_summary_to_slack(
                    slack_token=token, channel_id=channel,
                    body=row.short_summary or "",
                )
                posted = sum(
                    1 for r in (resps or [])
                    if isinstance(r, dict) and r.get("ok")
                )
                log.info(
                    "fireflies_slack_mirror_posted",
                    fireflies_id=row.fireflies_id, channel_id=channel,
                    chunks_posted=posted,
                    body_chars=len(row.short_summary or ""),
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_slack_mirror_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-160 — mirror to external webhook (n8n).
        try:
            from app.services.meeting_webhook import post_meeting_to_webhook

            url = self._settings.meeting_webhook_url
            if url and (row.short_summary or "").strip():
                post_meeting_to_webhook(
                    webhook_url=url,
                    source="fireflies",
                    source_id=row.fireflies_id,
                    title=row.title,
                    meeting_date=row.meeting_date,
                    duration_seconds=row.duration_seconds,
                    short_summary=row.short_summary,
                    detailed_summary=row.detailed_summary,
                    google_doc_url=row.google_doc_url,
                    participants=list(row.participants or []),
                    tasks_count=row.tasks_extracted_count,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_meeting_webhook_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        return sent

    # --- step 7: task extraction -----------------------------

    def _step_extract_tasks(
        self, session: Session, row: MeetingRecording
    ) -> int:
        if row.tasks_extracted:
            return row.tasks_extracted_count or 0
        if not row.transcript_text:
            return 0
        # FR-CR-05-129 — operator regression: rerun reset
        # `tasks_extracted=False` flag but the previous run's
        # Task rows stayed in DB, so each rerun stacks tasks
        # (87 → 145 → 200+). Soft-delete prior tasks for this
        # meeting before re-extracting.
        from datetime import datetime as _dt, timezone as _tz
        prior = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .filter(Task.deleted_at.is_(None))
            .all()
        )
        for t in prior:
            t.deleted_at = _dt.now(_tz.utc)
        if prior:
            session.flush()
            log.info(
                "fireflies_extract_wiped_prior_tasks",
                fireflies_id=row.fireflies_id,
                wiped=len(prior),
            )
        from app.services.team_members import as_known_employees

        try:
            known_employees = as_known_employees(session, prefer_telegram=True)
        except Exception as e:  # noqa: BLE001
            log.info("fireflies_team_registry_unavailable", error=str(e))
            known_employees = []
        # FR-CR-05-139 — render participants as a prominent block
        # so the LLM can disambiguate identical first names («Дима
        # Дроздов» vs «Дмитрий Седов») via Rule 8.
        # FR-CR-05-145 — Python-side defense: drop teammates whose
        # notes forbid this meeting's topic («не участвует в
        # Fundrising sync» / «не вести fundraising-задачи»).
        from app.services.team_members import (
            filter_participants_by_notes_forbid,
            infer_topic_keywords_from_text,
        )

        _ff_topic_kw = infer_topic_keywords_from_text(
            " ".join([
                row.title or "",
                (row.detailed_summary or "")[:3000],
                (row.transcript_text or "")[:1500],
            ])
        )
        _ff_filtered_participants, _ff_dropped = filter_participants_by_notes_forbid(
            list(row.participants or []),
            known_employees=[
                {
                    "real_name": (e.get("real_name") or "").strip(),
                    "notes": e.get("notes") or "",
                }
                for e in known_employees
            ],
            topic_keywords=_ff_topic_kw,
        )
        if _ff_dropped:
            log.info(
                "fireflies_participants_post_filter_applied",
                fireflies_id=row.fireflies_id,
                topic_keywords=_ff_topic_kw,
                dropped=_ff_dropped,
                kept=_ff_filtered_participants,
            )
        participants_lines = (
            "\n".join(f"  - {p}" for p in _ff_filtered_participants if p)
            or "  (нет данных)"
        )
        # FR-CR-05-185 — provide today_date so the LLM can resolve
        # relative deadlines ("завтра", "в понедельник") into ISO
        # `YYYY-MM-DD` for the `due_date` field on each task.
        meta = (
            f"meeting_title: {row.title or ''}\n"
            f"meeting_date: {row.meeting_date.isoformat() if row.meeting_date else ''}\n"
            f"today_date: {date.today().isoformat()}\n"
            f"\nmeeting_participants (REAL NAMES of who was on this call,\n"
            f"use to disambiguate identical first names — Rule 8):\n"
            f"{participants_lines}\n"
        )
        user_prompt = (
            "known_employees (pick a slack_user_id from this table):\n"
            + _render_known_employees_table(known_employees)
            + "\n\n"
            + meta
            + "\nТранскрипт встречи:\n"
            + row.transcript_text
        )
        # FR-CR-05-129 — switch to JSON-mode (complete_text +
        # response_format) instead of call_tool, mirroring the
        # counterparty matcher's switch. Avoids the gpt-5.5
        # «reasoning_effort + function tools» 400 in chat/
        # completions; retry-without-reasoning was returning
        # `raw_count=0` because the LLM without reasoning is
        # too shallow for granular task extraction.
        user_prompt = (
            "Return JSON: `{\"tasks\": [{\"title\": ..., "
            "\"description\": ..., \"owner\": ..., "
            "\"priority\": ..., \"due_date\": "
            "\"YYYY-MM-DD or omit\", \"due_time\": "
            "\"HH:MM or omit\"}, ...]}`. Empty list ok.\n\n"
            + user_prompt
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=TASK_EXTRACTION_SYSTEM,
                user_prompt=user_prompt,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
                response_format={"type": "json_object"},
            ) or ""
        except Exception as e:  # noqa: BLE001
            row.last_error = f"task extraction LLM failed: {e}"
            log.warning(
                "fireflies_task_extraction_llm_failed",
                fireflies_id=row.fireflies_id,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=self._settings.fireflies_tasks_reasoning_effort,
                error=str(e),
            )
            return 0
        # FR-CR-05-129 — parse JSON response (was tool result).
        try:
            import json as _json
            result = _json.loads(text) if text else {}
        except _json.JSONDecodeError:
            log.warning(
                "fireflies_task_extraction_json_parse_failed",
                fireflies_id=row.fireflies_id,
                text_preview=text[:200],
            )
            result = {}
        tasks = (result or {}).get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        # FR-CR-05-126 — full trace of what the LLM emitted so
        # the operator can sanity-check «meeting was procedural»
        # vs «model misfired» without re-running.
        from app.services.trace_log import trace_event as _te

        _raw_titles = [
            (t.get("title") or "")[:80]
            for t in tasks if isinstance(t, dict)
        ][:25]
        _raw_owners = [
            t.get("owner") for t in tasks if isinstance(t, dict)
        ][:25]
        log.info(
            "fireflies_task_extraction_llm_returned",
            fireflies_id=row.fireflies_id,
            model=self._settings.fireflies_tasks_model,
            raw_count=len(tasks),
            raw_titles=_raw_titles,
            raw_owners=_raw_owners,
        )
        _te(source="fireflies", recording_id=row.fireflies_id,
            event="task_extraction_llm_returned",
            model=self._settings.fireflies_tasks_model,
            raw_count=len(tasks),
            raw_titles=_raw_titles, raw_owners=_raw_owners)
        if not tasks:
            log.info(
                "fireflies_task_extraction_returned_empty",
                fireflies_id=row.fireflies_id,
                model=self._settings.fireflies_tasks_model,
                detailed_chars=len(row.detailed_summary or ""),
                hint=(
                    "either the meeting was procedural or the "
                    "LLM call returned []. Check last_error + "
                    "model name (FIREFLIES_TASKS_MODEL)."
                ),
            )
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
            if not owner_user_id:
                # FR-CR-05-142a — never emit owner=null. Cascade
                # to principal participant; never to admin/AI Lead
                # (FR-CR-05-134). Operator-pinned: «есть задачи
                # без ответственных / такого быть не может!
                # всегда ответственный должен быть».
                from app.services.team_members import (
                    infer_topic_keywords_from_text,
                    pick_meeting_owner_fallback,
                )

                topic_text = " ".join(
                    [
                        row.title or "",
                        title or "",
                        (description or "")[:300],
                    ]
                )
                fb = pick_meeting_owner_fallback(
                    known_employees=known_employees,
                    participants_real_names=list(row.participants or []),
                    topic_keywords=infer_topic_keywords_from_text(topic_text),
                )
                if fb:
                    owner_user_id = fb
                    owner_resolution = (
                        "fallback_principal"
                        if llm_owner_raw is None
                        else owner_resolution + "_fallback_principal"
                    )
                else:
                    owner_resolution = (
                        "fallback_no_participants"
                        if llm_owner_raw is None
                        else owner_resolution + "_fallback_no_participants"
                    )
            # FR-CR-05-192r — apply delegate marker if the resolved
            # owner carries DELEGATE_TASKS_TO in their TM notes.
            # Operator-pinned 2026-05-22: «на артема не ставить, на
            # ирину». The chain is single-hop and gracefully no-ops
            # when the delegate target isn't in known_employees.
            from app.services.team_members import apply_delegate_marker

            new_owner_user_id, delegate_name = apply_delegate_marker(
                owner_user_id, known_employees,
            )
            if delegate_name and new_owner_user_id != owner_user_id:
                log.info(
                    "fireflies_task_owner_delegated",
                    fireflies_id=row.fireflies_id,
                    title=title[:80],
                    from_owner=owner_user_id,
                    to_owner=new_owner_user_id,
                    delegate_real_name=delegate_name,
                )
                owner_user_id = new_owner_user_id
                owner_resolution += f"_delegated_to_{delegate_name}"

            log.info(
                "fireflies_task_owner_resolved",
                fireflies_id=row.fireflies_id,
                title=title[:80],
                llm_owner=llm_owner_raw,
                final_owner=owner_user_id,
                resolution=owner_resolution,
            )
            from app.services.trace_log import trace_event as _te2
            _te2(source="fireflies", recording_id=row.fireflies_id,
                 event="task_owner_resolved", title=title[:80],
                 llm_owner=llm_owner_raw, final_owner=owner_user_id,
                 resolution=owner_resolution)
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
                # FR-CR-05-185 — let LLM emit a relative-deadline-
                # resolved ISO date. Parser falls back to today/18:00
                # when the model omits or emits an unparseable value.
                from app.services.task_due import (
                    parse_due_date_from_llm,
                    parse_due_time_from_llm,
                )

                llm_due_date = parse_due_date_from_llm(
                    t.get("due_date"), fallback=today,
                )
                llm_due_time = parse_due_time_from_llm(
                    t.get("due_time"), fallback=time(23, 59),
                )
                task_status = TaskStatus.todo  # due=today → todo per FR-CR-04
                task = Task(
                    title=title[:10_000],
                    description=description,
                    owner_user_id=owner_user_id,
                    owner_display_name=owner_display_name,
                    priority=TaskPriority(priority) if priority in {p.value for p in TaskPriority} else TaskPriority.medium,
                    due_date=llm_due_date,
                    due_time=llm_due_time,
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
                # FR-CR-05-120 follow-up — DM card posting moved
                # to a separate `_step_post_task_cards` step that
                # runs AFTER the short summary is sent (operator
                # pinned: meeting overview first, then per-task
                # cards). Tasks just sit in the session here.
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "fireflies_task_create_failed",
                    title=title[:80],
                    error=str(e),
                )
        row.tasks_extracted_count = created
        row.tasks_extracted = True
        return created

    def _step_verify_tasks(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-121 — second LLM pass to catch tasks missed
        by `_step_extract_tasks`. Reads the transcript +
        already-extracted Task rows and asks the verifier
        prompt for any newly-missed actionable items. New rows
        are added to the same session; returns count.
        Idempotency: caller short-circuits via `row.attempts`
        and the per-recording bookmarking; re-running is safe
        because the verifier is told to skip duplicates."""
        from app.fireflies.prompts import TASK_VERIFICATION_SYSTEM
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
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
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
            known_employees = as_known_employees(session, prefer_telegram=True)
        except Exception:  # noqa: BLE001
            known_employees = []
        emp_table = _render_known_employees_table(known_employees)
        user_prompt = (
            "known_employees (pick a slack_user_id from this table):\n"
            + emp_table + "\n\n"
            "Already-extracted tasks (DO NOT duplicate these):\n"
            + existing_block + "\n\n"
            "Транскрипт встречи:\n"
            + row.transcript_text
        )
        # FR-CR-05-129 — JSON-mode (same fix as extract step).
        user_prompt = (
            "Return JSON: `{\"tasks\": [{\"title\": ..., "
            "\"description\": ..., \"owner\": ..., "
            "\"priority\": ..., \"due_date\": "
            "\"YYYY-MM-DD or omit\", \"due_time\": "
            "\"HH:MM or omit\"}, ...]}`. Empty list ok.\n\n"
            + user_prompt
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=TASK_VERIFICATION_SYSTEM,
                user_prompt=user_prompt,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
                response_format={"type": "json_object"},
            ) or ""
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_verification_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        try:
            import json as _json
            result = _json.loads(text) if text else {}
        except _json.JSONDecodeError:
            log.warning(
                "fireflies_task_verification_json_parse_failed",
                fireflies_id=row.fireflies_id,
                text_preview=text[:200],
            )
            result = {}
        new_tasks = (result or {}).get("tasks") or []
        if not isinstance(new_tasks, list):
            new_tasks = []
        # FR-CR-05-126 — verifier transparency: titles of what
        # the second pass actually wants to add, before insert.
        _new_titles = [
            (t.get("title") or "")[:80]
            for t in new_tasks if isinstance(t, dict)
        ][:25]
        _new_owners = [
            t.get("owner") for t in new_tasks if isinstance(t, dict)
        ][:25]
        log.info(
            "fireflies_task_verification_done",
            fireflies_id=row.fireflies_id,
            existing_count=len(existing),
            newly_added=len(new_tasks),
            new_titles=_new_titles,
            new_owners=_new_owners,
        )
        from app.services.trace_log import trace_event as _te3
        _te3(source="fireflies", recording_id=row.fireflies_id,
             event="task_verification_done",
             existing_count=len(existing), newly_added=len(new_tasks),
             new_titles=_new_titles, new_owners=_new_owners)
        if not new_tasks:
            return 0
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        admin_uid = _admin_fallback_owner_id()
        today = date.today()
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
            description = (t.get("description") or "").strip() or None
            if description:
                description = _strip_uid_suffixes(description, valid_ids)
            priority_raw = t.get("priority") or "medium"
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
                priority = (
                    TaskPriority(priority_raw)
                    if priority_raw in {p.value for p in TaskPriority}
                    else TaskPriority.medium
                )
                # FR-CR-05-185 — same LLM-due-date parser as the
                # primary extraction step above.
                from app.services.task_due import (
                    parse_due_date_from_llm as _pdd,
                    parse_due_time_from_llm as _pdt,
                )
                llm_due_date = _pdd(t.get("due_date"), fallback=today)
                llm_due_time = _pdt(t.get("due_time"), fallback=time(23, 59))
                task = Task(
                    title=title[:10_000],
                    description=description,
                    owner_user_id=owner_uid,
                    owner_display_name=owner_display_name,
                    priority=priority,
                    due_date=llm_due_date,
                    due_time=llm_due_time,
                    status=TaskStatus.todo,
                    is_current_week=True,
                    source_kind=TaskSourceKind.fireflies,
                    source_conversation_id=row.fireflies_id,
                    source_message_ts=row.fireflies_id,
                    source_permalink=row.fireflies_share_url,
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
                        reason="fireflies_verified",
                        at=datetime.now(timezone.utc),
                    )
                )
                schedule_sync_task(session, task.id)
                added += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "fireflies_task_verify_create_failed",
                    title=title[:80], error=str(e),
                )
        if added:
            row.tasks_extracted_count = (row.tasks_extracted_count or 0) + added
        return added

    def _step_classify_directions(
        self, session: Session, row: MeetingRecording,
    ) -> None:
        """FR-CR-05-163 — classify each task by strategic direction
        (beta / budget / design / investors / deliverables / other)
        and store in task.extra.direction. Important directions get
        badged in the To-Do block to call CEO attention."""
        from app.models import Task as _Task
        from app.services.task_direction import classify_directions

        tasks = (
            session.query(_Task)
            .filter(_Task.source_kind == TaskSourceKind.fireflies)
            .filter(_Task.source_conversation_id == row.fireflies_id)
            .filter(_Task.deleted_at.is_(None))
            .all()
        )
        if not tasks:
            return
        tasks_to_classify = [
            {"id": t.id, "title": t.title or "", "description": t.description or ""}
            for t in tasks
            if not (isinstance(t.extra, dict) and t.extra.get("direction"))
        ]
        if not tasks_to_classify:
            return
        mapping = classify_directions(
            tasks=tasks_to_classify,
            meeting_context=(row.detailed_summary or "")[:3000] or None,
            llm_backend=self._llm,
            model=self._settings.fireflies_tasks_model,
        )
        for t in tasks:
            direction = mapping.get(t.id, "other")
            extra = dict(t.extra or {})
            extra["direction"] = direction
            t.extra = extra
        log.info(
            "fireflies_task_directions_applied",
            fireflies_id=row.fireflies_id,
            classified=len(mapping),
            total=len(tasks),
        )

    def _step_post_task_cards(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-120 follow-up — post DM card per extracted
        Task ROW. Runs AFTER `_step_send_short_summary` so the
        operator gets the overview first, then per-task cards.
        Returns count of cards successfully posted.

        FR-CR-05-146d — parallel posting via thread-pool, max 10
        concurrent (Telegram allows 30 msg/sec across chats).
        Each thread opens its own session via `session_scope()`.
        """
        if (
            self._sender is None
            or not getattr(self._sender, "enabled", False)
        ):
            return 0
        from concurrent.futures import ThreadPoolExecutor

        from app.db import session_scope
        from app.telegram_bot.cards import post_initial_card

        admin_uid = _admin_fallback_owner_id()
        task_ids = [
            t.id for t in (
                session.query(Task.id)
                .filter(Task.source_kind == TaskSourceKind.fireflies)
                .filter(Task.source_conversation_id == row.fireflies_id)
                .filter(Task.deleted_at.is_(None))
                .order_by(Task.id.asc())
                .all()
            )
        ]
        if not task_ids:
            return 0

        sender = self._sender

        def _send_one(task_id: int) -> bool:
            with session_scope() as s:
                t = s.query(Task).filter(Task.id == task_id).first()
                if t is None:
                    return False
                try:
                    post_initial_card(
                        sender=sender,
                        session=s,
                        task=t,
                        chat_id=0,
                        reply_to_message_id=None,
                        author_user_id=admin_uid,
                    )
                    return True
                except Exception as e:  # noqa: BLE001
                    log.info(
                        "fireflies_task_card_post_failed",
                        task_id=task_id, error=str(e),
                    )
                    return False

        workers = max(1, min(10, len(task_ids)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ff-cards",
        ) as pool:
            results = list(pool.map(_send_one, task_ids))
        return sum(1 for r in results if r)

    # --- FR-CR-05-194: auto-Slack-publish step ----------------

    def _step_send_to_slack(self, session, row) -> None:
        """FR-CR-05-194a — симметрично Zoom (см. ZoomPipeline)."""
        try:
            from app.services.slack_publish import maybe_auto_publish
            result = maybe_auto_publish(session, row, settings=self._settings)
            if result is None:
                return
            if not result.get("ok"):
                log.warning("fireflies_step_send_to_slack_failed",
                            fireflies_id=getattr(row, "fireflies_id", None),
                            error=result.get("error"),
                            step=result.get("step"))
            else:
                log.info("fireflies_step_send_to_slack_done",
                         fireflies_id=getattr(row, "fireflies_id", None),
                         parent_ts=result.get("parent_ts"),
                         tasks_posted=result.get("tasks_posted"))
                # Persist slack_post_ts NOW — the Slack post is an
                # irreversible side-effect, but the row commits only on
                # session_scope exit. A restart in between rolls back
                # slack_post_ts while the message stays in Slack → next
                # run re-posts (duplicate). Commit immediately.
                if result.get("parent_ts") and not result.get("skipped_reason"):
                    try:
                        session.commit()
                    except Exception as ce:  # noqa: BLE001
                        session.rollback()
                        log.warning("fireflies_send_to_slack_commit_failed",
                                    fireflies_id=getattr(row, "fireflies_id", None),
                                    error=str(ce))
        except Exception as e:  # noqa: BLE001
            log.warning("fireflies_step_send_to_slack_exception",
                        fireflies_id=getattr(row, "fireflies_id", None),
                        error=str(e))

    # --- FR-CR-05-193g-3: новый объединённый step ------------

    def _step_extract_via_reasoning(self, session, row) -> None:
        """FR-CR-05-193g-3 — симметрично Zoom (см. ZoomPipeline).
        Reasoning extract + matcher + apply в одном step'е.
        Idempotent через row.extracted_via_reasoning.

        Skeleton method — full integration в следующем коммите.
        """
        import os
        if not row or getattr(row, "extracted_via_reasoning", False):
            if not getattr(row, "last_error", None):
                return
        enabled = os.environ.get(
            "ENTITY_RESOLUTION_V2_ENABLED", "true"
        ).strip().lower() not in ("false", "0", "no", "off")
        if not enabled:
            return
        log.info("fireflies_step_extract_via_reasoning_stub",
                 fireflies_id=getattr(row, "fireflies_id", None))

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
        # FR-CR-05-198 — Fireflies hasn't published the audio yet: no
        # audio_url and nothing downloaded before. Transient not-ready
        # state, NOT a failure — return early WITHOUT incrementing attempts
        # so polling never burns the retry cap waiting for the recording.
        already_have_audio = bool(
            row.audio_downloaded and row.audio_path and os.path.exists(row.audio_path)
        )
        if not row.audio_url and not already_have_audio:
            report.skipped_reason = "waiting_for_audio"
            log.info(
                "fireflies_pipeline_waiting_for_audio",
                fireflies_id=row.fireflies_id,
                attempts=row.attempts or 0,
            )
            return report
        # FR-CR-05-196 — runaway retry cap.
        if (row.attempts or 0) >= MAX_ATTEMPTS_BEFORE_GIVE_UP:
            row.last_error = "permanent_failure_attempts_exceeded"
            report.skipped_reason = "permanent_failure_attempts_exceeded"
            log.info(
                "fireflies_pipeline_skipped_permanent_failure",
                fireflies_id=row.fireflies_id,
                attempts=row.attempts,
                cap=MAX_ATTEMPTS_BEFORE_GIVE_UP,
            )
            return report
        # FR-CR-05-203 — cross-source Zoom↔Fireflies dedup (symmetric to
        # Zoom). If the same meeting was already posted by the other source
        # (Zoom cloud or another FF row), skip this capture entirely —
        # don't download / transcribe / post. First-to-post wins.
        from app.services import meeting_dedup

        _dup = meeting_dedup.find_cross_source_duplicate(
            session, title=row.title, meeting_date=row.meeting_date,
            self_kind="fireflies", self_id=row.fireflies_id,
        )
        if _dup:
            report.skipped_reason = "duplicate_other_source"
            log.info(
                "fireflies_pipeline_skipped_duplicate_other_source",
                fireflies_id=row.fireflies_id, dup_kind=_dup[0], dup_id=_dup[1],
            )
            return report
        # FR-CR-05-192t — operator-pinned 2026-05-22 min-duration
        # gate: skip meetings shorter than `min_meeting_seconds`
        # (default 300 = 5 min). Procedural / aborted-call recordings
        # waste LLM budget and produce no useful actionables.
        min_secs = getattr(self._settings, "min_meeting_seconds", 300)
        if min_secs and (row.duration_seconds or 0) < min_secs:
            report.skipped_reason = "duration_too_short"
            log.info(
                "fireflies_pipeline_skipped_duration_too_short",
                fireflies_id=row.fireflies_id,
                duration_seconds=row.duration_seconds,
                threshold=min_secs,
            )
            return report
        row.attempts += 1
        # FR-CR-05-122 — every step is wrapped in `_trace_step`
        # so the listener log shows started/done bookends with
        # `duration_ms`. Grep `fireflies_step_(started|done|failed)`
        # to walk through a single recording's run.
        ctx = {"fireflies_id": row.fireflies_id}

        with _trace_step("fireflies", "download", **ctx):
            if not self._step_download_audio(row):
                session.flush()
                report.errors.append(row.last_error or "download_failed")
                return report
        with _trace_step("fireflies", "transcribe", **ctx):
            if not self._step_transcribe(row, session=session):
                session.flush()
                report.errors.append(row.last_error or "transcribe_failed")
                return report
        report.transcript_chars = len(row.transcript_text or "")
        with _trace_step("fireflies", "detailed_summary", **ctx):
            if not self._step_detailed_summary(row, session=session):
                session.flush()
                report.errors.append(row.last_error or "detailed_summary_failed")
                return report
        report.detailed_chars = len(row.detailed_summary or "")
        # FR-CR-05-136 — replace the Fireflies-supplied title
        # with the matching Google Calendar event title (via
        # Apps Script proxy). Failures NEVER cascade.
        try:
            with _trace_step("fireflies", "match_calendar_title", **ctx):
                self._step_match_calendar_title(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_calendar_match_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-125 — match counterparty mentions against the
        # canonical directory before doc/summary generation so
        # both surfaces can render the «🔗 Контрагенты» block.
        try:
            with _trace_step("fireflies", "match_counterparties", **ctx):
                self._step_match_counterparties(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_counterparty_match_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-133 — for every unresolved mention surfaced
        # by Pass 2, post a «Track this entity?» widget to each
        # admin recipient. Failures here MUST NOT break the
        # rest of the pipeline (the meeting summary still goes
        # out even if enrollment fails entirely).
        try:
            with _trace_step("fireflies", "enroll_unresolved", **ctx):
                self._step_enroll_unresolved(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_enroll_unresolved_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-119 follow-up: extract tasks FIRST, then doc
        # with full task list, then short with compressed.
        with _trace_step("fireflies", "extract_tasks", **ctx):
            report.tasks_created = self._step_extract_tasks(session, row)
        # FR-CR-05-121 — verifier pass.
        try:
            with _trace_step("fireflies", "verify_tasks", **ctx):
                self._step_verify_tasks(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_verification_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-129 — canonicalize task descriptions /
        # titles using the Pass-2 mention→directory mapping
        # stashed on row by `_step_match_counterparties`. So
        # «Teaser - …» / «Тезер - …» / «Tezer - …» all become
        # «Tether - …», and the dedupe step (next) collapses
        # the now-identical topic prefixes.
        try:
            with _trace_step("fireflies", "canonicalize_task_names", **ctx):
                self._step_canonicalize_task_names(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_canonicalize_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-131 — LLM consolidation pass: merges
        # sequential phases of one action and splits composite
        # topics («Tether - формулировка» + «Tether - email» +
        # «Tether - WhatsApp» → one task; «Ziya/Odeya - apple» →
        # two tasks).
        try:
            with _trace_step("fireflies", "consolidate_tasks", **ctx):
                self._step_consolidate_tasks(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_consolidate_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-128 — soft-delete near-duplicate tasks (final
        # safety net after LLM consolidation).
        try:
            with _trace_step("fireflies", "dedupe_tasks", **ctx):
                _dedupe_meeting_tasks(
                    session,
                    source_kind=TaskSourceKind.fireflies,
                    conversation_id=row.fireflies_id,
                )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_dedupe_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # FR-CR-05-163 — classify tasks by strategic direction
        # (beta / budget / design / investors / deliverables / other).
        # Used by _build_todo_section for important-task badges.
        try:
            with _trace_step("fireflies", "classify_directions", **ctx):
                self._step_classify_directions(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_direction_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
        # Recount after dedupe.
        from app.models import Task as _Task
        report.tasks_created = (
            session.query(_Task)
            .filter(_Task.source_kind == TaskSourceKind.fireflies)
            .filter(_Task.source_conversation_id == row.fireflies_id)
            .filter(_Task.deleted_at.is_(None))
            .count()
        )
        row.tasks_extracted_count = report.tasks_created
        with _trace_step("fireflies", "doc_export", **ctx):
            if not self._step_doc_export(session, row):
                log.warning(
                    "fireflies_doc_export_failed",
                    recording_id=row.id, error=row.last_error,
                )
                report.errors.append(row.last_error or "doc_export_failed")
            else:
                report.google_doc_url = row.google_doc_url
        with _trace_step("fireflies", "short_summary", **ctx):
            if not self._step_short_summary(session, row):
                log.warning(
                    "fireflies_short_summary_failed",
                    recording_id=row.id, error=row.last_error,
                )
                report.errors.append(row.last_error or "short_summary_failed")
        report.short_chars = len(row.short_summary or "")
        with _trace_step("fireflies", "send_short_summary", **ctx):
            report.short_summary_recipients = self._step_send_short_summary(row)
        # FR-CR-05-120 follow-up — DM cards last.
        try:
            with _trace_step("fireflies", "post_task_cards", **ctx):
                self._step_post_task_cards(session, row)
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_post_task_cards_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )

        # FR-CR-05-194 — auto-publish в Slack (env-toggle).
        if (row.short_summary or "").strip():
            try:
                with _trace_step("fireflies", "send_to_slack", **ctx):
                    self._step_send_to_slack(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "fireflies_send_to_slack_unexpected_error",
                    fireflies_id=row.fireflies_id, error=str(e),
                )

        row.processed_at = datetime.now(timezone.utc)
        session.flush()

        # FR-CR-05-126 follow-up — single end-of-pipeline summary
        # log so the operator can see the WHOLE cycle in one
        # line: what was transcribed, what tasks landed, which
        # counterparties matched, where the doc lives.
        from app.models import Counterparty, CounterpartyMention

        cp_matches = (
            session.query(Counterparty.name)
            .join(
                CounterpartyMention,
                CounterpartyMention.counterparty_id == Counterparty.id,
            )
            .filter(CounterpartyMention.source_kind == "fireflies")
            .filter(CounterpartyMention.source_id == row.fireflies_id)
            .order_by(CounterpartyMention.id.asc())
            .all()
        )
        recent_tasks = (
            session.query(Task.title, Task.owner_display_name)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        _summary_payload = dict(
            title=(row.title or "")[:80],
            transcript_chars=report.transcript_chars,
            detailed_chars=report.detailed_chars,
            short_chars=report.short_chars,
            tasks_count=len(recent_tasks),
            tasks_titles=[t.title[:80] for t in recent_tasks][:25],
            tasks_owners=[
                t.owner_display_name for t in recent_tasks
            ][:25],
            counterparties_count=len(cp_matches),
            counterparties=[{"name": row_[0]} for row_ in cp_matches][:25],
            google_doc_url=row.google_doc_url,
            short_summary_recipients=report.short_summary_recipients,
            errors=report.errors,
        )
        log.info(
            "fireflies_pipeline_summary",
            fireflies_id=row.fireflies_id, **_summary_payload,
        )
        from app.services.trace_log import trace_event as _te4
        _te4(source="fireflies", recording_id=row.fireflies_id,
             event="pipeline_summary", **_summary_payload)
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


def _strip_markdown_emphasis(text: str) -> str:
    """FR-CR-05-117 — strip markdown emphasis markers from the
    detailed summary so it pastes cleanly into Google Docs.
    Google Docs renders `**bold**` as literal asterisks. Same
    deal for `__bold__`, `*italic*`, `_italic_`, and inline
    `` `code` ``. Preserves the inner text, just drops the
    surrounding markers. Conservative: only strips paired
    markers that wrap a non-empty span, leaves literal
    asterisks (e.g. multiplication «3 * 5») alone."""
    if not text:
        return text
    import re

    # **bold** / __bold__ — paired double markers.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    # *italic* / _italic_ — single markers; conservative match
    # (no whitespace right inside the markers, no markers around
    # an empty span). Avoids eating «3 * 5».
    text = re.sub(r"(?<!\*)\*(\S(?:.*?\S)?)\*(?!\*)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!_)_(\S(?:.*?\S)?)_(?!_)", r"\1", text, flags=re.DOTALL)
    # Inline `code`.
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


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
