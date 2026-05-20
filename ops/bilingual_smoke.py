#!/usr/bin/env python3
"""FR-CB2-3.39 smoke-test — manually verify bilingual restoration on
a REAL Zoom transcript.

Usage:
    docker compose exec -T bot python -m ops.bilingual_smoke \\
        --zoom-id "Es0xxBa8RHG4lPl9Z5ZMGQ=="

Steps:
  1. Pull the existing transcript via n8n_calendar::get_zoom_transcript
     (the same call the responder makes — no Anthropic involved).
  2. Ask the OpenAI detector (gpt-4o-mini by default) if the
     transcript looks like it needs an English re-pass.
  3. If YES — re-transcribe the SAME audio file with Whisper using
     `language="en"` (operator-pinned: «тот же STT что и брал, но
     язык англ»). Audio path is looked up from `ZoomRecording`.
  4. Merge PRIMARY + SECONDARY through gpt-4o.

Each step prints its head + chars so the operator can eyeball
quality before flipping the flag on in prod.

Flags:
  --detector-only — stop after step 2 (dry-run, cheapest)
  --audio-path PATH — bypass the DB lookup (useful when running
                      outside the docker container, no `db_session`)
"""
from __future__ import annotations

import argparse
import sys

from app.services.bilingual_restorer import (
    merge_transcripts_chunked,
    re_stt_english_via_whisper,
    should_re_stt_english,
)
from app.ceo_brain.config import get_mcp_servers
from app.ceo_brain.mcp_client import call_tool, list_tools
from app.config import get_settings


def _fetch_transcript(zoom_id: str) -> str:
    """Find the n8n_calendar MCP and pull the transcript directly via
    HTTP — same path the responder uses, no Anthropic involved.

    Lists the tool catalog first so we can fill in every required
    field of `get_zoom_transcript` (n8n's schema requires more than
    just `zoom_id` — `search_fragment` is mandatory too, and we
    default it to empty to get the full transcript)."""
    servers = get_mcp_servers()
    cal = next(
        (s for s in servers if s.get("name") == "n8n_calendar"), None,
    )
    if cal is None:
        print("ERROR: n8n_calendar not in MCP_SERVERS", file=sys.stderr)
        sys.exit(2)
    url = cal.get("url")
    if not url:
        print("ERROR: n8n_calendar has no url", file=sys.stderr)
        sys.exit(2)
    tools = list_tools(url) or []
    schema = next(
        (
            (t.get("inputSchema") or {}) for t in tools
            if (t.get("name") or "") == "get_zoom_transcript"
        ),
        {},
    )
    args: dict = {"zoom_id": zoom_id}
    for req in (schema.get("required") or []):
        if req not in args:
            # Default required strings to "" (matches the in-pipeline
            # coercion behaviour in mcp_client._coerce_args_to_schema).
            args[req] = ""
    ok, body = call_tool(
        url=url,
        tool_name="get_zoom_transcript",
        arguments=args,
        timeout=120.0,
        input_schema=schema,
    )
    if not ok:
        print(f"ERROR: get_zoom_transcript failed: {body[:200]}",
              file=sys.stderr)
        sys.exit(3)
    return body or ""


def _head(text: str, n: int = 800) -> str:
    if not text:
        return "(empty)"
    return text[:n] + (f"\n…[{len(text) - n} more chars]" if len(text) > n else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True, help="Zoom meeting ID")
    ap.add_argument(
        "--detector-only", action="store_true",
        help="Stop after the YES/NO detector decision.",
    )
    ap.add_argument(
        "--audio-path", default=None,
        help="Skip ZoomRecording DB lookup and use this path directly.",
    )
    ap.add_argument(
        "--max-head", type=int, default=800,
        help="Chars to show per transcript head (default 800).",
    )
    ap.add_argument(
        "--out-dir", default=None,
        help=(
            "Directory to write three full-text artifacts into: "
            "<zoom_id>_primary.txt, <zoom_id>_secondary_en.txt, "
            "<zoom_id>_final.txt. Created if missing."
        ),
    )
    args = ap.parse_args()

    out_dir = None
    if args.out_dir:
        import os
        out_dir = args.out_dir
        os.makedirs(out_dir, exist_ok=True)

    def _safe_zoom_id(z: str) -> str:
        return z.replace("/", "_").replace("=", "")

    def _dump(name: str, content: str) -> None:
        if not out_dir or not content:
            return
        import os
        path = os.path.join(
            out_dir, f"{_safe_zoom_id(args.zoom_id)}_{name}.txt",
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  wrote {len(content)} chars → {path}")

    s = get_settings()
    oai_key = (s.openai_api_key or "").strip()
    if not oai_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 2

    print("=" * 70)
    print(f"Zoom ID: {args.zoom_id}")
    print(f"Detector model:   {s.zoom_bilingual_detector_model}")
    print(f"Reconciler model: {s.zoom_bilingual_reconciler_model}")
    print(f"Whisper model:    {s.zoom_bilingual_whisper_model}")
    print(f"Flag (ZOOM_BILINGUAL_RESTORATION_ENABLED): "
          f"{s.zoom_bilingual_restoration_enabled}")
    print(f"Detector-only:    {args.detector_only}")
    print(f"Audio path arg:   {args.audio_path or '(use DB lookup)'}")
    print("=" * 70)

    print("\n[1/4] Fetching transcript via n8n_calendar::get_zoom_transcript…")
    primary = _fetch_transcript(args.zoom_id)
    print(f"  → {len(primary)} chars")
    print("\n--- PRIMARY HEAD ---")
    print(_head(primary, args.max_head))
    _dump("primary", primary)

    from openai import OpenAI
    openai_client = OpenAI(api_key=oai_key)

    print("\n[2/4] Detector → should we re-STT in English?")
    decision = should_re_stt_english(
        transcript=primary,
        openai_client=openai_client,
        model=s.zoom_bilingual_detector_model,
    )
    print(f"  → decision = {'YES' if decision else 'NO'}")
    if not decision:
        print("\nDetector said NO — no further action. Exiting clean.")
        return 0
    if args.detector_only:
        print("\n--detector-only set — stopping after detector. Exiting clean.")
        return 0

    print("\n[3/4] Re-STT (English Whisper pass)…")
    secondary = re_stt_english_via_whisper(
        zoom_id=args.zoom_id,
        audio_path=args.audio_path,
        openai_api_key=oai_key,
        model=s.zoom_bilingual_whisper_model,
    )
    if not secondary:
        print("  → Whisper returned no text (or audio file not found). "
              "Check ZoomRecording.audio_path for this zoom_id.")
        return 1
    print(f"  → {len(secondary)} chars")
    print("\n--- SECONDARY (EN) HEAD ---")
    print(_head(secondary, args.max_head))
    _dump("secondary_en", secondary)

    print("\n[4/4] Reconciler (chunked) — merging PRIMARY + SECONDARY…")
    merged = merge_transcripts_chunked(
        primary=primary,
        secondary=secondary,
        openai_client=openai_client,
        model=s.zoom_bilingual_reconciler_model,
        batch_input_chars=s.zoom_bilingual_reconcile_batch_input_chars,
        max_tokens_per_batch=s.zoom_bilingual_reconcile_max_tokens_per_batch,
    )
    if not merged:
        print("  → reconciler returned nothing. Stopping.")
        return 1
    print(f"  → {len(merged)} chars")
    print("\n--- FINAL (RECONCILED) HEAD ---")
    print(_head(merged, args.max_head))
    _dump("final", merged)

    print("\n" + "=" * 70)
    print(f"Summary: primary={len(primary)} secondary={len(secondary)} "
          f"final={len(merged)} chars")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
