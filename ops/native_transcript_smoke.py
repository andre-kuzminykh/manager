"""FR-NT-TR — READ-ONLY smoke: prove we can pull NATIVE transcripts from
Zoom (VTT) and Fireflies (GraphQL sentences) before flipping the
ZOOM_/FIREFLIES_PREFER_NATIVE_TRANSCRIPT flags on.

This does NOT write to the DB, does NOT touch any pipeline row, does NOT
run Whisper, and does NOT depend on the flags. It only:
  - lists recent recordings from each service (newest first),
  - fetches the native transcript for each,
  - prints chars + a short preview + the should_use_native() verdict
    (i.e. would the pipeline accept this native transcript as primary?).

Usage (on the host where Zoom/FF creds are in env, e.g. inside the
manager-zoom-ff container):

    python -m ops.native_transcript_smoke                 # 3 latest each
    python -m ops.native_transcript_smoke --zoom-n 5 --ff-n 5
    python -m ops.native_transcript_smoke --zoom-id <uuid>
    python -m ops.native_transcript_smoke --ff-id <id>

Exit code 0 always (it's a probe, not a gate).
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings


def _should_use_native(text: str | None, min_chars: int) -> bool:
    # Mirror of app.services.transcription.should_use_native — inlined so
    # this probe runs on an OLD image (before the new code is deployed).
    return bool(text and text.strip() and len(text.strip()) >= min_chars)


def _verdict(text: str | None, min_chars: int) -> str:
    use = _should_use_native(text, min_chars)
    n = len((text or "").strip())
    tag = "USE native (Whisper skipped)" if use else "FALL BACK to Whisper"
    return f"{n} chars -> {tag}"


def _preview(text: str | None, n: int = 200) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[:n] + "…") if len(t) > n else t


def _smoke_zoom(s, *, n: int, only_id: str | None, min_chars: int) -> None:
    print("\n=== ZOOM (native VTT) ===")
    if not (s.zoom_account_id and s.zoom_client_id and s.zoom_client_secret):
        print("  zoom creds not set — skipped")
        return
    from app.zoom.client import ZoomClient

    client = ZoomClient(
        account_id=s.zoom_account_id, client_id=s.zoom_client_id,
        client_secret=s.zoom_client_secret, api_base=s.zoom_api_base,
        oauth_url=s.zoom_oauth_url,
    )
    try:
        metas = client.list_recordings(limit=max(n, 1), page_size=100)
    except Exception as e:  # noqa: BLE001
        print(f"  list_recordings failed: {e}")
        return
    if only_id:
        metas = [m for m in metas if m.id == only_id]
        if not metas:
            print(f"  zoom_id {only_id} not in recent recordings")
            return
    else:
        metas = metas[:n]
    if not metas:
        print("  no recent Zoom recordings")
        return
    for m in metas:
        title = (getattr(m, "topic", None) or getattr(m, "title", None) or "")[:60]
        print(f"\n  • zoom_id={m.id}  {title!r}")
        # mirror pipeline._find_vtt_download_url WITHOUT a pipeline instance
        vtt_url = None
        for rf in ((m.raw or {}).get("recording_files") or []):
            if not isinstance(rf, dict):
                continue
            if (rf.get("recording_type") or "").lower() == "audio_transcript" \
               and (rf.get("file_extension") or "").upper() == "VTT":
                vtt_url = rf.get("download_url")
                break
        if not vtt_url:
            print("    VTT file: NOT FOUND in recording_files -> would fall back to Whisper")
            continue
        try:
            vtt_text = client.fetch_vtt_transcript(vtt_url)
        except Exception as e:  # noqa: BLE001
            print(f"    fetch_vtt_transcript failed: {e}")
            continue
        print(f"    VTT: {_verdict(vtt_text, min_chars)}")
        print(f"    preview: {_preview(vtt_text)}")


def _smoke_ff(s, *, n: int, only_id: str | None, min_chars: int) -> None:
    print("\n=== FIREFLIES (native sentences) ===")
    if not s.fireflies_api_token:
        print("  fireflies token not set — skipped")
        return
    from app.fireflies.client import FirefliesClient

    client = FirefliesClient(token=s.fireflies_api_token, endpoint=s.fireflies_api_url)
    ids: list[tuple[str, str]] = []
    if only_id:
        ids = [(only_id, "")]
    else:
        try:
            for t in client.list_transcripts(limit=max(n, 1)):
                ids.append((t.id, (t.title or "")[:60]))
        except Exception as e:  # noqa: BLE001
            print(f"  list_transcripts failed: {e}")
            return
    if not ids:
        print("  no recent Fireflies transcripts")
        return
    for fid, title in ids[:n if not only_id else 1]:
        print(f"\n  • fireflies_id={fid}  {title!r}")
        try:
            text = client.fetch_transcript_text(fid)
        except Exception as e:  # noqa: BLE001
            print(f"    fetch_transcript_text failed: {e}")
            continue
        print(f"    native: {_verdict(text, min_chars)}")
        print(f"    preview: {_preview(text)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--zoom-n", type=int, default=3)
    ap.add_argument("--ff-n", type=int, default=3)
    ap.add_argument("--zoom-id", default=None)
    ap.add_argument("--ff-id", default=None)
    ap.add_argument("--only", choices=["zoom", "ff"], default=None)
    a = ap.parse_args()

    s = get_settings()
    # getattr fallback: the new config field may not exist on an old image.
    min_chars = getattr(s, "native_transcript_min_chars", 100)
    print(f"native_transcript_min_chars = {min_chars}  "
          f"(flags are NOT read here — this is a read-only probe)")

    if a.only != "ff":
        _smoke_zoom(s, n=a.zoom_n, only_id=a.zoom_id, min_chars=min_chars)
    if a.only != "zoom":
        _smoke_ff(s, n=a.ff_n, only_id=a.ff_id, min_chars=min_chars)
    print("\ndone (read-only; nothing written, Whisper not called).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
