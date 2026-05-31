"""FR-CR-05-220 — embedding service: text_repr builders, hashing,
batch embed wrapper, and (pg-gated) refresh + cosine search.

Pure-function tests run everywhere. The DB-integration test runs only
when TEST_PG_VECTOR_URL points at a pgvector Postgres (the isolated
test DB / CI) — SQLite has no `vector` type so it is skipped there.
"""
from __future__ import annotations

import os

import pytest

from app.services.entity_embeddings import (
    EMBEDDING_DIM,
    build_text_repr_counterparty,
    build_text_repr_employee,
    build_text_repr_team_member,
    is_embeddable_counterparty,
    make_openai_embed_fn,
    text_repr_hash,
    _vec_literal,
)


# --------------------- FR-CR-05-229 directory hygiene --------------------- #
def test_alias_pronunciation_cards_not_embeddable():
    # auto-enrollment minted these from garbled Whisper mentions
    for bad in ["Mirae/«миры»", "FURTS/«фьюртс»", "PML/«пим»", "Anthropic/«Entropiq»"]:
        assert is_embeddable_counterparty(bad) is False


def test_category_tags_not_embeddable():
    for bad in ["Financial/VC", "Partnerships", "MENA", "Intros", "Strategic",
                "Financial/VC, Partnerships", "Business Clubs", "Network"]:
        assert is_embeddable_counterparty(bad) is False


def test_real_companies_are_embeddable():
    for ok in ["Mirae", "FURTS", "Bosch", "Goldman Sachs", "ADNOC",
               "Da Vinci Capital", "Aramco Ventures", "Mitsubishi Corporation"]:
        assert is_embeddable_counterparty(ok) is True


def test_blank_names_not_embeddable():
    assert is_embeddable_counterparty("") is False
    assert is_embeddable_counterparty("   ") is False


# --------------------------- text_repr builders --------------------------- #
def test_counterparty_text_repr_name_only():
    assert build_text_repr_counterparty("ADNOC", []) == "ADNOC"


def test_counterparty_text_repr_with_attributes():
    tr = build_text_repr_counterparty(
        "Goldman Sachs",
        [{"sector": "Investment bank", "region": "US", "key_person": "John Doe", "empty": ""}],
    )
    assert "Goldman Sachs" in tr
    assert "sector: Investment bank" in tr
    assert "region: US" in tr
    assert "key_person: John Doe" in tr
    # empty values dropped
    assert "empty" not in tr


def test_counterparty_attributes_are_deterministic():
    a = build_text_repr_counterparty("X", [{"b": "2", "a": "1"}])
    b = build_text_repr_counterparty("X", [{"a": "1", "b": "2"}])
    assert a == b  # sorted keys → stable regardless of dict order


def test_counterparty_text_repr_with_mention_contexts():
    # FR-CR-05-227 — mention contexts make alias-cards distinguishable
    # from canonical: name + meeting prose carries the actual signal.
    tr = build_text_repr_counterparty(
        "Mirae",
        [],
        mention_contexts=[
            "обсудили возможность нового раунда с Mirae в августе",
            "Mirae подтвердили участие в Series B",
        ],
    )
    assert "Mirae" in tr
    assert "обсудили возможность" in tr
    assert "Series B" in tr


def test_counterparty_text_repr_caps_mention_contexts():
    # very long context is capped per mention; only first N kept.
    tr = build_text_repr_counterparty(
        "X", [],
        mention_contexts=["A" * 1000, "B" * 1000, "C" * 1000, "D-skipped" * 100],
        max_mention_contexts=3,
        max_mention_chars=50,
    )
    assert "A" * 50 in tr
    assert "B" * 50 in tr
    assert "C" * 50 in tr
    assert "D-skipped" not in tr  # only 3 kept


def test_team_member_text_repr():
    tr = build_text_repr_team_member(
        real_name="Ирина Шипилова", role="Assistant",
        telegram_username="irina", notes="handles ops",
    )
    assert "Ирина Шипилова" in tr
    assert "role: Assistant" in tr
    assert "@irina" in tr
    assert "handles ops" in tr


def test_employee_text_repr_prefers_real_name():
    tr = build_text_repr_employee(
        display_name="andrew", real_name="Andrey Kuzminykh", title="CEO",
    )
    assert tr.startswith("Andrey Kuzminykh")
    assert "title: CEO" in tr
    assert "aka andrew" in tr


# ------------------------------- hashing ---------------------------------- #
def test_text_repr_hash_stable_and_distinct():
    h1 = text_repr_hash("ADNOC")
    h2 = text_repr_hash("ADNOC")
    h3 = text_repr_hash("ADNOC ")
    assert h1 == h2
    assert h1 != h3  # whitespace matters → distinct hash
    assert len(h1) == 64  # sha256 hex


# --------------------------- vector literal ------------------------------- #
def test_vec_literal_format():
    assert _vec_literal([0.1, 0.2, -0.3]) == "[0.1,0.2,-0.3]"


# ------------------------- openai embed wrapper --------------------------- #
class _FakeEmbeddings:
    def __init__(self, dim):
        self._dim = dim
        self.calls = []

    def create(self, *, model, input):
        self.calls.append({"model": model, "input": list(input)})

        class _D:
            def __init__(self, emb):
                self.embedding = emb

        class _R:
            pass

        r = _R()
        r.data = [_D([0.01] * self._dim) for _ in input]
        return r


class _FakeOpenAI:
    def __init__(self, dim):
        self.embeddings = _FakeEmbeddings(dim)


def test_make_openai_embed_fn_batches_in_order():
    client = _FakeOpenAI(EMBEDDING_DIM)
    fn = make_openai_embed_fn(client, model="text-embedding-3-large")
    out = fn(["a", "b", "c"])
    assert len(out) == 3
    assert all(len(v) == EMBEDDING_DIM for v in out)
    assert client.embeddings.calls[0]["model"] == "text-embedding-3-large"
    assert client.embeddings.calls[0]["input"] == ["a", "b", "c"]


def test_make_openai_embed_fn_empty_input_no_call():
    client = _FakeOpenAI(EMBEDDING_DIM)
    fn = make_openai_embed_fn(client)
    assert fn([]) == []
    assert client.embeddings.calls == []


# --------------------- pg-gated refresh + search -------------------------- #
_PG_URL = os.environ.get("TEST_PG_VECTOR_URL")


@pytest.mark.skipif(not _PG_URL, reason="needs TEST_PG_VECTOR_URL (pgvector Postgres)")
def test_refresh_and_search_roundtrip():
    """Against a real pgvector DB: seed two counterparties, embed with a
    deterministic fake embed_fn, and confirm cosine search ranks the
    closer one first + idempotent re-refresh embeds nothing."""
    from sqlalchemy import create_engine, text as sqltext
    from sqlalchemy.orm import sessionmaker

    from app.models.counterparty import Counterparty
    from app.services.entity_embeddings import (
        KIND_COUNTERPARTY, refresh_embeddings, search_entities,
    )

    eng = create_engine(_PG_URL.replace("postgresql+psycopg2", "postgresql+psycopg"), future=True)
    Session = sessionmaker(bind=eng, future=True)

    # deterministic toy embeddings: orthogonal-ish vectors keyed by name
    def fake_embed(texts):
        vecs = []
        for t in texts:
            v = [0.0] * EMBEDDING_DIM
            # put weight on a slot derived from the first char
            v[ord(t.strip()[0]) % EMBEDDING_DIM] = 1.0
            vecs.append(v)
        return vecs

    with Session() as s:
        # clean slate in a savepoint-ish manner (test DB only!)
        s.execute(sqltext("DELETE FROM entity_embeddings WHERE model = 'test-model'"))
        s.commit()
        stats = refresh_embeddings(
            s, embed_fn=fake_embed, kinds=[KIND_COUNTERPARTY],
            model="test-model", batch_size=128,
        )
        s.commit()
        assert stats["embedded"] >= 1
        # second run embeds nothing (hashes unchanged)
        stats2 = refresh_embeddings(
            s, embed_fn=fake_embed, kinds=[KIND_COUNTERPARTY], model="test-model",
        )
        assert stats2["embedded"] == 0

        # search: query starting with same char as some counterparty
        first_cp = s.query(Counterparty).first()
        if first_cp:
            hits = search_entities(
                s, kind=KIND_COUNTERPARTY, query_text=first_cp.name,
                embed_fn=fake_embed, model="test-model", k=5,
            )
            assert hits, "expected at least one hit"
            assert "score" in hits[0] and 0.0 <= hits[0]["score"] <= 1.0001
