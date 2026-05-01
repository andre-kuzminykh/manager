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


@contextmanager
def _trace_step(source: str, step: str, **ctx):
    """FR-CR-05-122 — every pipeline step is bracketed by a
    started/done log line so the operator can walk through a
    rerun by `grep step_started|step_done` over the listener
    output. Times the step in ms; on exception emits
    `*_step_failed` with the same shape so a single grep
    pattern covers all three outcomes.

    Usage::

        with _trace_step("fireflies", "transcribe", fireflies_id=…):
            ...

    Emits (with `source="fireflies"`):
        fireflies_step_started step=transcribe fireflies_id=…
        fireflies_step_done    step=transcribe duration_ms=N ok=True
                                 fireflies_id=…

    Failures emit `..._step_failed` and re-raise so the caller's
    error handling stays in charge.
    """
    started = _trace_time.monotonic()
    log.info(f"{source}_step_started", step=step, **ctx)
    try:
        yield
    except Exception as e:  # noqa: BLE001
        elapsed = int((_trace_time.monotonic() - started) * 1000)
        log.warning(
            f"{source}_step_failed",
            step=step, duration_ms=elapsed, error=str(e), **ctx,
        )
        raise
    else:
        elapsed = int((_trace_time.monotonic() - started) * 1000)
        log.info(
            f"{source}_step_done",
            step=step, duration_ms=elapsed, **ctx,
        )


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
    """FR-CR-05-119 / FR-CR-05-128 — full task list for the
    Google Doc body, sourced from the meeting's pending
    ActionDrafts (approval-gated; operator-pinned). Each entry
    gets the verbatim multi-sentence description + owner +
    due-date + priority. Returns "" when no drafts were
    created."""
    drafts = _meeting_drafts(
        session,
        source_kind=source_kind.value if hasattr(source_kind, "value") else str(source_kind),
        conversation_id=source_conversation_id,
    )
    if not drafts:
        return ""
    lines = ["", "📌 ЗАДАЧИ", ""]
    for i, d in enumerate(drafts, 1):
        v = _draft_render_view(d)
        body = (v["description"] or "").strip() or v["title"]
        lines.append(f"{i}. {body}")
        meta_bits = []
        if v["owner_display_name"]:
            meta_bits.append(f"Ответственный: {v['owner_display_name']}")
        if v["due_date"]:
            due = v["due_date"].strftime("%d.%m.%Y")
            if v["due_time"]:
                due += f" {v['due_time'].strftime('%H:%M')}"
            meta_bits.append(f"Срок: {due}")
        priority = v["priority"]
        if priority and priority != "medium":
            meta_bits.append(f"Приоритет: {priority}")
        if meta_bits:
            lines.append("   " + " · ".join(meta_bits))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _create_meeting_inference(
    session: "Session",
    *,
    source_kind: str,
    conversation_id: str,
    title: str | None,
    transcript_excerpt: str,
    pass_label: str,
    raw_extraction: list[dict] | None = None,
) -> tuple[int, int]:
    """FR-CR-05-128 — persist one ContextSnapshot + one
    IntentInference for a meeting LLM extraction pass. Returns
    `(snapshot_id, inference_id)` so callers can stamp drafts
    with the inference_id (NOT NULL on `action_drafts`).

    `pass_label` is a free-form string («fireflies_extract»,
    «zoom_verify», …) that lands in `inference.reasoning` for
    audit. `raw_extraction` is the LLM's tool-call output
    (list of task dicts), persisted on `inference.raw["tasks"]`
    so the operator can replay the run later.
    """
    from datetime import datetime, timezone

    from app.models import ContextSnapshot, IntentInference
    from app.models.intent import IntentType as IntentTypeEnum

    snap = ContextSnapshot(
        conversation_id=str(conversation_id),
        source_ts=str(conversation_id),
        thread_ts=None,
        source_message={
            "kind": source_kind,
            "title": title or "",
            "transcript_excerpt": (transcript_excerpt or "")[:4000],
        },
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inference = IntentInference(
        context_snapshot_id=snap.id,
        intent=IntentTypeEnum.create_task,
        confidence=1.0,
        invocation_type="passive",
        reasoning=pass_label,
        raw={"tasks": raw_extraction or []},
    )
    session.add(inference)
    session.flush()
    log.info(
        "meeting_inference_persisted",
        source_kind=source_kind,
        conversation_id=conversation_id,
        pass_label=pass_label,
        snapshot_id=snap.id,
        inference_id=inference.id,
        raw_task_count=len(raw_extraction or []),
    )
    _ = datetime.now(timezone.utc)  # silence unused import
    return snap.id, inference.id


def _create_meeting_draft(
    session: "Session",
    *,
    inference_id: int,
    payload: dict,
    pending: dict,
    admin_uid: str | None,
    slack_message_ts: str | None,
):
    """FR-CR-05-128 — build one ActionDraft for a meeting-
    extracted task. `payload` carries the canonical task fields
    (title/description/owner_user_id/owner_display_name/priority/
    due_date/due_time); `pending` carries source-routing fields
    (source_kind, conversation_id, message_ts, permalink,
    fallback_author) so `handle_confirm_draft` can build the
    Task with the right `source_*` fields when the operator
    presses ✅."""
    from app.models import ActionDraft, ActionDraftState
    from app.models.intent import IntentType as IntentTypeEnum

    full_payload = dict(payload)
    full_payload["_pending"] = dict(pending)
    draft = ActionDraft(
        inference_id=inference_id,
        intent=IntentTypeEnum.create_task,
        state=ActionDraftState.proposed,
        payload=full_payload,
        created_by_slack_user_id=admin_uid,
        slack_message_ts=slack_message_ts,
    )
    session.add(draft)
    session.flush()
    return draft


def _wipe_pending_meeting_drafts(
    session: "Session",
    *,
    source_kind: str,
    conversation_id: str,
) -> int:
    """FR-CR-05-128 — delete unconfirmed (proposed / edited)
    ActionDraft rows for a given meeting before a rerun re-
    creates them. CONFIRMED drafts are kept (the operator's ✅
    is sacred — re-running should never undo a previously
    approved task). IGNORED / EXPIRED stay too (audit trail of
    what was rejected). Returns the number deleted.
    """
    from app.models import ActionDraft, ActionDraftState

    rows = (
        session.query(ActionDraft)
        .filter(ActionDraft.state.in_(
            [ActionDraftState.proposed, ActionDraftState.edited]
        ))
        .all()
    )
    deleted = 0
    for d in rows:
        pending = (d.payload or {}).get("_pending") or {}
        if (pending.get("source_kind") or "") != source_kind:
            continue
        if (pending.get("conversation_id") or "") != conversation_id:
            continue
        session.delete(d)
        deleted += 1
    if deleted:
        session.flush()
        log.info(
            "meeting_pending_drafts_wiped",
            source_kind=source_kind,
            conversation_id=conversation_id,
            count=deleted,
        )
    return deleted


def _meeting_drafts(
    session: "Session",
    *,
    source_kind: str,
    conversation_id: str,
    include_states: tuple[str, ...] = ("proposed", "edited", "confirmed"),
) -> list:
    """FR-CR-05-128 — fetch ActionDraft rows attached to a given
    meeting, ordered by id (creation order). Drafts carry their
    `_pending.source_kind` + `_pending.conversation_id` in
    `payload`; we filter on those JSON fields. `include_states`
    excludes ignored / expired / failed drafts so the To-Do list
    + doc render only show LIVE proposals.

    Operator-pinned approval contract: meeting-extracted tasks
    arrive as confirm-widgets in TG (just like TG-ingested
    tasks); only after ✅ does the draft turn into a real Task
    row + Sheets sync. Read paths therefore source from drafts,
    not from `tasks` directly.
    """
    from app.models import ActionDraft, ActionDraftState

    state_enum = [ActionDraftState(s) for s in include_states]
    rows = (
        session.query(ActionDraft)
        .filter(ActionDraft.state.in_(state_enum))
        .order_by(ActionDraft.id.asc())
        .all()
    )
    out: list = []
    for d in rows:
        payload = d.payload or {}
        pending = payload.get("_pending") or {}
        if (pending.get("source_kind") or "") != source_kind:
            continue
        if (pending.get("conversation_id") or "") != conversation_id:
            continue
        out.append(d)
    return out


def _draft_render_view(draft) -> dict[str, "Any"]:
    """FR-CR-05-128 — read-side projection of an ActionDraft so
    the To-Do / Doc renderers don't need to know about JSON
    payload shape. Mirrors the field set the old Task-based
    helpers used."""
    from datetime import date as _date, time as _time

    payload = draft.payload or {}
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip() or None
    owner_user_id = payload.get("owner_user_id")
    owner_display_name = (payload.get("owner_display_name") or "").strip() or None
    priority = (payload.get("priority") or "medium").strip() or "medium"
    due_date = None
    raw_due_date = payload.get("due_date")
    if isinstance(raw_due_date, str) and raw_due_date:
        try:
            due_date = _date.fromisoformat(raw_due_date[:10])
        except ValueError:
            due_date = None
    due_time = None
    raw_due_time = payload.get("due_time")
    if isinstance(raw_due_time, str) and raw_due_time:
        try:
            hh, mm = raw_due_time.split(":")[:2]
            due_time = _time(int(hh), int(mm))
        except (ValueError, TypeError):
            due_time = None
    return {
        "id": draft.id,
        "title": title,
        "description": description,
        "owner_user_id": owner_user_id,
        "owner_display_name": owner_display_name,
        "priority": priority,
        "due_date": due_date,
        "due_time": due_time,
    }


def _build_todo_section(
    session: "Session",
    *,
    source_kind: "TaskSourceKind",
    source_conversation_id: str,
) -> str:
    """FR-CR-05-119 / FR-CR-05-128 — render the To-Do block from
    the meeting's pending ActionDraft rows (operator-pinned
    approval flow: meeting tasks live as drafts until confirmed
    in TG). Sorted by creation order so the operator sees the
    same sequence as the confirm widgets arriving in DM.

    Returns "" when no drafts were created (operator pinned:
    drop the section entirely instead of an empty header).
    """
    drafts = _meeting_drafts(
        session,
        source_kind=source_kind.value if hasattr(source_kind, "value") else str(source_kind),
        conversation_id=source_conversation_id,
    )
    if not drafts:
        return ""
    lines = ["To-Do:"]
    for i, d in enumerate(drafts, 1):
        # FR-CR-05-120 — task descriptions are now in the
        # operator-pinned «<topic> - <action with details>»
        # format (enforced by TASK_EXTRACTION_SYSTEM). Use as
        # is, just hard-cap at 350 chars so a runaway LLM emit
        # can't push a single line over Telegram's per-message
        # limit. Owner appended in parens only when set —
        # «(не назначен)» is noise the operator pinned out.
        v = _draft_render_view(d)
        raw = (v["description"] or "").strip() or v["title"]
        if len(raw) > 350:
            cut = raw.rfind(" ", 0, 350)
            raw = (raw[: cut if cut > 200 else 350]).rstrip(",;:- ") + "…"
        owner = v["owner_display_name"] or ""
        if owner:
            lines.append(f"{i}) {raw} ({owner})")
        else:
            lines.append(f"{i}) {raw}")
    return "\n".join(lines)


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
            log.info(
                "fireflies_whisper_bias_prompt_built",
                fireflies_id=row.fireflies_id,
                prompt_chars=len(whisper_prompt),
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
        """FR-CR-05-125 — match counterparty mentions in the
        transcript against the canonical directory and persist
        the link rows. Returns the count of matches.

        Idempotency on rerun: deletes existing mention rows for
        this `(source_kind, source_id)` first so the new set
        replaces the old without UNIQUE violations.

        Empty directory (operator hasn't run pull yet) → silent
        skip, the doc/summary just don't carry the section.
        """
        from app.models import CounterpartyMention
        from app.services.counterparty_match import (
            match_counterparties_in_transcript,
        )

        if not row.transcript_text:
            return 0
        try:
            matches = match_counterparties_in_transcript(
                session,
                transcript=row.transcript_text,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_counterparty_match_unexpected_error",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
        # Replace existing mentions (idempotent rerun).
        session.query(CounterpartyMention).filter(
            CounterpartyMention.source_kind == "fireflies",
            CounterpartyMention.source_id == row.fireflies_id,
        ).delete()
        session.flush()
        for cp in matches:
            session.add(
                CounterpartyMention(
                    counterparty_id=cp.id,
                    source_kind="fireflies",
                    source_id=row.fireflies_id,
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.flush()
        log.info(
            "fireflies_counterparty_match_done",
            fireflies_id=row.fireflies_id,
            matched=len(matches),
        )
        return len(matches)

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
        chunks = _split_for_telegram(row.short_summary, limit=3800)
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
        log.info(
            "fireflies_task_extraction_llm_returned",
            fireflies_id=row.fireflies_id,
            model=self._settings.fireflies_tasks_model,
            raw_count=len(tasks),
            raw_titles=[
                (t.get("title") or "")[:80]
                for t in tasks if isinstance(t, dict)
            ][:25],
            raw_owners=[
                t.get("owner") for t in tasks if isinstance(t, dict)
            ][:25],
        )
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
        # FR-CR-05-128 — `--rerun` resets the row's flags but
        # leaves old unconfirmed drafts in DB; wipe them before
        # re-extracting so the operator doesn't get duplicate
        # widgets. Confirmed / ignored drafts are kept.
        _wipe_pending_meeting_drafts(
            session, source_kind="fireflies",
            conversation_id=row.fireflies_id,
        )
        # FR-CR-05-128 — one inference per meeting-extract pass.
        try:
            _, inference_id = _create_meeting_inference(
                session,
                source_kind="fireflies",
                conversation_id=row.fireflies_id,
                title=row.title,
                transcript_excerpt=row.transcript_text or "",
                pass_label="fireflies_extract",
                raw_extraction=[t for t in tasks if isinstance(t, dict)],
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"meeting inference persistence failed: {e}"
            log.warning(
                "fireflies_meeting_inference_persist_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
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
                # FR-CR-05-128 — meeting tasks now arrive as
                # ActionDraft rows; the actual Task is created
                # only after the operator presses ✅ in the
                # confirm widget (same contract as TG-ingested
                # tasks). NO Sheets-sync scheduling here, NO
                # TaskStatusHistory — that all happens lazily
                # via `handle_confirm_draft`.
                draft_payload = {
                    "title": title[:10_000],
                    "description": description,
                    "owner_user_id": owner_user_id,
                    "owner_display_name": owner_display_name,
                    "priority": (
                        priority
                        if priority in {"low", "medium", "high", "urgent"}
                        else "medium"
                    ),
                    "due_date": today.isoformat(),
                    "due_time": "18:00",
                }
                pending = {
                    "source_kind": "fireflies",
                    "conversation_id": row.fireflies_id,
                    "message_ts": row.fireflies_id,
                    "thread_ts": None,
                    "permalink": row.fireflies_share_url,
                    "fallback_author": admin_uid,
                    "context_snapshot_id": None,
                    "source_chat_id": 0,
                    "source_message_id": 0,
                    "source_text": (row.title or "")[:10_000],
                }
                _create_meeting_draft(
                    session,
                    inference_id=inference_id,
                    payload=draft_payload,
                    pending=pending,
                    admin_uid=admin_uid,
                    slack_message_ts=row.fireflies_id,
                )
                created += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "fireflies_task_draft_create_failed",
                    title=title[:80],
                    error=str(e),
                )
        row.tasks_extracted_count = created
        row.tasks_extracted = True
        return created

    def _step_verify_tasks(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-121 / FR-CR-05-128 — second LLM pass to catch
        tasks missed by `_step_extract_tasks`. Reads the
        transcript + already-extracted ActionDraft rows and
        asks the verifier prompt for any newly-missed actionable
        items. New drafts are appended to the same session;
        returns count. Idempotency: caller short-circuits via
        `row.attempts` and the per-recording bookmarking;
        re-running is safe because the verifier is told to skip
        duplicates."""
        from app.fireflies.prompts import TASK_VERIFICATION_SYSTEM
        from app.persistence.tasks import normalize_task_title
        from app.services.team_members import as_known_employees

        if not row.transcript_text or not row.detailed_summary:
            return 0
        existing_drafts = _meeting_drafts(
            session,
            source_kind="fireflies",
            conversation_id=row.fireflies_id,
        )
        existing_block = "\n".join(
            f"- {(d.payload or {}).get('title','')}: "
            f"{((d.payload or {}).get('description') or '')[:300]} "
            f"[owner={(d.payload or {}).get('owner_display_name') or '—'}]"
            for d in existing_drafts
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
        log.info(
            "fireflies_task_verification_done",
            fireflies_id=row.fireflies_id,
            existing_count=len(existing_drafts),
            newly_added=len(new_tasks),
            new_titles=[
                (t.get("title") or "")[:80]
                for t in new_tasks if isinstance(t, dict)
            ][:25],
            new_owners=[
                t.get("owner") for t in new_tasks if isinstance(t, dict)
            ][:25],
        )
        if not new_tasks:
            return 0
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        admin_uid = _admin_fallback_owner_id()
        today = date.today()
        added = 0
        try:
            _, verify_inference_id = _create_meeting_inference(
                session,
                source_kind="fireflies",
                conversation_id=row.fireflies_id,
                title=row.title,
                transcript_excerpt=row.transcript_text or "",
                pass_label="fireflies_verify",
                raw_extraction=[t for t in new_tasks if isinstance(t, dict)],
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "fireflies_verify_inference_persist_failed",
                fireflies_id=row.fireflies_id, error=str(e),
            )
            return 0
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
                draft_payload = {
                    "title": title[:10_000],
                    "description": description,
                    "owner_user_id": owner_uid,
                    "owner_display_name": owner_display_name,
                    "priority": (
                        priority_raw
                        if priority_raw in {"low", "medium", "high", "urgent"}
                        else "medium"
                    ),
                    "due_date": today.isoformat(),
                    "due_time": "18:00",
                }
                pending = {
                    "source_kind": "fireflies",
                    "conversation_id": row.fireflies_id,
                    "message_ts": row.fireflies_id,
                    "thread_ts": None,
                    "permalink": row.fireflies_share_url,
                    "fallback_author": admin_uid,
                    "context_snapshot_id": None,
                    "source_chat_id": 0,
                    "source_message_id": 0,
                    "source_text": (row.title or "")[:10_000],
                }
                _create_meeting_draft(
                    session,
                    inference_id=verify_inference_id,
                    payload=draft_payload,
                    pending=pending,
                    admin_uid=admin_uid,
                    slack_message_ts=row.fireflies_id,
                )
                added += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "fireflies_task_verify_draft_failed",
                    title=title[:80], error=str(e),
                )
        if added:
            row.tasks_extracted_count = (row.tasks_extracted_count or 0) + added
        return added

    def _step_post_task_cards(
        self, session: Session, row: MeetingRecording
    ) -> int:
        """FR-CR-05-120 / FR-CR-05-128 — post a CONFIRM WIDGET
        per extracted draft (operator-pinned approval flow:
        meeting tasks must be approved in TG just like TG-
        ingested tasks; pre-128 they posted as final cards
        which felt «already pressed»). Reuses
        `post_draft_confirmation` so the ✅/✏️/❌ keyboard +
        callback handling is identical to the TG path. Runs
        AFTER `_step_send_short_summary` so the operator gets
        the overview first, then per-task widgets."""
        if (
            self._sender is None
            or not getattr(self._sender, "enabled", False)
        ):
            return 0
        from app.telegram_bot.cards import post_draft_confirmation

        admin_uid = _admin_fallback_owner_id()
        drafts = _meeting_drafts(
            session,
            source_kind="fireflies",
            conversation_id=row.fireflies_id,
            include_states=("proposed", "edited"),
        )
        posted = 0
        for draft in drafts:
            payload = draft.payload or {}
            owner_uid = payload.get("owner_user_id")
            try:
                post_draft_confirmation(
                    sender=self._sender,
                    session=session,
                    draft=draft,
                    source_chat_id=0,
                    source_message_id=0,
                    author_user_id=admin_uid,
                    owner_user_id=owner_uid,
                )
                posted += 1
            except Exception as e:  # noqa: BLE001
                log.info(
                    "fireflies_task_widget_post_failed",
                    draft_id=draft.id,
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
        report.tasks_created = row.tasks_extracted_count or report.tasks_created
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
        log.info(
            "fireflies_pipeline_summary",
            fireflies_id=row.fireflies_id,
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
