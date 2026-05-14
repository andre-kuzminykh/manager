"""FR-CR-05-165 — Agenda runner (worker thread).

One tick:
  1. Skip immediately if AGENDA_ENABLED=false (no API calls,
     no log spam).
  2. Fetch Calendar events scheduled at
     ``now + lead_time ± window`` minutes.
  3. ``build_candidates`` — drop already-posted + not-recurring-yet.
  4. For each surviving candidate:
     a. ``compose_agenda`` via LLM.
     b. ``DocsExportService.export_summary`` → google_doc_url.
     c. ``slack.chat_postMessage`` → Slack DM (or channel).
     d. ``AgendaService.record_post`` → idempotency row.
  5. Sleep until the next tick.

Failure of one candidate must NEVER kill the loop. Each step logs
on error and skips that candidate; the tick continues.

Runs in a daemon thread spawned by main.py. Feature flag gate is
checked once at startup AND on each tick — flipping the env-var
takes effect on the next iteration without restart, when the
process loads settings dynamically (currently it doesn't, but the
double-check is cheap and future-proof).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.agenda.compose import compose_agenda
from app.agenda.service import AgendaCandidate, AgendaService, build_candidates
from app.agenda.slack_format import render_agenda_text
from app.config import Settings
from app.db import session_scope
from app.logging_setup import get_logger

log = get_logger(__name__)


def _pick_prior_doc_url(candidate: AgendaCandidate) -> str | None:
    """Return the most recent prior recording's `google_doc_url`,
    or None when no prior has a Doc.

    `prior_recordings` is already ordered «newest first» by
    `find_prior_recordings`.
    """
    for r in candidate.prior_recordings or []:
        url = (r.get("google_doc_url") or "").strip()
        if url:
            return url
    return None


def _doc_body(candidate: AgendaCandidate, output_md: str) -> str:
    """Wrap the LLM-rendered markdown with a stable header so the
    Doc title and the first heading match.

    FR-CR-05-167 polish 2026-05-14: prepend a «Подробно по прошлой
    встрече» section pointing to the Google Docs of the most
    recent prior recordings (operator wants one click to the last
    summary). Google Docs auto-detects bare URLs and renders them
    as hyperlinks — no extra Docs-API styling pass needed.
    """
    header = (
        f"# {candidate.scheduled_start_at.strftime('%d/%m')} — "
        f"Повестка ко встрече «{candidate.title}»\n\n"
    )
    # Up to 3 most-recent prior recordings with a non-empty
    # google_doc_url. Format: «DD/MM — <url>» one per line.
    links: list[str] = []
    for r in candidate.prior_recordings[:3]:
        url = (r.get("google_doc_url") or "").strip()
        if not url:
            continue
        date_label = ""
        meeting_iso = r.get("meeting_date") or ""
        if meeting_iso:
            try:
                date_label = (
                    datetime.fromisoformat(meeting_iso).strftime("%d/%m")
                )
            except ValueError:
                date_label = ""
        prefix = f"{date_label} — " if date_label else ""
        links.append(f"- {prefix}{url}")
    prior_section = ""
    if links:
        prior_section = (
            "## Подробно по прошлой встрече\n"
            + "\n".join(links)
            + "\n\n"
        )
    return header + prior_section + (output_md or "").strip() + "\n"


class AgendaRunner:
    """Thread-safe runner. Construct once, call `start()` from
    main, never restart in the same process."""

    def __init__(
        self,
        *,
        settings: Settings,
        slack_client: WebClient,
        llm_backend: Any,
        calendar_factory: Any,
        docs_factory: Any,
    ) -> None:
        self._settings = settings
        self._slack = slack_client
        self._llm = llm_backend
        self._calendar_factory = calendar_factory
        self._docs_factory = docs_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._svc = AgendaService()

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if not self._settings.agenda_enabled:
            log.info(
                "agenda_runner_disabled_by_env",
                hint="set AGENDA_ENABLED=true to enable",
            )
            return
        if not self._settings.agenda_slack_target_channel_id:
            log.warning(
                "agenda_runner_no_slack_target",
                hint="set AGENDA_SLACK_TARGET_CHANNEL_ID to enable",
            )
            return
        source = (self._settings.agenda_source or "calendar").strip().lower()
        if source == "calendar" and (
            self._calendar_factory is None
            and not self._settings.calendar_apps_script_url
        ):
            log.warning(
                "agenda_runner_no_calendar_source",
                hint=(
                    "AGENDA_SOURCE=calendar but neither Calendar OAuth "
                    "(GOOGLE_CALENDAR_CLIENT_ID) nor Apps Script "
                    "(CALENDAR_APPS_SCRIPT_URL) is configured. Set "
                    "AGENDA_SOURCE=zoom_pattern to skip Calendar entirely "
                    "and predict from zoom_recordings."
                ),
            )
            return
        log.info(
            "agenda_runner_starting",
            lead_minutes=self._settings.agenda_lead_time_minutes,
            window_minutes=self._settings.agenda_window_minutes,
            tick_seconds=self._settings.agenda_tick_interval_seconds,
            channel=self._settings.agenda_slack_target_channel_id,
        )
        self._thread = threading.Thread(
            target=self._loop, name="agenda-runner", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # -- main loop ----------------------------------------------------

    def _loop(self) -> None:
        interval = max(10, int(self._settings.agenda_tick_interval_seconds))
        while not self._stop_event.is_set():
            try:
                self._tick_once()
            except Exception as e:  # noqa: BLE001
                log.warning("agenda_tick_failed", error=str(e))
            # Wake-on-stop: sleep in small slices so a shutdown
            # signal returns the thread within ~1s.
            slept = 0
            while slept < interval and not self._stop_event.is_set():
                time.sleep(min(1, interval - slept))
                slept += 1

    def _tick_once(self) -> None:
        if not self._settings.agenda_enabled:
            return

        events = self._fetch_events()
        if not events:
            return

        with session_scope() as session:
            candidates = build_candidates(
                session,
                events=events,
                lookback_days=self._settings.agenda_lookback_days,
                min_prior_meetings=self._settings.agenda_min_prior_meetings,
                organizer_email=self._settings.zoom_required_email or None,
            )

        log.info(
            "agenda_tick_summary",
            events=len(events),
            candidates=len(candidates),
        )

        for c in candidates:
            try:
                self._process_candidate(c)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "agenda_candidate_failed",
                    calendar_event_id=c.calendar_event_id,
                    error=str(e),
                )

    # -- per-candidate steps ------------------------------------------

    def _fetch_events(self) -> list[dict[str, Any]]:
        """Fetch upcoming events.

        Three paths (priority by ``AGENDA_SOURCE``):
          - ``"zoom_pattern"`` (FR-CR-05-166): predict next
            recurring instances from `zoom_recordings` weekly
            patterns. NO Calendar API call.
          - ``"calendar"`` (default, FR-CR-05-165):
              1. Direct Calendar API (FR-CR-05-144) — when
                 ``GOOGLE_CALENDAR_CLIENT_ID`` + OAuth/SA available.
              2. Apps Script proxy (FR-CR-05-136) — fallback when
                 only ``CALENDAR_APPS_SCRIPT_URL`` is configured.
        """
        source = (self._settings.agenda_source or "calendar").strip().lower()

        if source == "zoom_pattern":
            return self._fetch_events_from_zoom_pattern()
        return self._fetch_events_from_calendar()

    def _fetch_events_from_zoom_pattern(self) -> list[dict[str, Any]]:
        from app.agenda.zoom_pattern import predict_upcoming_events
        from app.db import session_scope

        lead = int(self._settings.agenda_lead_time_minutes)
        window = max(1, int(self._settings.agenda_window_minutes))
        target = datetime.now(timezone.utc) + timedelta(minutes=lead)
        try:
            with session_scope() as session:
                events_raw = predict_upcoming_events(
                    session,
                    target_dt=target,
                    window_minutes=window,
                    lookback_days=self._settings.agenda_lookback_days,
                    min_prior_meetings=max(
                        2, self._settings.agenda_min_prior_meetings,
                    ),
                    host_email=self._settings.zoom_required_email or None,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "agenda_zoom_pattern_failed", error=str(e),
            )
            return []
        return [self._normalise_event(ev) for ev in events_raw if ev]

    def _fetch_events_from_calendar(self) -> list[dict[str, Any]]:
        from app.services.calendar_match import (
            fetch_calendar_events_around,
            fetch_calendar_events_via_api,
        )

        lead = int(self._settings.agenda_lead_time_minutes)
        window = max(1, int(self._settings.agenda_window_minutes))
        target = datetime.now(timezone.utc) + timedelta(minutes=lead)

        events_raw: list[dict[str, Any]] = []
        if self._calendar_factory is not None:
            try:
                events_raw = fetch_calendar_events_via_api(
                    meeting_dt=target,
                    window_minutes=window,
                    credentials_factory=self._calendar_factory,
                    calendar_id=self._settings.google_calendar_id or "primary",
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "agenda_calendar_api_failed", error=str(e),
                )

        if not events_raw and self._settings.calendar_apps_script_url:
            try:
                events_raw = fetch_calendar_events_around(
                    meeting_dt=target,
                    window_minutes=window,
                    apps_script_url=self._settings.calendar_apps_script_url,
                    shared_token=self._settings.calendar_apps_script_shared_token,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "agenda_calendar_apps_script_failed", error=str(e),
                )

        return [self._normalise_event(ev) for ev in events_raw if ev]

    @staticmethod
    def _normalise_event(ev: dict[str, Any]) -> dict[str, Any] | None:
        """Shape one Calendar event for the rest of the pipeline.

        FR-CR-05-165: when the source doesn't supply a stable id
        (Apps Script proxy is title+time only), synthesise one
        from `title:start_iso` so the idempotency row in
        `meeting_agendas` is still unique-per-event AND stable
        across ticks within the lead-time window.
        """
        title = (ev.get("title") or "").strip()
        start = ev.get("start")
        if not title or start is None:
            return None
        ev_id = (
            ev.get("id")
            or ev.get("event_id")
            or ev.get("ical_uid")
            or ""
        ).strip()
        if not ev_id:
            # Synthetic: normalised title + ISO start, no leakage
            # of Calendar internals into our DB.
            from app.agenda.service import normalise_title

            start_iso = (
                start.isoformat() if hasattr(start, "isoformat") else str(start)
            )
            ev_id = f"agenda_synth:{normalise_title(title)}:{start_iso}"
        return {
            "id": ev_id,
            "title": title,
            "start": start,
            "end": ev.get("end"),
            "description": ev.get("description") or "",
            "attendees": ev.get("attendees") or [],
            "organizer": ev.get("organizer") or {},
            "recurring_event_id": ev.get("recurring_event_id"),
        }

    def _process_candidate(self, candidate: AgendaCandidate) -> None:
        # Idempotency double-check — in case the tick window
        # overlaps with itself.
        with session_scope() as session:
            if self._svc.is_already_posted(
                session, calendar_event_id=candidate.calendar_event_id
            ):
                return

        model = (
            self._settings.agenda_compose_model
            or self._settings.openai_model
        )
        output = compose_agenda(
            candidate, llm_backend=self._llm, model=model,
        )
        if output is None:
            return

        # FR-CR-05-167 operator-pinned 2026-05-14: «гиперссылка должна
        # вести на саммери встречи предыдущей». Prefer the most
        # recent prior recording's Google Doc as the Slack-header
        # hyperlink. Fall back to our own (newly-created) Doc when
        # no prior has a doc_url, and to plain text when neither
        # is available.
        prior_doc_url = _pick_prior_doc_url(candidate)
        own_doc_url, own_doc_id = self._maybe_create_doc(
            candidate, output.doc_body_md,
        )
        header_url = prior_doc_url or own_doc_url

        text = render_agenda_text(
            candidate=candidate, output=output, doc_url=header_url,
        )
        slack_ts = self._send_slack_dm(text)
        if not slack_ts:
            return

        # Persist idempotency row in a fresh session.
        with session_scope() as session:
            self._svc.record_post(
                session,
                candidate=candidate,
                slack_channel=self._settings.agenda_slack_target_channel_id,
                slack_ts=slack_ts,
                google_doc_id=own_doc_id,
                google_doc_url=own_doc_url,
                prior_zoom_ids=[
                    r.get("zoom_id") for r in candidate.prior_recordings
                    if r.get("zoom_id")
                ],
            )
        log.info(
            "agenda_posted",
            calendar_event_id=candidate.calendar_event_id,
            title=candidate.title,
            slack_ts=slack_ts,
            header_doc_url=header_url,
            own_doc_url=own_doc_url,
        )

    def _maybe_create_doc(
        self, candidate: AgendaCandidate, body_md: str
    ) -> tuple[str | None, str | None]:
        if not self._docs_factory:
            return None, None
        try:
            svc = self._docs_factory()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "agenda_docs_factory_failed",
                calendar_event_id=candidate.calendar_event_id,
                error=str(e),
            )
            return None, None
        if svc is None:
            return None, None
        try:
            title = (
                f"{candidate.scheduled_start_at.strftime('%d/%m')} — "
                f"Повестка — {candidate.title}"
            )
            doc_id, doc_url = svc.export_summary(
                title=title,
                body=_doc_body(candidate, body_md),
                parent_folder_id=self._settings.fireflies_docs_folder_id or "",
            )
            return doc_url, doc_id
        except Exception as e:  # noqa: BLE001
            log.warning(
                "agenda_doc_export_failed",
                calendar_event_id=candidate.calendar_event_id,
                error=str(e),
            )
            return None, None

    def _send_slack_dm(self, text: str) -> str | None:
        try:
            resp = self._slack.chat_postMessage(
                channel=self._settings.agenda_slack_target_channel_id,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
            if resp.get("ok"):
                return resp.get("ts")
            log.warning(
                "agenda_slack_post_failed_response",
                response=dict(resp),
            )
        except SlackApiError as e:
            log.warning("agenda_slack_post_failed", error=str(e))
        return None


__all__ = ["AgendaRunner"]
