"""FR-CR-05-241 — cheap concrete check of the hybrid's v1-fallback recall.

Extract counterparty mentions from a recording (Pass-1, SAME extractor v2
uses) and resolve them with v1 (whole-directory LLM over `counterparties`,
the 847 prod directory). Demonstrates that the speech-garbled forms a v2
top-K miss drops («хабспот», «BofA») are recovered by the fallback.

READ-ONLY. ~2 LLM calls (1 extract + 1 directory resolve). No Whisper.

Run in prod:
    docker exec manager-zoom-ff-1 python -m ops.diag_v1_fallback \\
        --zoom-id 2IjiKceGSp281P6SaZz28g==
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.models import ZoomRecording
from app.models.counterparty import Counterparty
from app.services.counterparty_match import (
    extract_counterparty_mentions,
    resolve_mentions_to_directory,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
    args = ap.parse_args()
    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    backend = OpenAIBackend(OpenAI(api_key=s.openai_api_key), s.openai_model)

    with session_scope() as ses:
        r = ses.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == args.zoom_id).first()
        if r is None or not (r.transcript_text or "").strip():
            print("ERROR: no recording / empty transcript", file=sys.stderr)
            return 3
        mentions = extract_counterparty_mentions(
            r.transcript_text, llm_backend=backend, model=s.openai_model,
            reasoning_effort="high",
        )
        print(f"извлечено упоминаний (Pass-1, тот же экстрактор что у v2): "
              f"{len(mentions)}\n  {mentions}\n")

        directory = ses.query(Counterparty).all()
        by_id = {c.id: c.name for c in directory}
        res = resolve_mentions_to_directory(
            mentions, directory, llm_backend=backend, model=s.openai_model,
            batch_size=0,
        )
        matched = 0
        print(f"=== v1 фолбэк против директории ({len(directory)} counterparties) ===")
        for m in mentions:
            cid = res.get(m)
            if cid:
                matched += 1
            print(f"  «{m}»  ->  {by_id.get(cid) if cid else 'none'}")
        print(f"\nv1-фолбэк распознал: {matched}/{len(mentions)} упоминаний")
        ses.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
