"""FR-CR-05-191 — Canonical name rewriting in meeting summaries.

After ``_step_detailed_summary`` / ``_step_short_summary`` produces text,
the pipeline must rewrite every mention of a known TeamMember + every
known Counterparty mention to the canonical form from the directory.

Coverage:
  - Entity extraction prompt returns valid JSON
  - Person resolver matches by token overlap, skips canonical-equal
  - Organization resolver delegates to FR-CR-05-129 LLM resolver
  - End-to-end `canonicalize_summary_text` chains all three
  - Empty / malformed inputs are graceful (no crash, no rewrite)
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.models import TeamMember
from app.models.counterparty import Counterparty
from app.services.summary_canonicalize import (
    canonicalize_summary_text,
    extract_name_entities,
    resolve_organizations_to_counterparties,
    resolve_people_to_team_members,
)


class _FakeLLM:
    """LLMBackend stub returning a queued response per call."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)

    def complete_text(self, **kwargs):  # noqa: ANN003
        if not self._responses:
            return ""
        return self._responses.pop(0)


# --------------------------------------------------------------------------- #
# extract_name_entities
# --------------------------------------------------------------------------- #


def test_extract_name_entities_parses_clean_json():
    llm = _FakeLLM(json.dumps({
        "people": ["Jarad Cannon", "Jared Kinnan"],
        "organizations": ["Affinity Partners", "Bosch"],
    }))
    out = extract_name_entities("dummy text", llm_backend=llm, model="m")
    assert out["people"] == ["Jarad Cannon", "Jared Kinnan"]
    assert out["organizations"] == ["Affinity Partners", "Bosch"]


def test_extract_name_entities_strips_code_fences():
    llm = _FakeLLM("```json\n" + json.dumps({
        "people": ["Артем Соколов"],
        "organizations": [],
    }) + "\n```")
    out = extract_name_entities("text", llm_backend=llm, model="m")
    assert out["people"] == ["Артем Соколов"]


def test_extract_name_entities_returns_empty_on_malformed_json():
    llm = _FakeLLM("this is not JSON {")
    out = extract_name_entities("text", llm_backend=llm, model="m")
    assert out == {"people": [], "organizations": []}


def test_extract_name_entities_returns_empty_on_blank_text():
    llm = _FakeLLM("not called")
    out = extract_name_entities("", llm_backend=llm, model="m")
    assert out == {"people": [], "organizations": []}


def test_extract_name_entities_returns_empty_on_blank_llm():
    llm = _FakeLLM("")
    out = extract_name_entities("some text", llm_backend=llm, model="m")
    assert out == {"people": [], "organizations": []}


# --------------------------------------------------------------------------- #
# resolve_people_to_team_members
# --------------------------------------------------------------------------- #


def test_resolve_people_skips_exact_canonical(session):
    session.add(TeamMember(
        real_name="Артем Соколов", telegram_user_id=1, active=True,
    ))
    session.flush()
    out = resolve_people_to_team_members(["Артем Соколов"], session)
    assert out == {}  # already canonical, no rewrite


def test_resolve_people_matches_by_last_name_token(session):
    session.add(TeamMember(
        real_name="Jarad Cannon", telegram_user_id=1, active=True,
    ))
    session.flush()
    out = resolve_people_to_team_members(["Jared Kinnan Cannon"], session)
    # Token "cannon" overlaps with canonical "Jarad Cannon" → rewrite
    assert "Jared Kinnan Cannon" in out
    assert out["Jared Kinnan Cannon"] == "Jarad Cannon"


def test_resolve_people_skips_inactive(session):
    session.add(TeamMember(
        real_name="Old Member", telegram_user_id=1, active=False,
    ))
    session.flush()
    out = resolve_people_to_team_members(["Old Member"], session)
    assert out == {}


def test_resolve_people_empty_mentions(session):
    assert resolve_people_to_team_members([], session) == {}


def test_resolve_people_no_team_members(session):
    out = resolve_people_to_team_members(["Some Name"], session)
    assert out == {}


def test_resolve_people_picks_best_token_overlap(session):
    """When the mention has tokens overlapping multiple members,
    pick the one with the highest overlap score."""
    session.add_all([
        TeamMember(real_name="Anna Smith", telegram_user_id=1, active=True),
        TeamMember(real_name="Anna Sokolova", telegram_user_id=2, active=True),
    ])
    session.flush()
    # "Anna Sokolova Petrov" matches "Anna Sokolova" by 2 tokens
    out = resolve_people_to_team_members(["Anna Sokolova Petrov"], session)
    assert out["Anna Sokolova Petrov"] == "Anna Sokolova"


# --------------------------------------------------------------------------- #
# resolve_organizations_to_counterparties
# --------------------------------------------------------------------------- #


def test_resolve_orgs_delegates_to_llm_resolver(session):
    cp = Counterparty(name="Affinity Partners", name_normalised="affinity partners")
    session.add(cp)
    session.flush()
    # Patch the LLM-based resolver to return a deterministic mapping
    with patch(
        "app.services.summary_canonicalize.resolve_mentions_to_directory",
        return_value={"Аффинити Партнерс": cp.id},
    ):
        out = resolve_organizations_to_counterparties(
            ["Аффинити Партнерс"], session,
            llm_backend=_FakeLLM(),
            model="m",
        )
    assert out == {"Аффинити Партнерс": "Affinity Partners"}


def test_resolve_orgs_skips_null_mapping(session):
    cp = Counterparty(name="X Ltd", name_normalised="x ltd")
    session.add(cp)
    session.flush()
    with patch(
        "app.services.summary_canonicalize.resolve_mentions_to_directory",
        return_value={"random": None},
    ):
        out = resolve_organizations_to_counterparties(
            ["random"], session, llm_backend=_FakeLLM(), model="m",
        )
    assert out == {}


def test_resolve_orgs_skips_canonical_equal(session):
    cp = Counterparty(name="Bosch", name_normalised="bosch")
    session.add(cp)
    session.flush()
    with patch(
        "app.services.summary_canonicalize.resolve_mentions_to_directory",
        return_value={"Bosch": cp.id},
    ):
        out = resolve_organizations_to_counterparties(
            ["Bosch"], session, llm_backend=_FakeLLM(), model="m",
        )
    # Same as canonical → skip
    assert out == {}


def test_resolve_orgs_empty(session):
    assert resolve_organizations_to_counterparties(
        [], session, llm_backend=_FakeLLM(), model="m",
    ) == {}


def test_resolve_orgs_no_directory(session):
    out = resolve_organizations_to_counterparties(
        ["Foo"], session, llm_backend=_FakeLLM(), model="m",
    )
    assert out == {}


# --------------------------------------------------------------------------- #
# canonicalize_summary_text — end-to-end
# --------------------------------------------------------------------------- #


def test_canonicalize_summary_text_end_to_end(session):
    session.add_all([
        TeamMember(
            real_name="Jarad Cannon", telegram_user_id=1, active=True,
        ),
        TeamMember(
            real_name="Sotirios Stasinopoulos", telegram_user_id=2, active=True,
        ),
    ])
    cp = Counterparty(
        name="Affinity Partners", name_normalised="affinity partners",
    )
    session.add(cp)
    session.flush()

    text = (
        "На звонке участвовали Jared Kinnan Cannon, Sotiris Dastanopoulos "
        "Stasinopoulos и команда Аффинити Партнерс. Они обсудили дальнейшие шаги."
    )
    extract_response = json.dumps({
        "people": ["Jared Kinnan Cannon", "Sotiris Dastanopoulos Stasinopoulos"],
        "organizations": ["Аффинити Партнерс"],
    })
    llm = _FakeLLM(extract_response)
    with patch(
        "app.services.summary_canonicalize.resolve_mentions_to_directory",
        return_value={"Аффинити Партнерс": cp.id},
    ):
        new_text, applied = canonicalize_summary_text(
            text,
            session=session, llm_backend=llm, model="m",
            trace_source="test", trace_recording_id="r1",
        )
    assert "Jarad Cannon" in new_text
    assert "Sotirios Stasinopoulos" in new_text
    assert "Affinity Partners" in new_text
    # Mis-spellings rewritten
    assert applied["Jared Kinnan Cannon"] == "Jarad Cannon"
    assert applied["Sotiris Dastanopoulos Stasinopoulos"] == "Sotirios Stasinopoulos"
    assert applied["Аффинити Партнерс"] == "Affinity Partners"


def test_canonicalize_summary_text_no_changes_when_already_canonical(session):
    session.add(TeamMember(
        real_name="Артем Соколов", telegram_user_id=1, active=True,
    ))
    session.flush()
    text = "Артем Соколов начал встречу."
    extract_response = json.dumps({
        "people": ["Артем Соколов"],
        "organizations": [],
    })
    llm = _FakeLLM(extract_response)
    new_text, applied = canonicalize_summary_text(
        text,
        session=session, llm_backend=llm, model="m",
    )
    assert new_text == text  # nothing changed
    assert applied == {}


def test_canonicalize_summary_text_blank_input(session):
    llm = _FakeLLM()
    new_text, applied = canonicalize_summary_text(
        "", session=session, llm_backend=llm, model="m",
    )
    assert new_text == ""
    assert applied == {}


def test_canonicalize_summary_text_llm_extract_failure_is_graceful(session):
    session.add(TeamMember(
        real_name="Артем", telegram_user_id=1, active=True,
    ))
    session.flush()
    text = "some text"
    llm = _FakeLLM("not json")  # malformed
    new_text, applied = canonicalize_summary_text(
        text, session=session, llm_backend=llm, model="m",
    )
    # Graceful: no rewrite, text unchanged
    assert new_text == text
    assert applied == {}
