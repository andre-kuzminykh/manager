"""FR-CR-05-220 — embedding service for directory entities.

Builds a deterministic `text_repr` per entity, embeds it with OpenAI
`text-embedding-3-large` (3072 dims), and upserts into the
`entity_embeddings` satellite (FR-CR-05-219). A `text_repr_hash`
(sha256) lets the refresh path (FR-CR-05-221) skip rows whose source
text hasn't changed — so a full directory sync costs zero OpenAI calls
when nothing moved.

Search (`search_entities`) embeds a query string and returns the top-K
nearest entities of ONE kind by cosine similarity — the point lookup
the Zoom/FF matcher (FR-CR-05-222) uses instead of dumping the whole
directory into the prompt.

Everything that talks to OpenAI is injected (`embed_fn`) so unit tests
run without network or keys.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models.entity_embedding import (
    EMBEDDING_DIM,
    KIND_COUNTERPARTY,
    KIND_EMPLOYEE,
    KIND_TASK,
    KIND_TEAM_MEMBER,
)

log = get_logger(__name__)

DEFAULT_EMBED_MODEL = "text-embedding-3-large"

# FR-CR-05-229 — directory hygiene at embed time (sources not touched).
# Two kinds of non-entity rows pollute counterparties and dominate the
# vector top-K because they're short:
#   1) category/segment tags leaked from the sheet's column-A label
#      («Financial/VC», «Partnerships», «MENA» …) — NOT real companies.
#   2) alias/pronunciation cards minted by past auto-enrollment from
#      garbled Whisper mentions («Mirae/«миры»», «FURTS/«фьюртс»») —
#      duplicates of a real canonical row.
# We simply DON'T embed these, so they never appear as candidates.
# Reversible + isolated (the source rows stay; only embeddings skip them).
CATEGORY_TAG_BLACKLIST: frozenset[str] = frozenset({
    "financial/vc", "financial/vc, business clubs", "financial/vc, partnerships",
    "financial/vc, network", "financial/vc, банки", "partnerships",
    "partnerships, financial/vc", "intros", "mena", "strategic",
    "business clubs", "network", "банки", "looking for intros",
    "outreach", "rejections", "financial", "vc",
})


def is_embeddable_counterparty(name: str) -> bool:
    """False for category tags and alias/pronunciation cards (FR-CR-05-229)."""
    if not name or not name.strip():
        return False
    n = name.strip()
    # alias/pronunciation card: contains guillemet quotes from enrollment
    if "«" in n or "»" in n:
        return False
    if n.casefold() in CATEGORY_TAG_BLACKLIST:
        return False
    return True

# An embed function maps a batch of strings → a batch of vectors.
EmbedFn = Callable[[Sequence[str]], list[list[float]]]


# --------------------------------------------------------------------------- #
# text_repr builders — ONE deterministic string per entity kind.
# --------------------------------------------------------------------------- #
def _clean(s: Any) -> str:
    return " ".join(str(s).split()) if s is not None else ""


def _flatten_attributes(attrs: dict[str, Any], *, max_chars: int = 600) -> str:
    """Flatten a counterparty attributes JSON into a compact
    `key: value` string. Skips empty values and very large blobs; keeps
    scalars and short lists. Deterministic (sorted keys)."""
    if not isinstance(attrs, dict):
        return ""
    parts: list[str] = []
    for k in sorted(attrs.keys()):
        v = attrs[k]
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, (list, tuple)):
            v = ", ".join(_clean(x) for x in v if x not in (None, ""))
        elif isinstance(v, dict):
            # one level deep only — avoid embedding giant nested blobs
            v = ", ".join(f"{ik}={_clean(iv)}" for ik, iv in v.items() if iv not in (None, "", [], {}))
        v = _clean(v)
        if not v:
            continue
        parts.append(f"{_clean(k)}: {v}")
    out = "; ".join(parts)
    return out[:max_chars]


def build_text_repr_counterparty(
    name: str,
    attributes_blobs: Iterable[dict[str, Any]],
    *,
    mention_contexts: Iterable[str] | None = None,
    max_mention_contexts: int = 3,
    max_mention_chars: int = 300,
) -> str:
    """FR-CR-05-227 — name + flatten ключевых атрибутов + до 3 последних
    реальных контекстов упоминаний из counterparty_mentions. Контексты
    из встреч резко повышают сигнал на коротких именах (категории-теги
    типа 'Partnerships' проваливаются вниз retrieval'а, потому что у
    них контекстов нет), и делают alias-карточки отличимыми от canonical.
    """
    base = _clean(name)
    attr_str = "; ".join(
        s for s in (_flatten_attributes(a) for a in attributes_blobs) if s
    )
    parts = [base]
    if attr_str:
        parts.append(attr_str)
    for ctx in (mention_contexts or [])[:max_mention_contexts]:
        c = _clean(ctx)
        if c:
            parts.append(c[:max_mention_chars])
    return ". ".join(p for p in parts if p).strip()


def build_text_repr_team_member(
    *, real_name: str | None, role: str | None,
    telegram_username: str | None, notes: str | None,
) -> str:
    bits = [_clean(real_name)]
    if role:
        bits.append(f"role: {_clean(role)}")
    if telegram_username:
        bits.append(f"@{_clean(telegram_username)}")
    if notes:
        bits.append(_clean(notes)[:200])
    return ". ".join(b for b in bits if b).strip()


def build_text_repr_employee(
    *, display_name: str | None, real_name: str | None, title: str | None,
) -> str:
    name = _clean(real_name) or _clean(display_name)
    bits = [name]
    if display_name and _clean(display_name) != name:
        bits.append(f"aka {_clean(display_name)}")
    if title:
        bits.append(f"title: {_clean(title)}")
    return ". ".join(b for b in bits if b).strip()


def build_text_repr_task(
    *,
    title: str | None,
    description: str | None = None,
    owner_display_name: str | None = None,
    status: str | None = None,
    due_date: Any = None,
    category: str | None = None,
    max_desc_chars: int = 500,
) -> str:
    """FR-TV-010 — CONTENT-only text for a task embedding: title + description
    + owner + category (the semantic match signal). `status` and `due_date`
    are accepted for signature symmetry but are DELIBERATELY NOT embedded
    (DEC-2): they are volatile and read live at query time, so a status change
    must neither re-embed the task (FR-TV-014) nor skew similarity. Empty
    content ⇒ "" (not embeddable)."""
    _ = (status, due_date)  # intentionally not part of the embedded content
    bits = [_clean(title)]
    if description:
        bits.append(_clean(description)[:max_desc_chars])
    if owner_display_name:
        bits.append(f"owner: {_clean(owner_display_name)}")
    if category:
        bits.append(f"category: {_clean(category)}")
    return ". ".join(b for b in bits if b).strip()


def text_repr_hash(text_repr: str) -> str:
    return hashlib.sha256(text_repr.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# OpenAI embed function factory
# --------------------------------------------------------------------------- #
def make_openai_embed_fn(client: Any, model: str = DEFAULT_EMBED_MODEL) -> EmbedFn:
    """Wrap an OpenAI client into an EmbedFn. Returns 3072-dim vectors."""

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = client.embeddings.create(model=model, input=list(texts))
        # OpenAI returns items in input order.
        return [d.embedding for d in resp.data]

    return _embed


# --------------------------------------------------------------------------- #
# Collect directory rows → (kind, entity_id, text_repr)
# --------------------------------------------------------------------------- #
def collect_entity_texts(
    session: Session, *, kinds: Sequence[str],
    task_created_since: Any = None,
) -> list[tuple[str, str, str]]:
    """Return [(kind, entity_id, text_repr)] for the requested kinds.
    Imports models lazily to keep this module import-light.

    `task_created_since` (a date) optionally restricts KIND_TASK to tasks
    created on/after that date (FR-TV — «только сегодняшние»)."""
    out: list[tuple[str, str, str]] = []

    if KIND_COUNTERPARTY in kinds:
        from app.models.counterparty import Counterparty, CounterpartyMention

        # Pull recent mention contexts per counterparty once (FR-CR-05-227):
        # the last 3 non-null contexts give the embedder real meeting prose,
        # which is what makes alias-cards distinguishable from canonical and
        # category-tags ('Partnerships') stop dominating retrieval.
        mention_ctx_by_cp: dict[int, list[str]] = {}
        q = (
            session.query(CounterpartyMention)
            .filter(CounterpartyMention.context.isnot(None))
            .order_by(
                CounterpartyMention.counterparty_id,
                CounterpartyMention.created_at.desc().nullslast(),
                CounterpartyMention.id.desc(),
            )
        )
        for m in q:
            lst = mention_ctx_by_cp.setdefault(m.counterparty_id, [])
            if len(lst) < 3:
                lst.append(m.context)

        for cp in session.query(Counterparty).all():
            # FR-CR-05-229 — skip category tags + alias cards.
            if not is_embeddable_counterparty(cp.name):
                continue
            blobs = [a.attributes for a in (cp.attributes or [])]
            tr = build_text_repr_counterparty(
                cp.name, blobs,
                mention_contexts=mention_ctx_by_cp.get(cp.id, []),
            )
            if tr:
                out.append((KIND_COUNTERPARTY, str(cp.id), tr))

    if KIND_TEAM_MEMBER in kinds:
        from app.models.team import TeamMember

        for tm in session.query(TeamMember).filter(TeamMember.active.is_(True)).all():
            # FR-TV — exclude bot accounts from the people index (TeamMember has
            # no is_bot flag; heuristic on name/telegram «…bot…»). Garbage rows
            # with empty names are dropped anyway (build_text_repr → "").
            blob = f"{tm.real_name or ''} {tm.telegram_username or ''}".lower()
            if "bot" in blob:
                continue
            tr = build_text_repr_team_member(
                real_name=tm.real_name, role=tm.role,
                telegram_username=tm.telegram_username, notes=tm.notes,
            )
            if tr:
                out.append((KIND_TEAM_MEMBER, str(tm.id), tr))

    if KIND_EMPLOYEE in kinds:
        from app.models.employee import Employee

        for e in session.query(Employee).filter(Employee.is_bot.is_(False)).all():
            tr = build_text_repr_employee(
                display_name=e.display_name, real_name=e.real_name, title=e.title,
            )
            if tr:
                out.append((KIND_EMPLOYEE, e.slack_user_id, tr))

    if KIND_TASK in kinds:
        # FR-TV-010/011/015 — index live (non-deleted) tasks. CONTENT-only
        # text_repr (status/due read live at query time, NOT embedded).
        import datetime as _d

        from app.models.task import Task

        _tq = session.query(Task).filter(Task.deleted_at.is_(None))
        if task_created_since is not None:
            _since = _d.datetime.combine(
                task_created_since, _d.time.min, tzinfo=_d.timezone.utc)
            _tq = _tq.filter(Task.created_at >= _since)
        for t in _tq.all():
            tr = build_text_repr_task(
                title=t.title, description=t.description,
                owner_display_name=t.owner_display_name,
                status=getattr(t.status, "value", t.status),
                due_date=t.due_date, category=t.category,
            )
            if tr:
                out.append((KIND_TASK, str(t.id), tr))

    return out


# --------------------------------------------------------------------------- #
# Refresh: embed only rows whose text_repr_hash changed / is missing.
# --------------------------------------------------------------------------- #
def refresh_embeddings(
    session: Session,
    *,
    embed_fn: EmbedFn,
    kinds: Sequence[str] = (KIND_COUNTERPARTY, KIND_TEAM_MEMBER, KIND_EMPLOYEE),
    model: str = DEFAULT_EMBED_MODEL,
    batch_size: int = 256,
) -> dict[str, int]:
    """Upsert embeddings for all entities whose text_repr changed (single DB:
    source rows and embeddings live in `session`).

    Returns counters: {"scanned", "embedded", "skipped", "pruned"}.
    """
    rows = collect_entity_texts(session, kinds=kinds)
    return _apply_embeddings(
        session, rows, embed_fn=embed_fn, kinds=kinds, model=model,
        batch_size=batch_size,
    )


def refresh_embeddings_cross_db(
    source_session: Session,
    target_session: Session,
    *,
    embed_fn: EmbedFn,
    kinds: Sequence[str],
    model: str = DEFAULT_EMBED_MODEL,
    batch_size: int = 256,
) -> dict[str, int]:
    """FR-TV — collect source rows from `source_session` (e.g. the primary DB
    holding tasks/team) and upsert their embeddings into `target_session` (the
    SEPARATE pgvector instance). Same prune/hash-skip/embed/upsert logic as
    `refresh_embeddings`; the two DBs are simply different. Caller commits
    `target_session`."""
    rows = collect_entity_texts(source_session, kinds=kinds)
    return _apply_embeddings(
        target_session, rows, embed_fn=embed_fn, kinds=kinds, model=model,
        batch_size=batch_size,
    )


def _apply_embeddings(
    session: Session,
    rows: list[tuple[str, str, str]],
    *,
    embed_fn: EmbedFn,
    kinds: Sequence[str],
    model: str,
    batch_size: int,
) -> dict[str, int]:
    """Prune vanished rows, (re)embed only changed/missing rows by
    text_repr_hash, upsert into `session`.entity_embeddings. Scoped to `kinds`
    + `model` so other data is never touched. Returns counters."""
    scanned = len(rows)

    # FR-CR-05-229 — prune embeddings whose source entity is no longer
    # collectable (deleted, or now filtered out as a tag/alias). Scoped
    # to the kinds + model we're refreshing so we never touch other data.
    wanted: set[tuple[str, str]] = {(k, eid) for k, eid, _ in rows}
    pruned = 0
    existing_pairs = session.execute(
        sql_text(
            "SELECT kind, entity_id FROM entity_embeddings "
            "WHERE model = :model AND kind = ANY(:kinds)"
        ),
        {"model": model, "kinds": list(kinds)},
    ).fetchall()
    for k, eid in existing_pairs:
        if (k, eid) not in wanted:
            session.execute(
                sql_text(
                    "DELETE FROM entity_embeddings "
                    "WHERE kind = :k AND entity_id = :eid AND model = :model"
                ),
                {"k": k, "eid": eid, "model": model},
            )
            pruned += 1
    if pruned:
        session.flush()

    # Existing hashes for this model, scoped to the refreshed kinds.
    existing: dict[tuple[str, str], str] = {}
    res = session.execute(
        sql_text(
            "SELECT kind, entity_id, text_repr_hash FROM entity_embeddings "
            "WHERE model = :model AND kind = ANY(:kinds)"
        ),
        {"model": model, "kinds": list(kinds)},
    )
    for kind, entity_id, h in res:
        existing[(kind, entity_id)] = h

    # Which rows need (re)embedding?
    todo: list[tuple[str, str, str, str]] = []  # (kind, entity_id, text_repr, hash)
    for kind, entity_id, tr in rows:
        h = text_repr_hash(tr)
        if existing.get((kind, entity_id)) != h:
            todo.append((kind, entity_id, tr, h))

    embedded = 0
    for i in range(0, len(todo), batch_size):
        chunk = todo[i : i + batch_size]
        vectors = embed_fn([c[2] for c in chunk])
        if len(vectors) != len(chunk):
            raise RuntimeError(
                f"embed_fn returned {len(vectors)} vectors for {len(chunk)} inputs"
            )
        for (kind, entity_id, tr, h), vec in zip(chunk, vectors):
            if len(vec) != EMBEDDING_DIM:
                raise RuntimeError(
                    f"embedding dim {len(vec)} != expected {EMBEDDING_DIM}"
                )
            _upsert_embedding(
                session, kind=kind, entity_id=entity_id, model=model,
                vec=vec, text_repr=tr, text_repr_hash=h,
            )
            embedded += 1
        session.flush()

    log.info(
        "entity_embeddings_refresh",
        scanned=scanned, embedded=embedded, skipped=scanned - embedded,
        pruned=pruned, kinds=list(kinds), model=model,
    )
    return {
        "scanned": scanned, "embedded": embedded,
        "skipped": scanned - embedded, "pruned": pruned,
    }


def _vec_literal(vec: Sequence[float]) -> str:
    """pgvector text literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _upsert_embedding(
    session: Session, *, kind: str, entity_id: str, model: str,
    vec: Sequence[float], text_repr: str, text_repr_hash: str,
) -> None:
    session.execute(
        sql_text(
            """
            INSERT INTO entity_embeddings
                (kind, entity_id, model, dim, embedding, text_repr, text_repr_hash, updated_at)
            VALUES
                (:kind, :entity_id, :model, :dim, (:emb)::vector, :tr, :hash, now())
            ON CONFLICT (kind, entity_id, model) DO UPDATE SET
                dim = EXCLUDED.dim,
                embedding = EXCLUDED.embedding,
                text_repr = EXCLUDED.text_repr,
                text_repr_hash = EXCLUDED.text_repr_hash,
                updated_at = now()
            """
        ),
        {
            "kind": kind, "entity_id": entity_id, "model": model,
            "dim": len(vec), "emb": _vec_literal(vec),
            "tr": text_repr, "hash": text_repr_hash,
        },
    )


# --------------------------------------------------------------------------- #
# Search: top-K nearest entities of ONE kind by cosine similarity.
# --------------------------------------------------------------------------- #
def search_entities(
    session: Session,
    *,
    kind: str,
    query_text: str,
    embed_fn: EmbedFn,
    model: str = DEFAULT_EMBED_MODEL,
    k: int = 10,
) -> list[dict[str, Any]]:
    """Embed `query_text`, return top-K rows of `kind` as
    [{entity_id, text_repr, score}] (score = cosine similarity in
    [0,1], higher = closer)."""
    vecs = embed_fn([query_text])
    if not vecs:
        return []
    qv = _vec_literal(vecs[0])
    res = session.execute(
        sql_text(
            """
            SELECT entity_id, text_repr,
                   1 - (embedding <=> (:q)::vector) AS score
            FROM entity_embeddings
            WHERE kind = :kind AND model = :model
            ORDER BY embedding <=> (:q)::vector
            LIMIT :k
            """
        ),
        {"q": qv, "kind": kind, "model": model, "k": k},
    )
    return [
        {"entity_id": entity_id, "text_repr": tr, "score": float(score)}
        for entity_id, tr, score in res
    ]


__all__ = [
    "DEFAULT_EMBED_MODEL",
    "EmbedFn",
    "build_text_repr_counterparty",
    "build_text_repr_team_member",
    "build_text_repr_employee",
    "text_repr_hash",
    "make_openai_embed_fn",
    "collect_entity_texts",
    "refresh_embeddings",
    "search_entities",
]
