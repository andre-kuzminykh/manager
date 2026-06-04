"""FR-EC-CRITIC — pipeline step: resolve meeting entity mentions against
Viktor's CRM and return a {surface_form: canonical} replacement map that merges
into the detailed-summary canon map (SPEC_ENTITY_CRITIC_v0.1 §11).

`resolve_for_meeting` is the single entry point called by the Zoom/FF
`_step_detailed_summary`. It is:
  * GATED by `settings.entity_fr_resolver_enabled` (off → returns {} immediately);
  * SHADOW-aware (`entity_fr_resolver_shadow` → logs decisions, returns {} so the
    text is NOT changed);
  * BEST-EFFORT (any failure → returns {}; the caller never breaks);
  * fully injectable (catalog / team_rows / call / recorder) so it unit-tests
    without MCP / OpenAI / a live DB.
"""
from __future__ import annotations

from typing import Any, Callable

from app.logging_setup import get_logger
from app.services import entity_resolver_fr as R

log = get_logger(__name__)

_TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mention": {"type": "string"},
                    "canonical": {"type": ["string", "null"]},
                    "source": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["mention", "canonical", "confidence"],
            },
        }
    },
    "required": ["resolutions"],
}


def _make_llm_call(llm: Any, *, model: str, effort: str) -> Callable[[list[dict]], list[R.Decision]]:
    """Adapt the pipeline's OpenAIBackend (`.call_tool`) to a
    messages -> list[Decision] callable for resolve_sharded."""
    def _call(messages: list[dict]) -> list[R.Decision]:
        system = messages[0]["content"]
        user = "\n\n".join(m["content"] for m in messages[1:])
        res = llm.call_tool(
            system_prompt=system, user_prompt=user,
            tool_name="resolve_entities",
            tool_description="Resolve meeting entity mentions to canonical CRM names.",
            tool_parameters=_TOOL_PARAMS, model=model, reasoning_effort=effort,
        )
        rows = (res or {}).get("resolutions") or []
        out: list[R.Decision] = []
        for r in rows:
            out.append(R.Decision(
                mention=str(r.get("mention", "")),
                canonical=(str(r["canonical"]).strip() if r.get("canonical") else None),
                source=str(r.get("source", "")),
                confidence=float(r.get("confidence") or 0.0),
            ))
        return out
    return _call


def _load_team_rows(session: Any) -> list[tuple[str, str, str]]:
    from app.models import TeamMember
    rows: list[tuple[str, str, str]] = []
    try:
        for t in session.query(TeamMember).filter(TeamMember.active.is_(True)).all():
            aliases = " ".join(x for x in [getattr(t, "telegram_username", None),
                                           getattr(t, "notes", None)] if x)
            rows.append((t.real_name or "", t.role or "", aliases))
    except Exception as e:  # noqa: BLE001
        log.info("fr_resolve_team_load_failed", error=str(e))
    return rows


def _db_recorder(session: Any, *, source: str, source_id: str) -> Callable[..., None]:
    from app.models import EntityFrDecision

    def _rec(d: R.Decision, *, applied: bool, shadow: bool) -> None:
        session.add(EntityFrDecision(
            source=source, source_id=source_id, mention=d.mention,
            canonical=d.canonical, source_list=d.source,
            confidence=d.confidence, applied=applied, shadow=shadow,
        ))
    return _rec


def resolve_for_meeting(
    *,
    settings: Any,
    text: str,
    meeting_title: str | None,
    participants: list[str] | None,
    source: str,
    source_id: str,
    llm: Any = None,
    session: Any = None,
    # injection points for tests (skip MCP / DB / OpenAI):
    catalog: list[R.FrEntity] | None = None,
    team_rows: list[tuple[str, str, str]] | None = None,
    call: Callable[[list[dict]], list[R.Decision]] | None = None,
    recorder: Callable[..., None] | None = None,
) -> dict[str, str]:
    """Resolve entities for one meeting. Returns the replacement map to merge
    into the detailed-summary canon map ({} in shadow / off / on failure)."""
    if not getattr(settings, "entity_fr_resolver_enabled", False):
        return {}
    shadow = bool(getattr(settings, "entity_fr_resolver_shadow", False))
    try:
        mcp_url = getattr(settings, "entity_fr_mcp_url", "") or ""
        if catalog is None:
            catalog = R.fetch_catalog(
                mcp_url=mcp_url,
                ttl_seconds=getattr(settings, "entity_fr_catalog_ttl_seconds", 3600),
            )
        if not catalog:
            log.warning("fr_resolve_empty_catalog", source=source, source_id=source_id)
            return {}

        budget = R.shard_char_budget(
            max_context_tokens=getattr(settings, "entity_fr_max_context_tokens", 30000),
            transcript_chars=len(text or ""),
        )
        shard_texts = [R.lean_catalog_text(sh) for sh in R.shard_catalog(catalog, max_chars=budget)]
        rows = team_rows if team_rows is not None else (
            _load_team_rows(session) if session is not None else [])
        roster = R.team_roster_text(rows)
        if roster:
            shard_texts.append(roster)

        if call is None:
            call = _make_llm_call(
                llm, model=getattr(settings, "entity_fr_resolver_model", "gpt-5.5"),
                effort="high")

        decisions = R.resolve_sharded(
            meeting_title=meeting_title, participants=participants,
            transcript_or_summary=text or "", shard_texts=shard_texts,
            call_map=call, call_critic=call,
            max_workers=getattr(settings, "entity_fr_shard_workers", 4),
        )

        min_conf = float(getattr(settings, "entity_fr_min_confidence", 0.7))
        replacements = R.build_replacements(decisions, min_confidence=min_conf)

        if recorder is None and session is not None:
            recorder = _db_recorder(session, source=source, source_id=source_id)
        if recorder is not None:
            for d in decisions:
                will_apply = (not shadow) and (d.mention in replacements
                                               or any(d.mention.startswith(k) for k in replacements))
                applied = bool(will_apply and d.canonical)
                try:
                    recorder(d, applied=applied, shadow=shadow)
                except Exception as e:  # noqa: BLE001
                    log.info("fr_resolve_record_failed", error=str(e))

        log.info("fr_resolve_done", source=source, source_id=source_id,
                 decisions=len(decisions), replacements=len(replacements),
                 shadow=shadow)
        return {} if shadow else replacements
    except Exception as e:  # noqa: BLE001
        log.warning("fr_resolve_failed", source=source, source_id=source_id, error=str(e))
        return {}


__all__ = ["resolve_for_meeting"]
