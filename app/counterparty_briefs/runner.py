"""FR-CR-05-168 — Counterparty Briefs runner.

Daemon thread. Every ``COUNTERPARTY_BRIEFS_TICK_INTERVAL_SECONDS``
the runner:

  1. Pulls Calendar events in
     ``[now, now + LOOKAHEAD_DAYS]``.
  2. Drops events where ``organizer.email`` or ``creator.email``
     ≠ operator (FR-CR-05-167 gate, reused).
  3. Skips events already in ``counterparty_briefs_events``.
  4. For each remaining event:
     a. Extracts ``{org_name, initial_persons}`` (LLM).
     b. Cached org research (TTL) or fresh
        ``o4-mini-deep-research`` call.
     c. ``extract_beneficiaries`` → ≤ N persons.
     d. Per-beneficiary cached or fresh deep research.
     e. Creates org Doc + N person Docs (cached briefs re-use
        the existing ``google_doc_url``).
     f. Posts ONE grouped Slack DM.
     g. Persists ``counterparty_briefs_events`` row + N
        ``counterparty_brief_links`` rows.

Cost is tracked per-event against ``LLM_BUDGET_USD``; when the
cap is hit, remaining person research calls are skipped and
their entries in the DM render as «N/A (research_failed)».
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from sqlalchemy.orm import Session

from app.config import Settings
from app.counterparty_briefs.doc import (
    build_doc_title,
    build_org_doc_body,
    build_person_doc_body,
)
from app.counterparty_briefs.extract import (
    BeneficiaryCandidate,
    extract_beneficiaries,
    extract_event_counterparties,
)
from app.counterparty_briefs.lookup import (
    CounterpartyContext,
    lookup_org,
    normalise_counterparty_name,
)
from app.counterparty_briefs.research import (
    OrgResearch,
    PersonResearch,
    research_org_with_cache,
    research_person,
)
from app.counterparty_briefs.slack_format import (
    render_org_top_message,
    render_person_thread_reply,
)
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import (
    CounterpartyBrief,
    CounterpartyBriefLink,
    CounterpartyBriefsEvent,
)

log = get_logger(__name__)


# -- helpers shared with tests + CLI ----------------------------------------

def compute_lookahead_window(
    settings: Settings, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    return now, now + timedelta(
        days=max(1, int(settings.counterparty_briefs_lookahead_days))
    )


def event_passes_host_gate(event: dict[str, Any], operator_email: str) -> bool:
    """Inherit the FR-CR-05-167 organizer + creator gate."""
    op = (operator_email or "").strip().lower()
    if not op:
        return True

    def _email_of(field: Any) -> str:
        if isinstance(field, dict):
            return (field.get("email") or "").strip().lower()
        if isinstance(field, str):
            return field.strip().lower()
        return ""

    org_email = _email_of(event.get("organizer"))
    creator_email = _email_of(event.get("creator"))
    if op not in {org_email, creator_email}:
        return False
    if creator_email and creator_email != op:
        return False
    return True


def event_already_processed(
    session: Session, *, calendar_event_id: str
) -> bool:
    if not calendar_event_id:
        return False
    return (
        session.query(CounterpartyBriefsEvent)
        .filter(CounterpartyBriefsEvent.calendar_event_id == calendar_event_id)
        .first()
        is not None
    )


def find_cached_brief(
    session: Session, *, counterparty_key: str, ttl_days: int
) -> CounterpartyBrief | None:
    if not counterparty_key:
        return None
    row = (
        session.query(CounterpartyBrief)
        .filter(CounterpartyBrief.counterparty_key == counterparty_key)
        .first()
    )
    if row is None or row.researched_at is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(1, int(ttl_days))
    )
    researched_at = row.researched_at
    if researched_at.tzinfo is None:
        researched_at = researched_at.replace(tzinfo=timezone.utc)
    if researched_at < cutoff:
        return None
    return row


# -- dataclasses for per-event state ----------------------------------------

@dataclass
class _BriefLink:
    """Description of one brief link inside the Slack DM."""

    kind: str  # "org" | "person"
    display_name: str
    role: str | None = None
    doc_url: str | None = None
    brief_id: int | None = None
    cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    note: str | None = None


# -- runner ------------------------------------------------------------------

class CounterpartyBriefRunner:
    """Construct once, ``.start()`` from main."""

    def __init__(
        self,
        *,
        settings: Settings,
        slack_client: WebClient,
        llm_backend: Any,
        calendar_factory: Any,
        docs_factory: Any,
        operator_email: str | None = None,
    ) -> None:
        self._settings = settings
        self._slack = slack_client
        self._llm = llm_backend
        self._calendar_factory = calendar_factory
        self._docs_factory = docs_factory
        # Default to FR-CR-05-143 host filter — same env-var.
        self._operator_email = (
            operator_email
            or (getattr(settings, "zoom_required_email", "") or "")
            or "1@thehumanoid.ai"
        ).strip().lower()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if not self._settings.counterparty_briefs_enabled:
            log.info(
                "brief_runner_disabled_by_env",
                hint="set COUNTERPARTY_BRIEFS_ENABLED=true to enable",
            )
            return
        if not self._settings.counterparty_briefs_slack_target_channel_id:
            log.warning(
                "brief_runner_no_slack_target",
                hint="set COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID",
            )
            return
        log.info(
            "brief_runner_starting",
            lookahead_days=self._settings.counterparty_briefs_lookahead_days,
            tick_seconds=self._settings.counterparty_briefs_tick_interval_seconds,
            ttl_days=self._settings.counterparty_briefs_cache_ttl_days,
            channel=self._settings.counterparty_briefs_slack_target_channel_id,
        )
        self._thread = threading.Thread(
            target=self._loop, name="counterparty-brief-runner", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # -- main loop ----------------------------------------------------

    def _loop(self) -> None:
        interval = max(
            30, int(self._settings.counterparty_briefs_tick_interval_seconds)
        )
        while not self._stop_event.is_set():
            try:
                self._tick_once()
            except Exception as e:  # noqa: BLE001
                log.warning("brief_tick_failed", error=str(e))
            slept = 0
            while slept < interval and not self._stop_event.is_set():
                time.sleep(min(1, interval - slept))
                slept += 1

    def _tick_once(self) -> None:
        if not self._settings.counterparty_briefs_enabled:
            return
        events = self._fetch_events()
        if not events:
            return
        log.info("brief_tick_summary", events=len(events))
        for ev in events:
            try:
                self.process_event(ev)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "brief_event_failed",
                    event_id=(ev or {}).get("id"),
                    error=str(e),
                )

    # -- event fetch + filter ----------------------------------------

    def _fetch_events(self) -> list[dict[str, Any]]:
        from app.services.calendar_match import fetch_calendar_events_via_api

        if self._calendar_factory is None:
            return []
        now, latest = compute_lookahead_window(self._settings)
        # Use centre + window encoding the same helper agenda uses.
        half = int((latest - now).total_seconds() / 60 / 2) or 1
        centre = now + (latest - now) / 2
        try:
            raw = fetch_calendar_events_via_api(
                meeting_dt=centre,
                window_minutes=half,
                credentials_factory=self._calendar_factory,
                calendar_id=self._settings.google_calendar_id or "primary",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("brief_calendar_api_failed", error=str(e))
            return []
        return [self._normalise_event(e) for e in raw if e]

    @staticmethod
    def _normalise_event(ev: dict[str, Any]) -> dict[str, Any] | None:
        title = (ev.get("title") or "").strip()
        start = ev.get("start")
        if not title or start is None:
            return None
        ev_id = (
            ev.get("id") or ev.get("event_id") or ev.get("ical_uid") or ""
        ).strip()
        if not ev_id:
            from app.agenda.service import normalise_title

            start_iso = (
                start.isoformat() if hasattr(start, "isoformat") else str(start)
            )
            ev_id = f"brief_synth:{normalise_title(title)}:{start_iso}"
        return {
            "id": ev_id,
            "title": title,
            "start": start,
            "end": ev.get("end"),
            "description": ev.get("description") or "",
            "attendees": ev.get("attendees") or [],
            "organizer": ev.get("organizer") or {},
            "creator": ev.get("creator") or {},
        }

    # -- per-event pipeline ------------------------------------------

    def process_event(self, ev: dict[str, Any], *, skip_slack: bool = False) -> None:
        """Run the full pipeline for a single event. Used by the
        daemon AND the one-shot CLI.

        ``skip_slack=True`` — operator-pinned test mode: do the
        full LLM + Docs work and persist the brief rows, but DO
        NOT post a Slack DM. Lets the operator inspect generated
        Docs before the daemon starts blasting messages.
        """
        ev_id = (ev or {}).get("id") or ""
        if not ev_id:
            return
        if not event_passes_host_gate(ev, self._operator_email):
            return

        with session_scope() as s:
            if event_already_processed(s, calendar_event_id=ev_id):
                return

        # Stage 0 — extract org + initial_persons.
        extract_model = (
            self._settings.counterparty_briefs_extract_model
            or self._settings.openai_model
        )
        ex = extract_event_counterparties(
            event=ev, llm_backend=self._llm, model=extract_model,
        )
        if not ex.org_name and not ex.initial_persons:
            log.info("brief_no_external_counterparty", event_id=ev_id)
            return

        budget = float(self._settings.counterparty_briefs_llm_budget_usd)
        spent: float = 0.0
        links: list[_BriefLink] = []

        org_brief_row: CounterpartyBrief | None = None
        org_research: OrgResearch | None = None

        # Stage 1 — org research (cached).
        if ex.org_name:
            org_key = normalise_counterparty_name(ex.org_name)
            with session_scope() as s:
                cached_row = find_cached_brief(
                    s,
                    counterparty_key=org_key,
                    ttl_days=self._settings.counterparty_briefs_cache_ttl_days,
                )
                if cached_row is not None:
                    # FR-CR-05-168 hotfix 2026-05-18: load the FULL
                    # payload from cache — earlier we only kept
                    # `leadership`, which left `overview_paragraph`
                    # empty and produced a Slack DM with no org gist
                    # under the hyperlink.
                    from app.counterparty_briefs.research import _coerce_org
                    payload = cached_row.research_payload or {}
                    # Preserve display name from the row in case the
                    # payload is missing it.
                    payload = {**payload, "name": payload.get("name") or cached_row.display_name}
                    org_research = _coerce_org(payload)
                    org_brief_row_id = cached_row.id
                    links.append(_BriefLink(
                        kind="org",
                        display_name=cached_row.display_name,
                        doc_url=cached_row.google_doc_url,
                        brief_id=cached_row.id,
                        cost_usd=Decimal("0"),
                    ))
                    org_brief_row = cached_row  # noqa: F841 (kept for symmetry)
                    log.info(
                        "brief_org_cache_hit",
                        org=ex.org_name, brief_id=cached_row.id,
                    )

            if org_research is None:
                cached = research_org_with_cache(
                    org_name=ex.org_name,
                    session=None,  # avoid double DB query; we already checked above
                    ttl_days=self._settings.counterparty_briefs_cache_ttl_days,
                    llm_backend=self._llm,
                    model=self._settings.counterparty_briefs_research_model,
                    budget_usd=budget,
                    spent_usd=spent,
                ) if False else None
                # Simpler: just call research_org directly (cache layer
                # already handled above).
                from app.counterparty_briefs.research import (
                    _estimate_cost_usd,
                    research_org,
                )
                est_cost = _estimate_cost_usd(
                    model=self._settings.counterparty_briefs_research_model,
                    prompt_chars=len(ex.org_name),
                )
                org_research = research_org(
                    org_name=ex.org_name,
                    llm_backend=self._llm,
                    model=self._settings.counterparty_briefs_research_model,
                    budget_usd=budget, spent_usd=spent,
                )
                if org_research is not None:
                    spent += est_cost
                    # Persist + create Doc.
                    doc_url, doc_id = self._maybe_create_doc(
                        title=build_doc_title(
                            kind="org",
                            display_name=org_research.name or ex.org_name,
                            scheduled_at=ev["start"]
                            if isinstance(ev["start"], datetime)
                            else datetime.now(timezone.utc),
                        ),
                        body=build_org_doc_body(
                            org_name=ex.org_name,
                            context=self._safe_lookup(ex.org_name),
                            research=org_research,
                        ),
                    )
                    with session_scope() as s:
                        row = CounterpartyBrief(
                            counterparty_key=org_key,
                            kind="org",
                            display_name=org_research.name or ex.org_name,
                            org_name=ex.org_name,
                            research_payload=_org_to_payload(org_research),
                            cost_usd=Decimal(str(est_cost)),
                            google_doc_id=doc_id,
                            google_doc_url=doc_url,
                            researched_at=datetime.now(timezone.utc),
                        )
                        s.add(row)
                        s.flush()
                        links.append(_BriefLink(
                            kind="org",
                            display_name=row.display_name,
                            doc_url=doc_url,
                            brief_id=row.id,
                            cost_usd=row.cost_usd,
                        ))
                else:
                    log.warning(
                        "brief_org_research_skipped", org=ex.org_name,
                    )

        # Stage 2 — beneficiary picker.
        beneficiaries: list[BeneficiaryCandidate] = []
        if org_research is not None or ex.initial_persons:
            beneficiaries = extract_beneficiaries(
                org_research=org_research,
                attendees=ev.get("attendees") or [],
                initial_persons=ex.initial_persons,
                max_n=self._settings.counterparty_briefs_max_beneficiaries,
                llm_backend=self._llm,
                model=extract_model,
            )

        # Stage 3 — per-beneficiary research + Doc.
        for b in beneficiaries:
            person_key = normalise_counterparty_name(b.person_name)
            cached_row: CounterpartyBrief | None = None
            with session_scope() as s:
                cached_row = find_cached_brief(
                    s,
                    counterparty_key=person_key,
                    ttl_days=self._settings.counterparty_briefs_cache_ttl_days,
                )
                if cached_row is not None:
                    links.append(_BriefLink(
                        kind="person",
                        display_name=cached_row.display_name,
                        role=b.person_role,
                        doc_url=cached_row.google_doc_url,
                        brief_id=cached_row.id,
                        cost_usd=Decimal("0"),
                    ))
                    log.info(
                        "brief_person_cache_hit",
                        person=b.person_name, brief_id=cached_row.id,
                    )

            if cached_row is not None:
                continue

            from app.counterparty_briefs.research import _estimate_cost_usd

            est_cost = _estimate_cost_usd(
                model=self._settings.counterparty_briefs_research_model,
                prompt_chars=len(b.person_name),
            )
            if spent + est_cost > budget:
                links.append(_BriefLink(
                    kind="person",
                    display_name=b.person_name,
                    role=b.person_role,
                    doc_url=None,
                    note="research_budget_exhausted",
                ))
                log.info(
                    "brief_research_budget_exhausted",
                    person=b.person_name, spent_usd=spent, budget=budget,
                )
                continue

            person_research = research_person(
                beneficiary=b,
                org_name=ex.org_name,
                llm_backend=self._llm,
                model=self._settings.counterparty_briefs_research_model,
                budget_usd=budget, spent_usd=spent,
            )
            if person_research is None:
                links.append(_BriefLink(
                    kind="person",
                    display_name=b.person_name,
                    role=b.person_role,
                    doc_url=None,
                    note="research_failed",
                ))
                continue
            spent += est_cost

            doc_url, doc_id = self._maybe_create_doc(
                title=build_doc_title(
                    kind="person", display_name=b.person_name,
                    scheduled_at=ev["start"]
                    if isinstance(ev["start"], datetime)
                    else datetime.now(timezone.utc),
                ),
                body=build_person_doc_body(
                    beneficiary=b,
                    context=self._safe_lookup(ex.org_name)
                    if ex.org_name else None,
                    research=person_research,
                ),
            )
            with session_scope() as s:
                row = CounterpartyBrief(
                    counterparty_key=person_key,
                    kind="person",
                    display_name=b.person_name,
                    org_name=ex.org_name,
                    research_payload=_person_to_payload(person_research),
                    cost_usd=Decimal(str(est_cost)),
                    google_doc_id=doc_id,
                    google_doc_url=doc_url,
                    researched_at=datetime.now(timezone.utc),
                )
                s.add(row)
                s.flush()
                links.append(_BriefLink(
                    kind="person",
                    display_name=b.person_name,
                    role=b.person_role,
                    doc_url=doc_url,
                    brief_id=row.id,
                    cost_usd=row.cost_usd,
                ))

        # Slack DM + idempotency row.
        if not any(
            (lk.doc_url or lk.note) for lk in links
        ):
            log.info("brief_event_yielded_no_briefs", event_id=ev_id)
            return

        # FR-CR-05-168 polish 2026-05-18: each Slack line carries
        # a one-line «gist» (overview paragraph) so the DM is
        # readable without clicking each Doc. Pull org gist from
        # OrgResearch.overview_paragraph; person gist from
        # PersonResearch.profile_overview.
        org_payload: dict[str, Any] | None = None
        for lk in links:
            if lk.kind != "org" or not lk.doc_url:
                continue
            org_payload = {
                "display_name": lk.display_name,
                "doc_url": lk.doc_url,
                "gist": (
                    org_research.overview_paragraph
                    if org_research is not None else ""
                ),
            }
            break

        # Build a lookup of person_key → research payload so we
        # can pull `profile_overview` for each person link.
        person_research_by_key: dict[str, PersonResearch] = {}
        try:
            with session_scope() as _s:
                rows = (
                    _s.query(CounterpartyBrief)
                    .filter(CounterpartyBrief.kind == "person")
                    .filter(CounterpartyBrief.id.in_([
                        lk.brief_id for lk in links
                        if lk.kind == "person" and lk.brief_id
                    ] or [-1]))
                    .all()
                )
                for r in rows:
                    payload = r.research_payload or {}
                    person_research_by_key[r.counterparty_key] = PersonResearch(
                        profile_overview=str(
                            payload.get("profile_overview") or ""
                        ),
                    )
        except Exception as e:  # noqa: BLE001
            log.info("brief_person_gist_lookup_failed", error=str(e))

        person_payloads: list[dict[str, Any]] = []
        for lk in links:
            if lk.kind != "person":
                continue
            key = normalise_counterparty_name(lk.display_name)
            gist_text = ""
            pr = person_research_by_key.get(key)
            if pr is not None:
                gist_text = pr.profile_overview
            person_payloads.append({
                "display_name": lk.display_name,
                "role": lk.role,
                "doc_url": lk.doc_url,
                "note": lk.note,
                "gist": gist_text,
            })

        scheduled_at = (
            ev["start"]
            if isinstance(ev["start"], datetime)
            else datetime.now(timezone.utc)
        )
        top_text = render_org_top_message(
            event_title=ev.get("title") or "",
            scheduled_at=scheduled_at,
            org_brief=org_payload,
            person_count=len(person_payloads),
        )
        ts: str | None = None
        if skip_slack:
            log.info(
                "brief_event_slack_skipped",
                event_id=ev_id,
                hint="--skip-slack — Docs created, no DM sent",
            )
        else:
            ts = self._send_slack_dm(text=top_text)
            if not ts:
                return
            # Operator-pinned 2026-05-18: persons go as thread
            # replies under the org top message so the DM
            # surface stays clean.
            for person in person_payloads:
                reply_text = render_person_thread_reply(person=person)
                reply_ts = self._send_slack_dm(
                    text=reply_text, thread_ts=ts,
                )
                if not reply_ts:
                    log.warning(
                        "brief_person_thread_reply_failed",
                        event_id=ev_id,
                        person=person.get("display_name"),
                    )

        with session_scope() as s:
            evrow = CounterpartyBriefsEvent(
                calendar_event_id=ev_id,
                event_title=ev.get("title") or "",
                scheduled_meeting_at=ev["start"]
                if isinstance(ev["start"], datetime)
                else datetime.now(timezone.utc),
                posted_at=datetime.now(timezone.utc),
                slack_channel=self._settings.counterparty_briefs_slack_target_channel_id,
                slack_ts=ts,
                total_cost_usd=Decimal(str(spent)),
                link_summary=[
                    {
                        "kind": lk.kind,
                        "display_name": lk.display_name,
                        "doc_url": lk.doc_url,
                        "brief_id": lk.brief_id,
                        "note": lk.note,
                    }
                    for lk in links
                ],
            )
            s.add(evrow)
            s.flush()
            for lk in links:
                if lk.brief_id is None:
                    continue
                s.add(CounterpartyBriefLink(
                    event_id=evrow.id, brief_id=lk.brief_id,
                ))
        log.info(
            "brief_event_posted",
            event_id=ev_id, links=len(links),
            spent_usd=spent, slack_ts=ts,
        )

    # -- helpers ------------------------------------------------------

    def _safe_lookup(self, org_name: str | None) -> CounterpartyContext | None:
        if not org_name:
            return None
        try:
            with session_scope() as s:
                return lookup_org(s, org_name=org_name)
        except Exception as e:  # noqa: BLE001
            log.warning("brief_lookup_failed", org=org_name, error=str(e))
            return None

    def _maybe_create_doc(
        self, *, title: str, body: str,
    ) -> tuple[str | None, str | None]:
        """FR-CR-05-168 — render Markdown body to HTML, upload as
        Drive file with mimeType=document. Drive auto-converts:
        headings, bold/italic, hyperlinks and inline `<img>`
        all render natively in the resulting Google Doc.

        Falls back to plain-text `export_summary` if the new
        `export_html_as_doc` method is not present (older
        DocsExportService deployments).
        """
        from app.counterparty_briefs.doc import markdown_to_html

        if self._docs_factory is None:
            return None, None
        try:
            svc = self._docs_factory()
        except Exception as e:  # noqa: BLE001
            log.warning("brief_docs_factory_failed", error=str(e))
            return None, None
        if svc is None:
            return None, None
        try:
            html = markdown_to_html(body)
            if hasattr(svc, "export_html_as_doc"):
                doc_id, doc_url = svc.export_html_as_doc(
                    title=title, html_body=html,
                    parent_folder_id=getattr(
                        self._settings, "fireflies_docs_folder_id", ""
                    ) or "",
                )
            else:
                doc_id, doc_url = svc.export_summary(
                    title=title, body=body,
                    parent_folder_id=getattr(
                        self._settings, "fireflies_docs_folder_id", ""
                    ) or "",
                )
            return doc_url, doc_id
        except Exception as e:  # noqa: BLE001
            log.warning("brief_doc_export_failed", title=title, error=str(e))
            return None, None

    def _send_slack_dm(
        self, *, text: str, thread_ts: str | None = None,
    ) -> str | None:
        kwargs: dict[str, Any] = {
            "channel": self._settings.counterparty_briefs_slack_target_channel_id,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            resp = self._slack.chat_postMessage(**kwargs)
            if resp.get("ok"):
                return resp.get("ts")
            log.warning(
                "brief_slack_post_failed_response", response=dict(resp),
            )
        except SlackApiError as e:
            log.warning("brief_slack_post_failed", error=str(e))
        return None


# -- payload serialisers ----------------------------------------------------

def _org_to_payload(o: OrgResearch) -> dict[str, Any]:
    return {
        "name": o.name,
        "official_name": o.official_name,
        "website": o.website,
        "headquarters": o.headquarters,
        "type": o.type,
        "sector_focus": list(o.sector_focus),
        "leadership": list(o.leadership),
        "portfolio_highlights": list(o.portfolio_highlights),
        "recent_news": list(o.recent_news),
        "overview_paragraph": o.overview_paragraph,
    }


def _person_to_payload(p: PersonResearch) -> dict[str, Any]:
    return {
        "photo_url": p.photo_url,
        "personal_information": dict(p.personal_information),
        "profile_overview": p.profile_overview,
        "current_positions": list(p.current_positions),
        "previous_positions": list(p.previous_positions),
        "investment_highlights": dict(p.investment_highlights),
        "investments": list(p.investments),
        "exits": p.exits,
        "achievements": list(p.achievements),
        "honors_awards": list(p.honors_awards),
        "education": list(p.education),
        "publications": list(p.publications),
        "skills": list(p.skills),
        "languages": list(p.languages),
    }


__all__ = [
    "CounterpartyBriefRunner",
    "compute_lookahead_window",
    "event_already_processed",
    "event_passes_host_gate",
    "find_cached_brief",
]
