"""FR-CR-05-231 — build a deduplicated entity catalog with an LLM.

The operator dropped a 1.4 MB tab-separated export (investors, advisors,
companies, people with bios / contacts / LinkedIn) and asked for a clean
catalog: one row per distinct entity with ``name``, an ``is_org`` flag,
``parent_org`` for people, and a ``description`` — so name+description can
drive point vector search later (FR-CR-05-219 satellite).

The source is heterogeneous: several differently-shaped tables glued
together, quoted multi-line cells, mis-transcribed surface forms. A column
parser is hopeless, so we lean on the LLM — but «понемного, чтобы контекст
не переполнялся»:

  1. ``iter_source_chunks`` — quote-aware TSV read → re-serialised, size-
     bounded chunks of whole logical rows. Deterministic: chunk N always
     covers the same rows, which is what makes the build resumable.
  2. ``extract_entities_from_chunk`` — one strict-JSON LLM call per chunk.
  3. ``upsert_entity`` — dedup on ``normalise_name`` (+ org/person), merging
     descriptions and aliases so re-seeing an entity *accumulates* context
     instead of duplicating it.
  4. ``ingest_source`` — orchestrates + checkpoints each chunk so a re-run
     skips finished chunks (idempotent, ZERO repeat LLM calls).

Everything writes to the STAGING tables only; nothing here touches the live
directory.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterator

import structlog
from sqlalchemy.orm import Session

from app.intent.llm_backends import LLMBackend
from app.models.entity_catalog import (
    INGEST_DONE,
    INGEST_ERROR,
    EntityCatalogIngest,
    EntityCatalogStaging,
)

log = structlog.get_logger(__name__)

# Default per-chunk character budget. ~6k chars keeps each LLM call cheap
# and well inside the context window while still giving the model enough
# surrounding rows to judge org-vs-person and affiliations.
DEFAULT_MAX_CHARS = 6000

# Hard cap on a single merged description so a hot entity (mentioned in
# dozens of chunks) can't grow unbounded.
DESCRIPTION_LIMIT = 4000


# ---------------------------------------------------------------------------
# 1) Chunking
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChunkSpec:
    """One deterministic unit of work for the LLM."""

    chunk_no: int
    row_lo: int  # inclusive logical-row index
    row_hi: int  # inclusive logical-row index
    text: str


def _row_to_line(row: list[str]) -> str:
    """Re-serialise a parsed TSV row to a single text line. Internal
    newlines (from quoted multi-line cells) are flattened to ' / ' so one
    logical row stays on one line and the char budget stays meaningful."""
    cells = [re.sub(r"\s*\n+\s*", " / ", (c or "").strip()) for c in row]
    # drop trailing empties so width noise doesn't bloat the prompt
    while cells and not cells[-1]:
        cells.pop()
    return "\t".join(cells)


def iter_source_chunks(
    path: str, *, max_chars: int = DEFAULT_MAX_CHARS,
) -> Iterator[ChunkSpec]:
    """Quote-aware read of the TSV export, yielding size-bounded chunks of
    whole logical rows. Empty rows are skipped but still advance the row
    index, so ``row_lo``/``row_hi`` map back to the source faithfully.

    Deterministic for a given (file, max_chars): chunk N always covers the
    same rows — the basis for resumable ingestion.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        chunk_no = 0
        buf: list[str] = []
        buf_chars = 0
        lo = 0
        idx = -1
        for idx, row in enumerate(reader):
            line = _row_to_line(row)
            if not line.strip():
                continue
            # +1 for the joining newline
            add = len(line) + 1
            if buf and buf_chars + add > max_chars:
                yield ChunkSpec(chunk_no, lo, idx - 1, "\n".join(buf))
                chunk_no += 1
                buf, buf_chars, lo = [], 0, idx
            buf.append(line)
            buf_chars += add
        if buf:
            yield ChunkSpec(chunk_no, lo, idx, "\n".join(buf))


# ---------------------------------------------------------------------------
# 2) LLM extraction
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = """\
You receive a chunk of a tab-separated export (investors, advisors,
companies, government bodies, and people with bios / contacts / LinkedIn).
Columns are inconsistent across rows and cells may be messy. Extract every
distinct real-world ENTITY you can identify.

For EACH entity output an object with:
- "name":        the cleanest full canonical form (a company/fund/org name,
                 or a person's full name). Strip contact noise, emails,
                 phone numbers, URLs, role suffixes from the NAME itself.
- "is_org":      true if it's an organisation (company, fund, bank, VC,
                 government body, club, brand); false if it's a natural
                 person.
- "parent_org":  for a PERSON, the organisation they belong to / represent,
                 if stated or clearly implied; otherwise null. For an ORG,
                 null.
- "description": a concise factual description assembled from the row —
                 what the org does / who the person is, their role, location,
                 notable facts, relationship/status notes. Keep useful
                 context (used for vector search); drop raw emails/phones.

Rules:
- One object per distinct entity. If a row names a company AND its contact
  person, emit BOTH (the company as org, the person as a person with
  parent_org = that company).
- Do NOT invent entities. Skip pure section labels, generic category words
  ("Financial/VC", "Partnerships", "MENA"), and stray header rows.
- Skip the operator's own company "Humanoid" / "Humain" as an entity.
- Prefer the most complete spelling when a name appears in several forms.
- description in the source's own language is fine (Russian or English).

Output STRICTLY valid JSON, no commentary, no code fences:
{"entities": [{"name": "...", "is_org": true, "parent_org": null,
  "description": "..."}, ...]}
Empty list if the chunk has no real entities.
"""


def _coerce_bool(v: object) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "yes", "org", "organization", "organisation", "1"):
            return True
        if t in ("false", "no", "person", "individual", "0"):
            return False
    return None


def _clean_entity(raw: object) -> dict | None:
    """Validate + normalise one LLM entity object; None if unusable."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if len(name) < 2:
        return None
    is_org = _coerce_bool(raw.get("is_org"))
    if is_org is None:
        return None
    parent = raw.get("parent_org")
    parent = str(parent).strip() if parent not in (None, "", "null") else None
    if is_org:
        parent = None  # orgs never carry a parent_org
    desc = str(raw.get("description") or "").strip()
    return {
        "name": name,
        "is_org": is_org,
        "parent_org": parent or None,
        "description": desc,
    }


def extract_entities_from_chunk(
    text: str, *, llm_backend: LLMBackend, model: str,
    reasoning_effort: str | None = None,
) -> list[dict]:
    """One LLM call → list of cleaned entity dicts for this chunk."""
    if not text or not text.strip():
        return []
    raw = llm_backend.complete_text(
        system_prompt=EXTRACT_SYSTEM,
        user_prompt=text,
        model=model,
        temperature=0.0,
        reasoning_effort=reasoning_effort,
        response_format={"type": "json_object"},
    )
    if not raw:
        return []
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("entity_catalog_json_parse_failed", raw_preview=raw[:200])
        return []
    items = data.get("entities") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        cleaned = _clean_entity(it)
        if cleaned is not None:
            out.append(cleaned)
    return out


# ---------------------------------------------------------------------------
# 3) Dedup + merge upsert
# ---------------------------------------------------------------------------
def _normalise_name(s: str | None) -> str:
    # Reuse the directory's battle-tested fuzzy key so the catalog dedups
    # the same way counterparty matching does.
    from app.sync.counterparties import normalise_name

    return normalise_name(s)


def merge_description(old: str, new: str, *, limit: int = DESCRIPTION_LIMIT) -> str:
    """Accumulate context without duplicating it. If `new` adds nothing
    (already contained, case-insensitively) keep `old`; otherwise append,
    bounded to `limit` chars."""
    old = (old or "").strip()
    new = (new or "").strip()
    if not new:
        return old
    if not old:
        return new[:limit]
    lo, ln = old.lower(), new.lower()
    if ln in lo or lo in ln:
        # keep the more informative of the two
        return (new if len(new) > len(old) else old)[:limit]
    merged = f"{old}\n\n{new}"
    return merged[:limit]


def _merge_aliases(existing: str | None, surface: str, canonical: str) -> str | None:
    """Track distinct surface forms (other than the stored canonical)."""
    surface = (surface or "").strip()
    if not surface or surface.lower() == (canonical or "").strip().lower():
        return existing
    seen = [a for a in (existing or "").split("\n") if a.strip()]
    if surface.lower() in {a.lower() for a in seen}:
        return existing
    seen.append(surface)
    return "\n".join(seen)


def upsert_entity(session: Session, rec: dict) -> tuple[EntityCatalogStaging, bool]:
    """Insert or merge one cleaned entity into staging. Dedup key is
    ``(name_normalised, is_org)``. Returns (row, created)."""
    norm = _normalise_name(rec["name"])
    if not norm:
        norm = rec["name"].strip().lower()
    is_org = bool(rec["is_org"])
    row = (
        session.query(EntityCatalogStaging)
        .filter(
            EntityCatalogStaging.name_normalised == norm,
            EntityCatalogStaging.is_org.is_(is_org),
        )
        .one_or_none()
    )
    if row is None:
        row = EntityCatalogStaging(
            name=rec["name"].strip(),
            name_normalised=norm,
            is_org=is_org,
            parent_org=rec.get("parent_org"),
            description=rec.get("description") or "",
            aliases=None,
            mentions_count=1,
        )
        session.add(row)
        session.flush()
        return row, True

    # Merge into existing.
    incoming_name = rec["name"].strip()
    # Prefer the longer / more complete surface as canonical; demote the
    # other to an alias so nothing is lost.
    if len(incoming_name) > len(row.name):
        row.aliases = _merge_aliases(row.aliases, row.name, incoming_name)
        row.name = incoming_name
    else:
        row.aliases = _merge_aliases(row.aliases, incoming_name, row.name)
    row.description = merge_description(row.description, rec.get("description") or "")
    if not row.parent_org and rec.get("parent_org"):
        row.parent_org = rec["parent_org"]
    row.mentions_count = (row.mentions_count or 1) + 1
    session.flush()
    return row, False


# ---------------------------------------------------------------------------
# 4) Orchestration with per-chunk checkpoints
# ---------------------------------------------------------------------------
def ingest_source(
    session: Session,
    *,
    path: str,
    llm_backend: LLMBackend,
    model: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    start_chunk: int = 0,
    limit_chunks: int | None = None,
    reasoning_effort: str | None = None,
    dry_run: bool = False,
    commit: Callable[[], None] | None = None,
) -> dict:
    """Walk the source in chunks, extracting + upserting entities, skipping
    chunks already recorded ``done`` in the ingest ledger.

    `commit` (optional) is called after each chunk so progress is durable
    and a crash/timeout loses at most one chunk. With `dry_run` no LLM call
    is made and nothing is written — it just reports chunk/row counts.
    """
    done: set[int] = set()
    if not dry_run:
        done = {
            n
            for (n,) in session.query(EntityCatalogIngest.chunk_no)
            .filter(EntityCatalogIngest.status == INGEST_DONE)
            .all()
        }

    stats = {
        "chunks_total": 0,
        "chunks_processed": 0,
        "chunks_skipped": 0,
        "entities_seen": 0,
        "entities_created": 0,
        "entities_merged": 0,
    }
    processed = 0
    for spec in iter_source_chunks(path, max_chars=max_chars):
        stats["chunks_total"] += 1
        if spec.chunk_no < start_chunk:
            continue
        if spec.chunk_no in done:
            stats["chunks_skipped"] += 1
            continue
        if limit_chunks is not None and processed >= limit_chunks:
            break

        if dry_run:
            stats["chunks_processed"] += 1
            processed += 1
            continue

        try:
            entities = extract_entities_from_chunk(
                spec.text, llm_backend=llm_backend, model=model,
                reasoning_effort=reasoning_effort,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "entity_catalog_chunk_failed",
                chunk_no=spec.chunk_no, error=str(e),
            )
            session.add(
                EntityCatalogIngest(
                    chunk_no=spec.chunk_no, row_lo=spec.row_lo,
                    row_hi=spec.row_hi, char_len=len(spec.text),
                    entities_found=0, status=INGEST_ERROR, note=str(e)[:500],
                )
            )
            if commit:
                commit()
            continue

        created = merged = 0
        for ent in entities:
            _, was_created = upsert_entity(session, ent)
            if was_created:
                created += 1
            else:
                merged += 1
        session.add(
            EntityCatalogIngest(
                chunk_no=spec.chunk_no, row_lo=spec.row_lo, row_hi=spec.row_hi,
                char_len=len(spec.text), entities_found=len(entities),
                status=INGEST_DONE,
            )
        )
        if commit:
            commit()

        stats["chunks_processed"] += 1
        stats["entities_seen"] += len(entities)
        stats["entities_created"] += created
        stats["entities_merged"] += merged
        processed += 1
        log.info(
            "entity_catalog_chunk_done",
            chunk_no=spec.chunk_no, rows=f"{spec.row_lo}-{spec.row_hi}",
            entities=len(entities), created=created, merged=merged,
        )

    return stats


__all__ = [
    "ChunkSpec",
    "DEFAULT_MAX_CHARS",
    "DESCRIPTION_LIMIT",
    "EXTRACT_SYSTEM",
    "iter_source_chunks",
    "extract_entities_from_chunk",
    "merge_description",
    "upsert_entity",
    "ingest_source",
]
