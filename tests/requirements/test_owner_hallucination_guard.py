"""FR-CR-04-22 — owner-stage hallucination guard.

The LLM occasionally returns the message author's *real_name*
(e.g. "Андре Кузьминых") as the assignee even though the message
uses 1st-person plural ("давай мы подготовим презу") and the prompt
forbids self-assignment. Two protections:

1. `node_owner` matches the LLM's display_name against BOTH `display_name`
   and `real_name` columns of the employees table — so when the LLM
   emits the user's full real name, we still resolve it to their
   slack_user_id and the standard quiet-author-fallback in
   `classify_and_persist` takes over.

2. When the LLM emits an unresolvable name that doesn't appear in the
   source / context AND the author is in the employees table, we drop
   the hallucinated `display_name` so the bot never asks "I couldn't
   find Андре Кузьминых".
"""
from __future__ import annotations

from app.intent.pipeline import _name_present, node_owner


class _BackendStub:
    """Always returns the canned owner payload for `record_owner`."""

    def __init__(self, payload: dict | None) -> None:
        self._payload = payload

    def call_tool(self, **kw):
        from app.intent.owner_prompt import OWNER_TOOL_NAME

        if kw.get("tool_name") == OWNER_TOOL_NAME:
            return self._payload
        return None


def _state(*, payload, source_text, employees, author="U-andre", context=None):
    return {
        "backend": _BackendStub(payload),
        "source_text": source_text,
        "context_messages": context or [],
        "author_user_id": author,
        "known_employees": employees,
    }


# --------------------------------------------------------------------------- #
# `_name_present` heuristic
# --------------------------------------------------------------------------- #


def test_name_present_finds_substring_in_source():
    assert _name_present("Иван", source_text="на Ивана сделай это", context_messages=[])


def test_name_present_finds_token_in_context():
    assert _name_present(
        "Sergei Volkov",
        source_text="давай сделаем X",
        context_messages=[{"text": "@Sergei можешь?"}],
    )


def test_name_absent_when_no_match():
    assert not _name_present(
        "Андре Кузьминых",
        source_text="давай мы на завтра подготовим презу",
        context_messages=[],
    )


def test_name_present_returns_false_for_empty():
    assert not _name_present("", source_text="x", context_messages=[])


# --------------------------------------------------------------------------- #
# Real-name match (the LLM returns the user's full real name; our
# display_name is shorter)
# --------------------------------------------------------------------------- #


def test_owner_resolves_real_name_to_slack_user_id():
    employees = [
        {
            "slack_user_id": "U-andre",
            "display_name": "Andre",
            "real_name": "Андре Кузьминых",
        }
    ]
    out = node_owner(
        _state(
            payload={"display_name": "Андре Кузьминых", "reasoning": "..."},
            source_text="давай мы на завтра подготовим презу",
            employees=employees,
        )
    )
    assert out["owner_user_id"] == "U-andre"


# --------------------------------------------------------------------------- #
# Hallucination guard — LLM emits author's name, but the name doesn't
# resolve and doesn't appear in the source. Drop it so the
# author-fallback can fire.
# --------------------------------------------------------------------------- #


def test_owner_drops_hallucinated_name_not_in_source():
    employees = [
        {
            "slack_user_id": "U-andre",
            "display_name": "Andre",
            # Note: no real_name field — resolve cannot match.
        }
    ]
    out = node_owner(
        _state(
            payload={"display_name": "Семён Кузьмич", "reasoning": "..."},
            source_text="давай мы на завтра подготовим презу",
            employees=employees,
        )
    )
    assert out["owner_user_id"] is None
    assert out["owner_display_name"] is None  # dropped → triggers author fallback


def test_owner_keeps_unresolvable_name_when_it_appears_in_source():
    """If the LLM extracted a name that's actually mentioned in the
    text, we still want the follow-up question — that's a real
    extraction we couldn't resolve, not a hallucination."""
    employees = [
        {
            "slack_user_id": "U-author",
            "display_name": "Andre",
            "real_name": "Andre Kuzminykh",
        }
    ]
    out = node_owner(
        _state(
            payload={"display_name": "Petya", "reasoning": "..."},
            source_text="@Petya, please prepare the deck for tomorrow",
            employees=employees,
            author="U-author",
        )
    )
    assert out["owner_user_id"] is None
    assert out["owner_display_name"] == "Petya"  # kept → bot will ask
