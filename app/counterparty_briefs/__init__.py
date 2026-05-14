"""FR-CR-05-168 — Counterparty Briefs.

Event-trigger pipeline. As soon as a new Calendar event appears
with an external counterparty, the runner:
  1. Extracts {org_name, initial_persons} from the event (LLM).
  2. Deep-researches the org (`o4-mini-deep-research`).
  3. Picks ≤ N beneficiaries from leadership + attendees (LLM).
  4. Deep-researches each beneficiary.
  5. Creates one org Google Doc + N person Google Docs.
  6. Posts ONE grouped Slack DM with all links.

See SPEC_COUNTERPARTY_BRIEFS_v0.1.md for the full design.
"""
from app.counterparty_briefs.extract import (
    BeneficiaryCandidate,
    EventExtraction,
    PersonCandidate,
    extract_beneficiaries,
    extract_event_counterparties,
)
from app.counterparty_briefs.lookup import CounterpartyContext, lookup_org
from app.counterparty_briefs.research import (
    OrgResearch,
    PersonResearch,
    research_org,
    research_org_with_cache,
    research_person,
)
from app.counterparty_briefs.runner import CounterpartyBriefRunner

__all__ = [
    "BeneficiaryCandidate",
    "CounterpartyBriefRunner",
    "CounterpartyContext",
    "EventExtraction",
    "OrgResearch",
    "PersonCandidate",
    "PersonResearch",
    "extract_beneficiaries",
    "extract_event_counterparties",
    "lookup_org",
    "research_org",
    "research_org_with_cache",
    "research_person",
]
