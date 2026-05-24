from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    ActionDraft,
    ActionDraftState,
    ContextSnapshot,
    IntentInference,
)
from app.models.intent import IntentType as IntentTypeEnum
from app.logging_setup import get_logger
from app.schemas.intent import IntentClassification, IntentType, InvocationType

log = get_logger(__name__)


class ConfidenceBucket(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


@dataclass
class PassiveDecision:
    action: str  # "card" | "soft_prompt" | "silent"
    confidence_bucket: ConfidenceBucket
    draft_id: int | None


def bucket_for(confidence: float, settings: Settings) -> ConfidenceBucket:
    if confidence >= settings.intent_confidence_high:
        return ConfidenceBucket.high
    if confidence >= settings.intent_confidence_low:
        return ConfidenceBucket.medium
    return ConfidenceBucket.low


class Orchestrator:
    """Turns an IntentClassification into persisted drafts and a UX decision."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- persistence ------------------------------------------------------

    def persist_context_snapshot(
        self, session: Session, snapshot_dict: dict[str, Any]
    ) -> ContextSnapshot:
        snap = ContextSnapshot(
            conversation_id=snapshot_dict["conversation_id"],
            source_ts=snapshot_dict["source_ts"],
            thread_ts=snapshot_dict.get("thread_ts"),
            source_message=snapshot_dict["source_message"],
            history_before=snapshot_dict.get("history_before", []),
            thread_messages=snapshot_dict.get("thread_messages", []),
        )
        session.add(snap)
        session.flush()
        return snap

    def persist_inference(
        self,
        session: Session,
        *,
        context_snapshot: ContextSnapshot,
        classification: IntentClassification,
        invocation_type: InvocationType,
    ) -> IntentInference:
        inference = IntentInference(
            context_snapshot_id=context_snapshot.id,
            intent=IntentTypeEnum(classification.intent.value),
            confidence=classification.confidence,
            invocation_type=invocation_type.value,
            reasoning=classification.reasoning,
            raw={
                "task": classification.task.model_dump(mode="json")
                if classification.task
                else None,
                "meeting": classification.meeting.model_dump(mode="json")
                if classification.meeting
                else None,
            },
        )
        session.add(inference)
        session.flush()
        return inference

    def create_draft(
        self,
        session: Session,
        *,
        inference: IntentInference,
        classification: IntentClassification,
        created_by_slack_user_id: str | None,
        slack_message_ts: str | None,
    ) -> ActionDraft:
        payload = classification.draft_payload() or {}
        # FR-CR-05-163 — classify the draft by strategic direction at
        # creation (gpt-4o-mini), so it's «уже классифицировано» downstream
        # (digest just reads payload["direction"]; no re-classification).
        # Only for task intents; best-effort — never block draft creation.
        if classification.intent in (IntentType.create_task, IntentType.update_task):
            self.classify_draft_direction(payload)
        draft = ActionDraft(
            inference_id=inference.id,
            intent=IntentTypeEnum(classification.intent.value),
            state=ActionDraftState.proposed,
            payload=payload,
            created_by_slack_user_id=created_by_slack_user_id,
            slack_message_ts=slack_message_ts,
        )
        session.add(draft)
        session.flush()
        return draft

    def _classify_backend(self):
        """Lazy, cached OpenAI backend for direction classification."""
        be = getattr(self, "_cls_backend", None)
        if be is None:
            from openai import OpenAI

            from app.intent.llm_backends import OpenAIBackend

            be = OpenAIBackend(
                client=OpenAI(api_key=self._settings.openai_api_key),
                model=self._settings.openai_model,
            )
            self._cls_backend = be
        return be

    def classify_draft_direction(self, payload: dict[str, Any]) -> None:
        """Set payload["direction"] in-place via gpt-4o-mini. No-op for
        meeting drafts / missing title / missing API key. Best-effort."""
        title = (payload.get("title") or "").strip()
        if not title or not getattr(self._settings, "openai_api_key", ""):
            return
        try:
            from app.services.task_direction import classify_one_direction

            payload["direction"] = classify_one_direction(
                title=title,
                description=(payload.get("description") or ""),
                llm_backend=self._classify_backend(),
                model=self._settings.openai_model,
            )
        except Exception as e:  # noqa: BLE001 — must never break ingest
            log.warning("draft_direction_classify_failed", error=str(e))

    # ---- routing ----------------------------------------------------------

    def decide_passive(
        self,
        *,
        classification: IntentClassification,
        draft_id: int | None,
    ) -> PassiveDecision:
        """Passive UX rule (per product decision 2026-04-24):

        Passive NEVER auto-creates. It only OFFERS. The author explicitly
        confirms (or ignores) in the thread. Auto-create is reserved for
        the @mention path, which has its own handler.

        Mapping:
          no_action               → silent
          create_*  high conf     → soft_prompt  (was: card)
          create_*  medium conf   → soft_prompt  (unchanged)
          create_*  low conf      → silent       (unchanged)
        """
        if classification.intent == IntentType.no_action:
            return PassiveDecision(
                action="silent",
                confidence_bucket=ConfidenceBucket.low,
                draft_id=None,
            )

        bucket = bucket_for(classification.confidence, self._settings)
        if bucket in (ConfidenceBucket.high, ConfidenceBucket.medium):
            return PassiveDecision(
                action="soft_prompt", confidence_bucket=bucket, draft_id=draft_id
            )
        return PassiveDecision(action="silent", confidence_bucket=bucket, draft_id=None)
