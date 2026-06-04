"""FR-EC-CRITIC — LIVE read-only smoke of the FR entity resolver on ONE meeting.

Shows what the resolver WOULD canonicalise, end-to-end, WITHOUT touching the
DB / pipeline / Slack / webhook:
  1. load a meeting's text (detailed_summary by default, or --use transcript);
  2. fetch Viktor's CRM dump via his MCP, lean it (app.services.entity_resolver_fr);
  3. one reasoning-model pass (gpt-5.5, high effort) → {mention → canonical,
     source, confidence};
  4. print the table.

Nothing is written. Default target = the «Алина, Ирина» Zoom meeting.

Run on the host (needs OpenAI key + the new module copied in):
    docker cp app/services/entity_resolver_fr.py manager-zoom-ff-1:/app/app/services/
    docker cp ops/fr_resolver_smoke.py            manager-zoom-ff-1:/app/ops/
    docker exec -i manager-zoom-ff-1 python -m ops.fr_resolver_smoke
"""
from __future__ import annotations

import argparse
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
                    "mention": {"type": "string",
                                "description": "the entity surface form as it appears in the text"},
                    "canonical": {"type": ["string", "null"],
                                  "description": "canonical CRM name, or null if not confidently in the CRM"},
                    "source": {"type": "string", "description": "CRM source sheet"},
                    "confidence": {"type": "number"},
                },
                "required": ["mention", "canonical", "confidence"],
            },
        }
    },
    "required": ["resolutions"],
}


def _load_text(zoom_id: str | None, ff_id: str | None, use: str) -> tuple[str, str, list[str]]:
    from app.models import MeetingRecording, ZoomRecording

    with session_scope() as s:
        if ff_id:
            row = s.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == ff_id).one_or_none()
        else:
            row = s.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == zoom_id).one_or_none()
        if row is None:
            return "", "(not found)", []
        text = (row.detailed_summary if use == "summary" else row.transcript_text) or ""
        parts = list(getattr(row, "participants", None) or [])
        return text, (row.title or "(untitled)"), parts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--zoom-id", default=_DEFAULT_ZOOM)
    ap.add_argument("--ff-id", default=None)
    ap.add_argument("--use", choices=["summary", "transcript"], default="summary")
    ap.add_argument("--mcp-url", default=None)
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--limit", type=int, default=2000)
    a = ap.parse_args()

    s = get_settings()
    mcp_url = a.mcp_url or getattr(s, "entity_fr_mcp_url", "") or _DEFAULT_MCP

    text, title, participants = _load_text(a.zoom_id, a.ff_id, a.use)
    print(f"meeting: {title!r}  source={a.use}  text_chars={len(text)}")
    if not text.strip():
        print("no text for this meeting — nothing to resolve.")
        return 0

    print(f"fetching CRM dump via MCP (limit={a.limit}) ...")
    catalog = R.fetch_catalog(mcp_url=mcp_url, limit=a.limit, ttl_seconds=0)
    catalog_text = R.lean_catalog_text(catalog)
    print(f"catalog: {len(catalog)} entities, lean_chars={len(catalog_text)}")
    if not catalog:
        print("empty catalog — check MCP access.")
        return 1

    msgs = R.build_resolution_messages(
        meeting_title=title, participants=participants,
        transcript_or_summary=text, catalog_text=catalog_text,
    )
    user_prompt = msgs[1]["content"] + "\n\n" + msgs[2]["content"]

    from openai import OpenAI

    from app.intent.llm_backends import OpenAIBackend
    backend = OpenAIBackend(OpenAI(api_key=s.openai_api_key), a.model)
    print(f"calling {a.model} (effort={a.effort}) ...")
    result = backend.call_tool(
        system_prompt=msgs[0]["content"],
        user_prompt=user_prompt,
        tool_name="resolve_entities",
        tool_description="Resolve meeting entity mentions to canonical CRM names.",
        tool_parameters=_TOOL_PARAMS,
        reasoning_effort=a.effort,
    )
    if not result:
        print("model returned nothing.")
        return 1

    rows = result.get("resolutions") or []
    rows.sort(key=lambda r: -(r.get("confidence") or 0))
    print(f"\n=== {len(rows)} resolutions ===")
    for r in rows:
        canon = r.get("canonical")
        mark = "→" if canon else "·"
        print(f"  {mark} {r.get('mention','')!r:32} -> {canon or '(unknown)'!r:28} "
              f"[{r.get('confidence', 0):.2f}]  {r.get('source','')}")
    changed = sum(1 for r in rows
                  if r.get("canonical")
                  and str(r.get("canonical")).strip().lower()
                  != str(r.get("mention")).strip().lower())
    print(f"\nwould change {changed}/{len(rows)} mentions. (read-only; nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
