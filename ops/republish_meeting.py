"""Republish ONE Fireflies meeting through the REAL pipeline (resolver →
detailed → tasks → short → Doc) and, when armed, publish it (Telegram DM +
Slack mirror + n8n webhook + per-task cards).

Built for the Kima Ventures regression (01KT94K42F): the meeting was processed
too early off a roster-only transcript → «Запись без содержимого», suppressed
by the content gate. The native Fireflies transcript is now complete, so we
re-run it for real and ship the correct result.

Two phases (so you can eyeball before anything leaves the building):

  1. --regenerate  → refetch the native transcript, reset the stale flags,
                     run resolver+detailed+tasks+short+title+Doc, COMMIT.
                     NOTHING is sent to Slack / Telegram / the webhook.
                     Inspect the DB row + the freshly-created Google Doc.

  2. --send        → assumes step 1 already populated detailed/tasks/short.
                     Runs ONLY the publish steps (TG admin DM, Slack mirror,
                     n8n webhook, per-task DM cards) and COMMITS.

  --regenerate --send  → do both in one shot (only when you're confident).

Unlike `dry_run_meeting` this DOES write to the DB and (with --send) DOES post.
Unlike `reprocess_fireflies_summary` it refetches the native transcript, calls
the steps directly (precise control over which publish steps fire — no stale
auto-publish leak), and re-derives the title.

Usage (on the host, inside the container):
    docker exec -i manager-zoom-ff-1 python -m ops.republish_meeting \\
        --ff-id 01KT94K42FGR5N2YP3MC74GNBW --regenerate
    # inspect DB + Doc, then:
    docker exec -i manager-zoom-ff-1 python -m ops.republish_meeting \\
        --ff-id 01KT94K42FGR5N2YP3MC74GNBW --send
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--ff-id", required=True)
    ap.add_argument("--regenerate", action="store_true",
                    help="refetch native transcript, reset stale flags, and "
                         "re-run resolver+detailed+tasks+short+Doc (no sending)")
    ap.add_argument("--send", action="store_true",
                    help="publish: Telegram DM + Slack mirror + n8n webhook + "
                         "per-task cards. Requires detailed/short already present "
                         "(run --regenerate first, or pass both).")
    ap.add_argument("--no-webhook", action="store_true",
                    help="with --send: skip the n8n webhook (Slack + TG still go). "
                         "Use to publish to Slack first, webhook later.")
    ap.add_argument("--webhook-only", action="store_true",
                    help="send ONLY the n8n webhook (no Slack / TG / cards). "
                         "For the deferred webhook step after Slack looks right.")
    ap.add_argument("--repost-slack", action="store_true",
                    help="post a FRESH full Slack message (parent + task thread) "
                         "to the CEO Brain channel, deleting the stale earlier "
                         "post of this meeting first. Use when an empty/early "
                         "version already landed and you want a clean new one.")
    ap.add_argument("--update-slack", action="store_true",
                    help="UPDATE the existing Slack message in place (chat.update) "
                         "with the current short summary — no new message, no dup.")
    ap.add_argument("--keep-stale-slack", action="store_true",
                    help="with --repost-slack: do NOT delete the old message.")
    ap.add_argument("--no-title-push", action="store_true",
                    help="don't push the re-derived title back to Fireflies' UI")
    a = ap.parse_args()
    if not any([a.regenerate, a.send, a.webhook_only, a.repost_slack,
                a.update_slack]):
        print("nothing to do: pass --regenerate / --send / --webhook-only / "
              "--repost-slack / --update-slack")
        return 2

    s = get_settings()
    from openai import OpenAI

    from app.fireflies.client import FirefliesClient
    from app.fireflies.pipeline import (
        FirefliesPipeline,
        _DDMM_PREFIX,
        _dedupe_meeting_tasks,
    )
    from app.intent.llm_backends import OpenAIBackend
    from app.models import MeetingRecording, Task, TaskSourceKind
    from app.sync.factories import (
        build_calendar_credentials_factory_with_sa_fallback,
        build_docs_factory,
    )
    from app.telegram_bot.sender import TelegramSender

    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), s.openai_model)
    try:
        cal = build_calendar_credentials_factory_with_sa_fallback(s)
    except Exception as e:  # noqa: BLE001
        print(f"[cal_factory failed: {type(e).__name__}: {str(e)[:120]}]")
        cal = None
    try:
        docs = build_docs_factory(s)
    except Exception as e:  # noqa: BLE001
        print(f"[docs_factory failed: {type(e).__name__}: {str(e)[:120]}]")
        docs = None
    sender = TelegramSender(token=s.telegram_bot_token)

    pipe = FirefliesPipeline(
        settings=s,
        client=FirefliesClient(token=s.fireflies_api_token,
                               endpoint=s.fireflies_api_url),
        llm_backend=llm, docs_factory=docs, sender=sender, calendar_factory=cal,
    )
    src_kind, src_id = TaskSourceKind.fireflies, a.ff_id

    def _try(label, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            print(f"[{label} failed: {type(e).__name__}: {str(e)[:200]}]")
            return None

    with session_scope() as sess:
        row = sess.query(MeetingRecording).filter(
            MeetingRecording.fireflies_id == a.ff_id).one()
        print(f"meeting: {row.title!r}  ff_id={a.ff_id}")
        print(f"  transcript_chars(stored)={len(row.transcript_text or '')}  "
              f"detailed={bool(row.detailed_summary)}  "
              f"short_sent={row.short_summary_sent}")
        print(f"  mode: regenerate={a.regenerate} send={a.send}")
        print(f"  factories: docs={'ok' if docs else 'MISSING'} "
              f"cal={'ok' if cal else 'MISSING'} "
              f"tg={'on' if getattr(sender, 'enabled', False) else 'off'}\n")

        if a.regenerate:
            # 1) native transcript — refetch the complete call (was roster-only).
            native = _try("refetch_native",
                          lambda: pipe._client.fetch_transcript_text(a.ff_id))
            if native and len(native) > len(row.transcript_text or ""):
                print(f"native transcript: {len(native)} chars "
                      f"(was {len(row.transcript_text or '')})")
                row.transcript_text = native
            row.transcribed = True
            row.audio_downloaded = True  # skip download/Whisper — native is primary

            # 2) reset stale derived state so each step regenerates.
            row.detailed_summarised = False
            row.detailed_summary = None
            row.tasks_extracted = False
            row.tasks_extracted_count = 0
            row.short_summary = None
            row.short_summary_sent = False
            row.doc_exported = False
            row.google_doc_id = None
            row.google_doc_url = None
            row.title = None  # force re-derive from THIS transcript
            sess.query(Task).filter(
                Task.source_kind == src_kind,
                Task.source_conversation_id == src_id,
            ).delete(synchronize_session=False)

            # 3) detailed (runs the FR resolver inside → improves transcript,
            #    stashes _ff_detail_canon_map on this very row instance).
            _try("detailed", lambda: pipe._step_detailed_summary(row, session=sess))

            # 4) title — derive from the (now resolved) transcript. _step_*
            #    doesn't do this for FF; process_one does it inline, so we
            #    replicate that here (and optionally push to the FF UI).
            derived = _try("derive_title", lambda: pipe._derive_topic_title(row))
            if derived:
                if row.meeting_date and not _DDMM_PREFIX.match(derived):
                    derived = f"{row.meeting_date.strftime('%d/%m')} - {derived}"
                row.title = derived
                if not a.no_title_push:
                    _try("push_title", lambda: pipe._client.update_transcript_title(
                        a.ff_id, derived))
            else:
                row.title = (f"{row.meeting_date.strftime('%d/%m')} - Meeting"
                             if row.meeting_date else "Meeting")

            # 5) tasks — extract → verify → canonicalize → consolidate →
            #    dedupe → classify (same sequence as process_one; legacy
            #    counterparty match stays OFF, matching prod).
            _try("extract_tasks", lambda: pipe._step_extract_tasks(sess, row))
            _try("verify_tasks", lambda: pipe._step_verify_tasks(sess, row))
            _try("canonicalize_tasks",
                 lambda: pipe._step_canonicalize_task_names(sess, row))
            _try("consolidate_tasks",
                 lambda: pipe._step_consolidate_tasks(sess, row))
            _try("dedupe_tasks", lambda: _dedupe_meeting_tasks(
                sess, source_kind=src_kind, conversation_id=src_id))
            _try("classify_directions",
                 lambda: pipe._step_classify_directions(sess, row))

            # 6) Doc + short summary (short reads google_doc_url, so Doc first).
            _try("doc_export", lambda: pipe._step_doc_export(sess, row))
            _try("short_summary", lambda: pipe._step_short_summary(sess, row))

        # ---- report ----
        print("=" * 70)
        print(f"TITLE:  {row.title!r}")
        print(f"DOC:    {row.google_doc_url or '(none)'}")
        print("=" * 70)
        print("DETAILED SUMMARY:\n")
        print((row.detailed_summary or "(none)")[:4000]
              + ("\n…(truncated)…" if len(row.detailed_summary or "") > 4000 else ""))
        print("\n" + "=" * 70)
        tasks = (sess.query(Task)
                 .filter(Task.source_kind == src_kind,
                         Task.source_conversation_id == src_id,
                         Task.deleted_at.is_(None)).all())
        print(f"TASKS ({len(tasks)}):\n")
        for t in tasks:
            print(f"  • {t.title}   [owner: {t.owner_display_name}]")
        print("\n" + "=" * 70)
        print("SHORT SUMMARY:\n")
        print(row.short_summary or "(none / suppressed by content gate)")
        print("\n" + "=" * 70)

        if a.repost_slack or a.update_slack:
            import app.services.slack_publish as _sp
            channel = _sp._get_channel()
            token = getattr(s, _sp._get_token_key(), "") or ""
            old_ts = getattr(row, "slack_post_ts", None)
            if not (row.short_summary or "").strip():
                print("REFUSING slack: short_summary empty (run --regenerate).")
            elif not channel or not token:
                print("slack: AUTO_SEND_TO_SLACK_CHANNEL / token not configured.")
            elif a.update_slack:
                print(f">>> UPDATING existing Slack message (ts={old_ts})…")
                res = _try("slack_update", lambda: _sp.publish_zoom_recording_to_slack(
                    sess, row, channel=channel, token=token,
                    use_db_tasks=True, update_if_exists=True))
                print("update result:", res)
            else:  # --repost-slack
                if old_ts and not a.keep_stale_slack:
                    try:
                        from slack_sdk import WebClient
                        WebClient(token=token).chat_delete(
                            channel=channel, ts=old_ts)
                        print(f"deleted stale slack message ts={old_ts}")
                    except Exception as e:  # noqa: BLE001
                        print(f"[delete stale failed: {type(e).__name__}: "
                              f"{str(e)[:140]}] — continuing")
                row.slack_post_ts = None  # force a fresh post
                print(">>> POSTING fresh full Slack message (parent + tasks)…")
                res = _try("slack_repost", lambda: _sp.publish_zoom_recording_to_slack(
                    sess, row, channel=channel, token=token,
                    use_db_tasks=True, all_tasks_in_thread=True))
                print("repost result:", res)

        if a.send or a.webhook_only:
            if not (row.short_summary or "").strip():
                print("REFUSING to send: short_summary is empty (run "
                      "--regenerate first / content gate suppressed it).")
            elif a.webhook_only:
                # Only the n8n webhook: stub out TG + Slack mirror so
                # _step_send_short_summary runs solely the webhook tail.
                import app.services.slack_mirror as _sm
                _sm.post_meeting_summary_to_slack = lambda *aa, **kw: []
                if getattr(pipe, "_sender", None) is not None:
                    pipe._sender.send_message = lambda *aa, **kw: {"message_id": 0}
                row.short_summary_sent = False
                print(">>> SENDING WEBHOOK ONLY (no Slack / TG / cards)…")
                _try("webhook", lambda: pipe._step_send_short_summary(row))
                print(">>> webhook sent.")
            else:
                if a.no_webhook:
                    import app.services.meeting_webhook as _mw
                    _mw.post_meeting_to_webhook = (
                        lambda *aa, **kw: {"skipped": "no_webhook_flag"})
                    print("(--no-webhook: n8n webhook suppressed)")
                label = ("TG DM + Slack mirror + cards"
                         if a.no_webhook
                         else "TG DM + Slack mirror + webhook + cards")
                print(f">>> PUBLISHING ({label})…")
                _try("send_short_summary",
                     lambda: pipe._step_send_short_summary(row))
                _try("post_task_cards",
                     lambda: pipe._step_post_task_cards(sess, row))
                _try("send_to_slack", lambda: pipe._step_send_to_slack(sess, row))
                from datetime import datetime, timezone
                row.processed_at = datetime.now(timezone.utc)
                print(">>> published.")
        elif not (a.repost_slack or a.update_slack):
            print("NOT sent (no --send). DB persisted + Doc created. Re-run "
                  "with --send / --repost-slack / --update-slack to publish.")
        # session_scope commits on exit.
    print("\n(committed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
