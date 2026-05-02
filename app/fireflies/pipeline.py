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
    lines = ["", "🔗 КОНТРАГЕНТЫ", ""]
    for cp in rows:
        type_ = (cp.type or "").strip()
        suffix = f" — {type_}" if type_ else ""
        lines.append(f"• {cp.name}{suffix}")
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
    items: list[str] = []
    for i, t in enumerate(tasks, 1):
        owner = (t.owner_display_name or "").strip()
        if compact:
            raw = (t.title or "").strip() or (t.description or "").strip()
        else:
            raw = (t.description or "").strip() or (t.title or "").strip()
            if len(raw) > 350:
                cut = raw.rfind(" ", 0, 350)
                raw = (raw[: cut if cut > 200 else 350]).rstrip(",;:- ") + "…"
        if owner:
            items.append(f"{i}) {raw} ({owner})")
        else:
            items.append(f"{i}) {raw}")
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
    # To-Do / next-steps block + everything after it up to the
    # next blank-blank-line boundary or end-of-string. Used to
    # strip such a block before we append the deterministic one.
    r"\n{1,2}(?:to[\s\-]?do|to do list|следующие\s+шаги|"
    r"next\s+steps|action\s+items|action\s+list|"
    r"задачи|to[-\s]do list)\s*:[\s\S]*?(?=\n{2,}\S|\Z)",
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
            if kept_prefix and cand_prefix and kept_prefix == cand_prefix:
                reason = "topic_prefix_match"
            elif (
                # No topic prefix on either — fall back to full
                # description ratio ≥ 0.92 (very tight).
                not kept_prefix
                and not cand_prefix
                and kept_desc
                and cand_desc
                and difflib.SequenceMatcher(None, kept_desc, cand_desc).ratio()
                >= 0.92
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
                prompt=whisper_prompt,
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
        row.detailed_summary = _strip_markdown_emphasis(text)
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
            .order_by(Counterparty.type, Counterparty.name)
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
                trace_source="fireflies",
                trace_recording_id=row.fireflies_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_counterparty_resolve_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        # Persist mention→canonical mapping on the recording so
        # the canonicalize step can pick it up.
        by_id = {cp.id: cp for cp in directory}
        mention_to_canonical: dict[str, str] = {}
        for mention, cid in mention_to_id.items():
            if cid is not None and cid in by_id:
                mention_to_canonical[mention] = by_id[cid].name
        # Stash in row.extra-style storage.
        if not hasattr(row, "_fr_canonical_map"):
            row.__dict__["_fr_canonical_map"] = mention_to_canonical

        # Replace existing CounterpartyMention rows (idempotent rerun).
        session.query(CounterpartyMention).filter(
            CounterpartyMention.source_kind == "fireflies",
            CounterpartyMention.source_id == row.fireflies_id,
        ).delete()
        session.flush()
        unique_ids: set[int] = set()
        for cid in mention_to_id.values():
            if cid is None or cid in unique_ids:
                continue
            unique_ids.add(cid)
            session.add(
                CounterpartyMention(
                    counterparty_id=cid,
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
        )
        return len(unique_ids)

    def _step_canonicalize_task_names(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-129 — rewrite Task title/description for
        this meeting so each Whisper-mangled mention («Тезер»,
        «Bauer/Dart», «Тензор», «Жамаль») is replaced with the
        canonical name from the directory («Tether»,
        «Bauerdart», «Tencent», «Jabal»). Uses the mapping
        stashed on the row by `_step_match_counterparties`.

        Returns count of tasks rewritten.
        """
        from app.services.counterparty_match import canonicalize_text
        from app.services.trace_log import trace_event

        mapping: dict[str, str] = (
            getattr(row, "_fr_canonical_map", None)
            or row.__dict__.get("_fr_canonical_map", {})
            or {}
        )
        if not mapping:
            return 0
        tasks = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .filter(Task.deleted_at.is_(None))
            .all()
        )
        rewrites: list[dict] = []
        for t in tasks:
            new_title = canonicalize_text(t.title, mapping)
            new_desc = canonicalize_text(t.description, mapping)
            changes = {}
            if new_title and new_title != t.title:
                changes["title_before"] = t.title
                changes["title_after"] = new_title
                t.title = new_title
            if new_desc and new_desc != t.description:
                changes["desc_before"] = (t.description or "")[:100]
                changes["desc_after"] = new_desc[:100]
                t.description = new_desc
            if changes:
                changes["task_id"] = t.id
                rewrites.append(changes)
        if rewrites:
            session.flush()
            log.info(
                "fireflies_task_canonical_rewrite_done",
                fireflies_id=row.fireflies_id,
                rewritten=len(rewrites),
                mapping_size=len(mapping),
            )
            trace_event(
                source="fireflies", recording_id=row.fireflies_id,
                event="task_canonical_rewrite_done",
                rewritten=len(rewrites),
                mapping=mapping,
                samples=rewrites[:20],
            )
        return len(rewrites)

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
        body = _truncate(text, limit=3800)
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
        # FR-CR-05-125 — single-line «🔗 Контрагенты: name1,
        # name2» appended after To-Do, before the doc-link
        # trailer. Only emitted when matches exist.
        cp_line = _build_counterparties_section_for_short_summary(
            session,
            source_kind="fireflies",
            source_id=row.fireflies_id,
        )
        if cp_line:
            body = body.rstrip() + "\n\n" + cp_line
        # FR-CR-05-127 — operator-pinned: the «DD/MM - <Topic>»
        # header becomes an HTML hyperlink to the Google Doc.
        # Replaces the old «📄 Подробный отчёт: <url>» trailer
        # line so the doc-link is on the title itself and the
        # body looks cleaner. Sent with parse_mode=HTML
        # (sender's default).
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
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
            )
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
        try:
            result = self._llm.call_tool(  # type: ignore[attr-defined]
                system_prompt=TASK_VERIFICATION_SYSTEM,
                user_prompt=user_prompt,
                tool_name=TASK_EXTRACTION_TOOL_NAME,
                tool_description=TASK_EXTRACTION_TOOL_DESCRIPTION,
                tool_parameters=TASK_EXTRACTION_TOOL_PARAMETERS,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_task_verification_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
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
                task = Task(
                    title=title[:10_000],
                    description=description,
                    owner_user_id=owner_uid,
                    owner_display_name=owner_display_name,
                    priority=priority,
                    due_date=today,
                    due_time=time(18, 0),
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

    def _step_post_task_cards(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-120 follow-up — post DM card per extracted
        Task ROW. Runs AFTER `_step_send_short_summary` so the
        operator gets the overview first, then per-task cards.
        Returns count of cards successfully posted."""
        if (
            self._sender is None
            or not getattr(self._sender, "enabled", False)
        ):
            return 0
        from app.telegram_bot.cards import post_initial_card

        admin_uid = _admin_fallback_owner_id()
        tasks = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        posted = 0
        for task in tasks:
            try:
                post_initial_card(
                    sender=self._sender,
                    session=session,
                    task=task,
                    chat_id=0,
                    reply_to_message_id=None,
                    author_user_id=admin_uid,
                )
                posted += 1
            except Exception as e:  # noqa: BLE001
                log.info(
                    "fireflies_task_card_post_failed",
                    task_id=task.id,
                    error=str(e),
                )
        return posted

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
            if not self._step_detailed_summary(row):
                session.flush()
                report.errors.append(row.last_error or "detailed_summary_failed")
                return report
        report.detailed_chars = len(row.detailed_summary or "")
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
        # FR-CR-05-128 — soft-delete near-duplicate tasks the
        # extract+verify passes produce («Сегментация
        # инвесторов» 3×, «BauerDart»/«Bauer/Dart» 2×, etc.).
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

        row.processed_at = datetime.now(timezone.utc)
        session.flush()

        # FR-CR-05-126 follow-up — single end-of-pipeline summary
        # log so the operator can see the WHOLE cycle in one
        # line: what was transcribed, what tasks landed, which
        # counterparties matched, where the doc lives.
        from app.models import Counterparty, CounterpartyMention

        cp_matches = (
            session.query(Counterparty.name, Counterparty.type)
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
            counterparties=[
                {"name": n, "type": t} for n, t in cp_matches
            ][:25],
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
