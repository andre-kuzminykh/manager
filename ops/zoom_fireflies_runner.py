"""FR-CR-05-192y — тонкий runner для Zoom + Fireflies polling, без TG.

Контракт:
  1) Только встречи с участием OPERATOR_REQUIRED_EMAIL (fallback ZOOM_REQUIRED_EMAIL).
  2) Только встречи с meeting_date >= startup time MINUS OPERATOR_INGEST_LOOKBACK_HOURS
     (default 24h, FR-CR-05-192y-polish — restart не теряет встречи дня).
  3) Sender = реальный TelegramSender чтобы Zoom/FF pipeline шёл с карточками в TG.
"""
from __future__ import annotations

import os, signal, sys, threading, time
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import session_scope
from app.fireflies.client import FirefliesClient
from app.fireflies.pipeline import FirefliesPipeline
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingRecording, ZoomRecording
from app.services.pg_lock import try_meeting_lock
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback, build_docs_factory,
)
from app.zoom.client import ZoomClient
from app.zoom.pipeline import ZoomPipeline
from openai import OpenAI

log = get_logger(__name__)


def _operator_email(s):
    val = (os.environ.get("OPERATOR_REQUIRED_EMAIL")
           or getattr(s, "zoom_required_email", "")
           or "").strip().lower()
    return val or None


def _ff_has_operator(t, op_email):
    parts = [(p or "").strip().lower() for p in (t.participants or [])]
    return op_email in parts


def _meeting_date_utc(md):
    if md is None: return None
    return md if md.tzinfo else md.replace(tzinfo=timezone.utc)


def _poll_fireflies(pipeline, s, op_email, started_at):
    interval = max(1, int(s.fireflies_poll_interval_seconds))
    batch = max(1, int(s.fireflies_poll_batch_size))
    log.info("ff_runner_started", interval=interval, batch=batch,
             op_email=op_email, cutoff=started_at.isoformat())
    while True:
        try:
            transcripts = pipeline._client.list_transcripts(limit=batch)
        except Exception as e:
            log.warning("ff_runner_list_failed", error=str(e))
            time.sleep(interval); continue
        if not transcripts:
            log.info("ff_runner_tick", transcripts=0)
            time.sleep(interval); continue

        kept_new = []
        for t in transcripts:
            md = _meeting_date_utc(t.meeting_date)
            if md is not None and md >= started_at:
                kept_new.append(t)
        skipped_old = len(transcripts) - len(kept_new)

        if op_email:
            kept = [t for t in kept_new if _ff_has_operator(t, op_email)]
        else:
            kept = kept_new
        skipped_no_op = len(kept_new) - len(kept)

        if not kept:
            log.info("ff_runner_tick", transcripts=len(transcripts),
                     skipped_old=skipped_old, skipped_no_op=skipped_no_op,
                     processed=0)
            time.sleep(interval); continue

        ids = [t.id for t in kept]
        existing = {}
        with session_scope() as _s:
            for fid, done, err in _s.query(
                MeetingRecording.fireflies_id, MeetingRecording.tasks_extracted,
                MeetingRecording.last_error,
            ).filter(MeetingRecording.fireflies_id.in_(ids)).all():
                existing[fid] = (bool(done), err)

        processed = skipped = errors = 0
        for t in kept:
            st = existing.get(t.id)
            if st is not None and st[0] and not st[1]:
                skipped += 1; continue
            try:
                with session_scope() as session:
                    # FR-CR-05-258 — per-meeting advisory lock closes the
                    # runner-vs-manual (and runner-vs-runner) double-process
                    # race that caused duplicate Slack/Doc/webhook deliveries.
                    if not try_meeting_lock(session, "fireflies", t.id):
                        log.info("ff_runner_skip_locked", fireflies_id=t.id)
                        skipped += 1
                        continue
                    r = pipeline.process_one(session, t)
                if r.skipped_reason: skipped += 1
                else: processed += 1
            except Exception as e:
                errors += 1
                log.warning("ff_runner_failed", fireflies_id=t.id, error=str(e))
        log.info("ff_runner_tick",
                 transcripts=len(transcripts), kept=len(kept),
                 skipped_old=skipped_old, skipped_no_op=skipped_no_op,
                 processed=processed, skipped_done=skipped, errors=errors)
        time.sleep(interval)


def _poll_zoom(pipeline, s, op_email, started_at):
    interval = max(1, int(s.zoom_poll_interval_seconds))
    batch = max(1, int(s.zoom_poll_batch_size))
    strict = bool(getattr(s, "zoom_required_email_strict_host", False))
    log.info("zoom_runner_started", interval=interval, batch=batch,
             op_email=op_email, strict=strict, cutoff=started_at.isoformat())
    while True:
        try:
            metas = pipeline._client.list_recordings(
                limit=batch, required_email=op_email, strict_host=strict,
            )
        except Exception as e:
            log.warning("zoom_runner_list_failed", error=str(e))
            time.sleep(interval); continue
        if not metas:
            log.info("zoom_runner_tick", metas=0)
            time.sleep(interval); continue

        kept = []
        for m in metas:
            md = _meeting_date_utc(m.meeting_date)
            if md is not None and md >= started_at:
                kept.append(m)
        skipped_old = len(metas) - len(kept)

        if not kept:
            log.info("zoom_runner_tick", metas=len(metas),
                     skipped_old=skipped_old, processed=0)
            time.sleep(interval); continue

        ids = [m.id for m in kept]
        existing = {}
        with session_scope() as _s:
            for zid, done, err in _s.query(
                ZoomRecording.zoom_id, ZoomRecording.tasks_extracted,
                ZoomRecording.last_error,
            ).filter(ZoomRecording.zoom_id.in_(ids)).all():
                existing[zid] = (bool(done), err)

        processed = skipped = errors = 0
        for m in kept:
            st = existing.get(m.id)
            if st is not None and st[0] and not st[1]:
                skipped += 1; continue
            try:
                with session_scope() as session:
                    # FR-CR-05-258 — per-meeting advisory lock (see Fireflies).
                    if not try_meeting_lock(session, "zoom", m.id):
                        log.info("zoom_runner_skip_locked", zoom_id=m.id)
                        skipped += 1
                        continue
                    r = pipeline.process_one(session, m)
                if r.skipped_reason: skipped += 1
                else: processed += 1
            except Exception as e:
                errors += 1
                log.warning("zoom_runner_failed", zoom_id=m.id, error=str(e))
        log.info("zoom_runner_tick", metas=len(metas), kept=len(kept),
                 skipped_old=skipped_old, processed=processed,
                 skipped_done=skipped, errors=errors)
        time.sleep(interval)


def main():
    setup_logging()
    s = get_settings()
    op = _operator_email(s)
    # FR-CR-05-192y-polish: cutoff = now - LOOKBACK_HOURS (default 24h)
    # — restart не теряет встречи дня
    lookback_h = float(os.environ.get("OPERATOR_INGEST_LOOKBACK_HOURS", "24"))
    started_at = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
    log.info("zoom_ff_runner_starting",
             ff_realtime=s.fireflies_realtime_enabled,
             zoom_realtime=s.zoom_realtime_enabled,
             operator_email=op,
             lookback_hours=lookback_h,
             cutoff=started_at.isoformat())
    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), s.openai_model)
    try: cal = build_calendar_credentials_factory_with_sa_fallback(s)
    except Exception as e: log.warning("cal_factory_failed", error=str(e)); cal = None
    try: docs = build_docs_factory(s)
    except Exception as e: log.warning("docs_factory_failed", error=str(e)); docs = None
    from app.telegram_bot.sender import TelegramSender
    sender = TelegramSender(token=s.telegram_bot_token)
    threads = []
    if s.fireflies_realtime_enabled and s.fireflies_api_token:
        ff = FirefliesPipeline(
            settings=s,
            client=FirefliesClient(token=s.fireflies_api_token, endpoint=s.fireflies_api_url),
            llm_backend=llm, docs_factory=docs, sender=sender, calendar_factory=cal,
        )
        t = threading.Thread(target=_poll_fireflies, args=(ff, s, op, started_at),
                             name="ff", daemon=True); t.start(); threads.append(t)
    if (s.zoom_realtime_enabled and s.zoom_account_id
        and s.zoom_client_id and s.zoom_client_secret):
        zm = ZoomPipeline(
            settings=s,
            client=ZoomClient(
                account_id=s.zoom_account_id, client_id=s.zoom_client_id,
                client_secret=s.zoom_client_secret, api_base=s.zoom_api_base,
                oauth_url=s.zoom_oauth_url,
            ),
            llm_backend=llm, docs_factory=docs, sender=sender, calendar_factory=cal,
        )
        t = threading.Thread(target=_poll_zoom, args=(zm, s, op, started_at),
                             name="zoom", daemon=True); t.start(); threads.append(t)
    if not threads:
        log.error("no_pipelines_enabled"); return 2
    log.info("zoom_ff_runner_ready", threads=len(threads))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT,  lambda *a: stop.set())
    while not stop.is_set(): time.sleep(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
