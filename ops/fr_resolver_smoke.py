"""FR-EC-CRITIC — LIVE read-only smoke of the sharded FR entity resolver.

End-to-end, NOTHING written:
  1. load a meeting's text (detailed_summary, or --use transcript);
  2. fetch Viktor's CRM via MCP, lean it, SHARD it to <=30K-token slices;
  3. add our team_members roster as one more slice (people → Jochen Rudat …);
  4. map ALL slices in PARALLEL (gpt-5.5, high effort) → per-slice candidates;
  5. a critic merges them → {mention → canonical, source, confidence};
  6. with --truth, score against the boss's ground-truth corrections.

Default target = the «Алина, Ирина» Zoom meeting.

Run on the host (copy the new module + this file in first):
    docker cp app/services/entity_resolver_fr.py manager-zoom-ff-1:/app/app/services/
    docker cp ops/fr_resolver_smoke.py            manager-zoom-ff-1:/app/ops/
    docker exec -i manager-zoom-ff-1 python -m ops.fr_resolver_smoke --truth
"""
from __future__ import annotations

import argparse
import re
import sys

from app.config import get_settings
from app.db import session_scope
from app.services import entity_resolver_fr as R

_DEFAULT_MCP = "https://thehumanoid.app.n8n.cloud/mcp/308ec41b-8efc-438d-9260-14fc1e916b67"
_DEFAULT_ZOOM = "bqf0xAK6S8yb4Kcrzo74TA=="   # «Алина, Ирина»

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

# Boss's ground-truth corrections for «Алина, Ирина» (key substring in the
# mention → expected canonical). Used by --truth to score the resolver.
_TRUTH: list[tuple[str, str]] = [
    ("ki one", "Key 1 Capital"), ("киван", "Key 1 Capital"),
    ("winrobotics", "Vinrobotics"), ("almiraia", "Mirae"), ("альмирая", "Mirae"),
    ("химейн", "HUMAIN"), ("humaine", "HUMAIN"),
    ("tru arrow", "Tru Arrow"), ("accenture", "Accenture Ventures"),
    ("incharge", "Incharge Capital Partners"), ("mazon", "Amazon Industrial Fund"),
    ("felix", "Felix Jahn"), ("rigby", "Rigby"),
    ("lingotto", "Lingotto Investment Management"),
    ("utrenberg", "Daniel Gutenberg"), ("утренберг", "Daniel Gutenberg"),
    ("йохан", "Jochen Rudat"), ("самир", "Samer Zawaideh"),
    ("стеф", "Stepan Natalevich"),
]


def _load_text(zoom_id, ff_id, use):
    from app.models import MeetingRecording, ZoomRecording
    with session_scope() as s:
        if ff_id:
            row = s.query(MeetingRecording).filter(MeetingRecording.fireflies_id == ff_id).one_or_none()
        else:
            row = s.query(ZoomRecording).filter(ZoomRecording.zoom_id == zoom_id).one_or_none()
        if row is None:
            return "", "(not found)", []
        text = (row.detailed_summary if use == "summary" else row.transcript_text) or ""
        return text, (row.title or "(untitled)"), list(getattr(row, "participants", None) or [])


def _team_roster():
    from app.models import TeamMember
    rows = []
    with session_scope() as s:
        for t in s.query(TeamMember).filter(TeamMember.active.is_(True)).all():
            aliases = " ".join(x for x in [t.telegram_username, t.notes] if x)
            rows.append((t.real_name or "", t.role or "", aliases))
    return R.team_roster_text(rows)


def _words(s):
    return {w for w in re.findall(r"[a-zа-яё0-9]{4,}", (s or "").lower())}


def _score(decisions, *, verbose=True):
    by_mention = {d.mention.lower(): d for d in decisions}
    hits = 0
    print("\n=== TRUTH CHECK (boss ground truth) ===")
    seen_keys = set()
    for key, expected in _TRUTH:
        if expected in seen_keys and key in ("киван", "альмирая", "humaine", "утренберг"):
            pass  # duplicate alias of same expected; still scored individually
        pred = next((d for m, d in by_mention.items() if key in m), None)
        got = pred.canonical if pred else None
        ok = bool(got) and bool(_words(expected) & _words(got))
        hits += 1 if ok else 0
        mark = "✓" if ok else "✗"
        print(f"  {mark} {key:14} expect {expected!r:34} got {got!r}")
    print(f"\nscore: {hits}/{len(_TRUTH)} entity-correct")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--zoom-id", default=_DEFAULT_ZOOM)
    ap.add_argument("--ff-id", default=None)
    ap.add_argument("--use", choices=["summary", "transcript"], default="summary")
    ap.add_argument("--mcp-url", default=None)
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--no-team", action="store_true", help="skip the team_members slice")
    ap.add_argument("--people", action="store_true", help="run Track-2 agentic people pass")
    ap.add_argument("--truth", action="store_true", help="score against boss ground truth")
    a = ap.parse_args()

    s = get_settings()
    mcp_url = a.mcp_url or getattr(s, "entity_fr_mcp_url", "") or _DEFAULT_MCP
    max_tokens = getattr(s, "entity_fr_max_context_tokens", 30000)
    workers = getattr(s, "entity_fr_shard_workers", 4)

    text, title, participants = _load_text(a.zoom_id, a.ff_id, a.use)
    print(f"meeting: {title!r}  source={a.use}  text_chars={len(text)}")
    if not text.strip():
        print("no text — nothing to resolve.")
        return 0

    print(f"fetching CRM dump via MCP (limit={a.limit}) ...")
    catalog = R.fetch_catalog(mcp_url=mcp_url, limit=a.limit, ttl_seconds=0)
    if not catalog:
        print("empty catalog — check MCP access.")
        return 1

    budget = R.shard_char_budget(max_context_tokens=max_tokens, transcript_chars=len(text))
    shards = R.shard_catalog(catalog, max_chars=budget)
    shard_texts = [R.lean_catalog_text(sh) for sh in shards]
    if not a.no_team:
        roster = _team_roster()
        if roster:
            shard_texts.append(roster)
    print(f"catalog: {len(catalog)} entities → {len(shards)} CRM shard(s) "
          f"(budget {budget} chars each) + {0 if a.no_team else 1} team slice; "
          f"{len(shard_texts)} parallel map-calls")

    from openai import OpenAI

    from app.intent.llm_backends import OpenAIBackend
    backend = OpenAIBackend(OpenAI(api_key=s.openai_api_key), a.model)

    def _call(messages):
        system = messages[0]["content"]
        user = "\n\n".join(m["content"] for m in messages[1:])
        res = backend.call_tool(
            system_prompt=system, user_prompt=user,
            tool_name="resolve_entities",
            tool_description="Resolve meeting entity mentions to canonical CRM names.",
            tool_parameters=_TOOL_PARAMS, reasoning_effort=a.effort,
        )
        rows = (res or {}).get("resolutions") or []
        out = []
        for r in rows:
            out.append(R.Decision(
                mention=str(r.get("mention", "")),
                canonical=(str(r["canonical"]).strip() if r.get("canonical") else None),
                source=str(r.get("source", "")), confidence=float(r.get("confidence") or 0.0)))
        return out

    print(f"running {len(shard_texts)} parallel maps + critic ({a.model}, effort={a.effort}) ...")
    decisions = R.resolve_sharded(
        meeting_title=title, participants=participants, transcript_or_summary=text,
        shard_texts=shard_texts, call_map=_call, call_critic=_call, max_workers=workers,
    )
    # Track 2 — agentic people pass on unresolved person-like mentions.
    if a.people:
        unresolved = [d.mention for d in decisions if not d.canonical]
        print(f"\nTrack-2 people: {len(unresolved)} unresolved mentions → org-guess → search → extract ...")
        from app.services.entity_people_fr import PERSON_ORG_TOOL, resolve_people

        def _orgs(messages):
            res = backend.call_tool(
                system_prompt=messages[0]["content"],
                user_prompt="\n\n".join(m["content"] for m in messages[1:]),
                tool_name="classify_people", tool_description="people + org",
                tool_parameters=PERSON_ORG_TOOL, reasoning_effort=a.effort)
            return [(str(p.get("mention", "")), str(p.get("org", "")))
                    for p in (res or {}).get("people") or [] if p.get("is_person") and p.get("org")]

        def _search(org):
            from app.ceo_brain.mcp_client import call_tool as ct
            ok2, t2 = ct(url=mcp_url, tool_name="humanoid_fr_search",
                         arguments={"query": org, "limit": "2"}, timeout=30.0)
            return R._unwrap_mcp_text(t2) if ok2 else ""

        people = resolve_people(
            unresolved_mentions=unresolved, meeting_title=title,
            participants=participants, transcript=text,
            call_orgs=_orgs, search_fn=_search, call_extract=_call,
            max_orgs=getattr(s, "entity_fr_people_max_orgs", 6))
        print(f"Track-2 resolved {len(people)} people:")
        for d in people:
            print(f"   → {d.mention!r:28} -> {d.canonical!r:28} [{d.confidence:.2f}] {d.source}")
        decisions = decisions + people

    decisions.sort(key=lambda d: -d.confidence)
    print(f"\n=== {len(decisions)} resolutions ===")
    for d in decisions:
        mark = "→" if d.canonical else "·"
        print(f"  {mark} {d.mention!r:34} -> {d.canonical or '(unknown)'!r:30} "
              f"[{d.confidence:.2f}]  {d.source}")
    changed = sum(1 for d in decisions if d.canonical
                  and d.canonical.strip().lower() != d.mention.strip().lower())
    print(f"\nwould change {changed}/{len(decisions)} mentions. (read-only; nothing written)")

    if a.truth:
        _score(decisions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
