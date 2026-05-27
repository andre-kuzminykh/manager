"""FR-CR-05-204 — LLM cost: cheaper models for non-reasoning tasks.

Default models are asserted via `Settings.model_fields[...].default` (no env
needed). Env-override still works at runtime; these lock the *defaults*.
"""
from __future__ import annotations


def _default(field: str) -> str:
    from app.config import Settings

    return Settings.model_fields[field].default


def test_fr_cr_05_204_date_model_default_gpt4o() -> None:
    """Date parsing is structured — no gpt-5.5 reasoning needed."""
    assert _default("openai_date_model") == "gpt-4o"


def test_fr_cr_05_204_dedup_default_gpt4o_not_mini() -> None:
    """Semantic dedup stays on gpt-4o — NOT gpt-4o-mini, which missed
    near-identical cases (FR-CR-05-102)."""
    assert _default("openai_dedup_model") == "gpt-4o"
    assert _default("openai_dedup_model") != "gpt-4o-mini"


def test_fr_cr_05_204_short_summary_stays_gpt55() -> None:
    """Short summary keeps gpt-5.5 for RU quality; cost is controlled by
    NOT passing reasoning_effort at the call site, not by downgrading."""
    assert _default("fireflies_short_summary_model") == "gpt-5.5"
