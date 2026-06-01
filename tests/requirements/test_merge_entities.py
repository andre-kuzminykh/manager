"""FR-CR-05-236 — contract tests for `ops.merge_entities.merge_pair`.

The merger is the only write-path against `entity_catalog_staging` after
LLM-assisted dedup. These tests lock the operator-pinned merge contract
(see docstring of ops.merge_entities) so a future tweak can't silently:
  - lose aliases (the surface forms by which an entity is recognised),
  - shrink the description,
  - drop mention counters (skews «most-informative leader» selection),
  - wipe parent_org when only the dst lacks it,
  - leave the source row behind (deletion is mandatory).
"""
from __future__ import annotations

from ops.merge_entities import _alias_set, merge_pair
from app.models.entity_catalog import EntityCatalogStaging


def _row(session, **kw):
    r = EntityCatalogStaging(
        name=kw["name"],
        name_normalised=kw.get("name_normalised", kw["name"].lower()),
        is_org=kw.get("is_org", True),
        parent_org=kw.get("parent_org"),
        description=kw.get("description", ""),
        aliases=kw.get("aliases"),
        mentions_count=kw.get("mentions_count", 1),
    )
    session.add(r); session.flush()
    return r


def test_aliases_union_dedup_includes_src_name(session):
    dst = _row(session, name="Baillie Gifford", aliases="BG",
               description="UK asset manager")
    src = _row(session, name="Ballie Gifford", name_normalised="ballie gifford",
               aliases="BG, bg", description="UK asset manager")
    merge_pair(session, src=src, dst=dst)
    # union, case-insensitive dedup, src.name lifted into aliases
    assert _alias_set(dst.aliases) == ["BG", "Ballie Gifford"]


def test_mentions_count_sums_and_src_deleted(session):
    dst = _row(session, name="LocalGlobe", mentions_count=7)
    src = _row(session, name="LocalGlobe UK", name_normalised="localglobe uk",
               mentions_count=3)
    src_id = src.id
    merge_pair(session, src=src, dst=dst)
    assert dst.mentions_count == 10
    assert session.get(EntityCatalogStaging, src_id) is None


def test_parent_org_filled_only_when_dst_empty(session):
    # dst already has a parent_org → must NOT be overwritten by src
    dst = _row(session, name="Acme Capital", parent_org="Acme Holdings")
    src = _row(session, name="Acme Capital LLC",
               name_normalised="acme capital llc", parent_org="Different Holdings")
    merge_pair(session, src=src, dst=dst)
    assert dst.parent_org == "Acme Holdings"

    # dst empty, src has → filled
    dst2 = _row(session, name="Beta Cap", name_normalised="beta cap", parent_org=None)
    src2 = _row(session, name="Beta Capital", name_normalised="beta capital",
                parent_org="Beta Holdings")
    merge_pair(session, src=src2, dst=dst2)
    assert dst2.parent_org == "Beta Holdings"


def test_description_grows_does_not_shrink(session):
    dst = _row(session, name="X", description="short")
    src = _row(session, name="X corp", name_normalised="x corp",
               description="additional context about the entity")
    before_len = len(dst.description)
    merge_pair(session, src=src, dst=dst)
    assert len(dst.description) >= before_len  # never shrinks
    assert "additional context" in dst.description


def test_cross_isorg_merge_carries_aliases_and_mentions(session):
    """Manual per↔org merge (e.g. «Genia Xasis» the person mistakenly also
    entered as an org): merge_pair itself does NOT gate on is_org — the
    guard lives in the CLI (--allow-cross-isorg). Here we assert the data
    contract still holds when the operator forces it."""
    person = _row(session, name="Genia Xasis", name_normalised="genia xasis",
                  is_org=False, mentions_count=5, description="Advisor; MENA.")
    org = _row(session, name="Genia Xasis", name_normalised="genia xasis org",
               is_org=True, mentions_count=1, description="Financial/VC firm.")
    merge_pair(session, src=org, dst=person)
    # org's distinct facts survive as alias/description; person stays the leader
    assert person.is_org is False
    assert person.mentions_count == 6
    assert "Financial/VC firm" in person.description


def test_alias_set_helper_orders_and_strips():
    assert _alias_set("BG, bg ,  Ballie Gifford , BG") == ["BG", "Ballie Gifford"]
    assert _alias_set("") == []
    assert _alias_set(None) == []
