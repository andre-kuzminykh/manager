"""Process Telegram source messages through the existing intent
pipeline and persist the resulting tasks in our local DB."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.context.retriever import ContextWindow
from app.intent import IntentClassifier
from app.logging_setup import get_logger
from app.models import (
    ActionDraft,
    ActionDraftState,
    ProcessedTelegramMessage,
    Task,
    TaskSourceKind,
)
from app.orchestrator import Orchestrator
from app.persistence import create_task_from_draft
from app.schemas.intent import InvocationType, IntentType
from app.telegram_ingest.reader import TelegramSourceMessage

log = get_logger(__name__)


@dataclass
class IngestReport:
    """Counters returned from a batch run."""

    seen: int = 0
    skipped_already_processed: int = 0
    skipped_empty_text: int = 0
    no_action: int = 0
    tasks_created: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def _build_window(
    msg: TelegramSourceMessage,
    *,
    history_before: list[dict] | None = None,
) -> ContextWindow:
    """Wrap a TelegramSourceMessage in a Slack-shaped ContextWindow.

    The intent pipeline reads ``conversation_id`` / ``source_ts`` /
    ``source_message`` — we hand it Telegram identifiers in the same
    fields so no pipeline code changes.

    ``history_before`` is the optional adaptive-context window
    (FR-CR-05-09) — chronological list of recent chat messages
    feeding the detect / title / owner stages so vague phrases
    like «хорошо! напишу ему» get rewritten into proper imperative
    titles using the surrounding conversation.
    """
    return ContextWindow(
        conversation_id=str(msg.chat_id),
        source_ts=str(msg.message_id),
        thread_ts=str(msg.reply_to) if msg.reply_to else None,
        source_message={
            "ts": str(msg.message_id),
            "thread_ts": str(msg.reply_to) if msg.reply_to else None,
            "user": str(msg.user_id) if msg.user_id else (msg.user_name or "tg_unknown"),
            "text": msg.text,
            "subtype": None,
        },
        history_before=list(history_before or []),
    )


def _adaptive_context_for(
    reader,
    *,
    chat_id: int,
    before_message_id: int,
    max_chars: int = 10_000,
) -> list[dict]:
    """FR-CR-05-09 — fetch the chat-local adaptive context window
    via the reader and return it shaped for `ContextWindow.history_
    before` (Slack-shape dicts the intent pipeline already consumes).

    Wrapped in a try/except so a missing / unconfigured reader (the
    listener doesn't always have one), or a transient Postgres
    issue, drops back to «no context» rather than aborting the
    capture.
    """
    if reader is None:
        return []
    try:
        prior = reader.recent_in_chat(
            chat_id=int(chat_id),
            before_message_id=int(before_message_id),
            max_chars=max_chars,
        )
    except Exception as e:  # noqa: BLE001
        log.info("telegram_adaptive_context_failed", error=str(e))
        return []
    out: list[dict] = []
    for m in prior:
        out.append(
            {
                "ts": str(m.message_id),
                "thread_ts": str(m.reply_to) if m.reply_to else None,
                "user": (
                    str(m.user_id) if m.user_id else (m.user_name or "tg_unknown")
                ),
                "text": m.text or "",
                "subtype": None,
            }
        )
    return out


def _admin_fallback_owner_id() -> str | None:
    """FR-CR-05-09 — when the classifier can't resolve a real owner,
    route the task to the first admin from
    ``TELEGRAM_ADMIN_USER_IDS`` instead of leaving it unassigned (or
    worse, attached to a bot account / a bare hint like «Валя»).
    Returns None when no admins are configured."""
    try:
        from app.telegram_bot.handlers import admin_user_ids

        admins = sorted(admin_user_ids())
    except Exception as e:  # noqa: BLE001
        log.info("telegram_admin_fallback_unavailable", error=str(e))
        return None
    return admins[0] if admins else None


def _admin_display_for(
    admin_uid: str, known_employees: list[dict]
) -> str | None:
    """Look up the admin's friendly label from the registry so the
    card shows `@andre_andreevich` rather than the raw numeric id."""
    for e in known_employees or []:
        if e.get("slack_user_id") == admin_uid:
            return e.get("display_name") or admin_uid
    return None


def _resolve_owner(
    td,
    *,
    known_employees: list[dict],
    sender_user_id: str | None,
    sender_user_name: str | None,
    admin_uid: str | None,
) -> None:
    """FR-CR-05-10 — single owner-resolution pipeline applied to
    each TaskDraft after the LLM stages have run.

    The chain — first match wins:

      1. LLM picked a `owner_user_id` AND it resolves to a row in
         `known_employees` (the team registry + per-chat members).
         Keep as-is; ensure `owner_display_name` is set from the
         registry row when missing.
      2. LLM gave only a `owner_display_name` that isn't in the
         registry (the «CEO Rosecliff» case — outsider mentioned
         in chat). Drop it entirely and fall through to admin.
      3. Sender, but only when the registry is populated AND they
         appear in it (FR-CR-05-09 author-fallback rule).
      4. Admin uid from `TELEGRAM_ADMIN_USER_IDS`. ALWAYS clobber
         `owner_display_name` to the admin's label — otherwise a
         stale «CEO Rosecliff» from step 2 would render on the
         card next to the admin's id.

    Mutates `td` in place. Pass-through when nothing in the chain
    matches (rare — only when no admins are configured AND the
    LLM returned nothing AND the registry is empty)."""
    valid_ids = {e.get("slack_user_id") for e in (known_employees or [])}

    # Step 1 — keep an LLM-picked id only when it resolves.
    if td.owner_user_id:
        if known_employees and td.owner_user_id not in valid_ids:
            # LLM hallucinated an id — drop it and re-run the chain.
            td.owner_user_id = None
            td.owner_display_name = None
        else:
            # FR-CR-05-21 — when the id matches a registry row, the
            # registry's display ALWAYS wins over the LLM-extracted
            # display. Otherwise the same teammate landed as «Артем»
            # on one card and «Артем Соколов» on another, depending
            # on what fragment the source message used. The Sheet
            # is the operator's source of truth.
            if known_employees:
                for e in known_employees:
                    if e.get("slack_user_id") == td.owner_user_id:
                        canonical = (
                            e.get("display_name") or e.get("real_name")
                        )
                        if canonical and canonical != td.owner_user_id:
                            td.owner_display_name = canonical
                        elif not td.owner_display_name:
                            td.owner_display_name = (
                                e.get("display_name")
                                or e.get("real_name")
                                or td.owner_user_id
                            )
                        break
            return

    # Step 2 — LLM returned only a display_name (no id) and it
    # doesn't match any known team member. Drop it.
    if td.owner_display_name and known_employees:
        # `owner_display_name` may include `@`-prefix; normalise.
        needle = td.owner_display_name.strip().lstrip("@").lower()
        matched = False
        for e in known_employees:
            disp = (e.get("display_name") or "").strip().lstrip("@").lower()
            real = (e.get("real_name") or "").strip().lower()
            if needle and (needle == disp or needle == real):
                td.owner_user_id = e.get("slack_user_id")
                td.owner_display_name = e.get("display_name") or td.owner_display_name
                matched = True
                break
        if not matched:
            # «CEO Rosecliff» — outsider; drop the hint entirely.
            td.owner_display_name = None

    if td.owner_user_id:
        return

    # Step 3 — sender fallback (only when allowed by FR-CR-05-09 rule).
    if _author_fallback_allowed(
        sender_user_id, known_employees=known_employees
    ):
        td.owner_user_id = sender_user_id
        if not td.owner_display_name and sender_user_name:
            td.owner_display_name = sender_user_name
        return

    # Step 4 — admin fallback. Always clobber display_name so a
    # stray hint from the LLM doesn't end up rendered next to the
    # admin's id.
    if admin_uid:
        td.owner_user_id = admin_uid
        td.owner_display_name = (
            _admin_display_for(admin_uid, known_employees) or admin_uid
        )


def _resolve_uids_in_text(session: "Session", text: str) -> str:
    """FR-CR-05-94 — operator regression: «По переписке с
    6660151534» where the LLM left a raw numeric Telegram uid
    in the description because it appeared verbatim in the
    source/context.

    Find every standalone 9-15 digit token and try to resolve
    via `team_members.telegram_user_id → real_name`. Replace
    the digits with the name when there's a hit. Leave
    untouched when nothing matches (could be an order number,
    contract id, etc.).

    Bounded by the same digit-length window used elsewhere in
    the project so we don't flag short numbers («5», «23») as
    user ids.
    """
    import re
    from app.models import TeamMember

    # 9-15 digit tokens preceded/followed by non-digit (or
    # start/end). Telegram uids are typically 9-12 digits.
    pattern = re.compile(r"(?<!\d)(\d{9,15})(?!\d)")
    matches = list(pattern.finditer(text))
    if not matches:
        return text

    # Batch lookup so we hit the DB once per unique candidate.
    uids = {int(m.group(1)) for m in matches}
    rows = (
        session.query(TeamMember)
        .filter(TeamMember.telegram_user_id.in_(uids))
        .all()
    )
    by_id: dict[int, str] = {}
    for r in rows:
        if r.telegram_user_id and r.real_name:
            by_id[int(r.telegram_user_id)] = r.real_name

    if not by_id:
        return text

    def _replace(m: "re.Match[str]") -> str:
        uid = int(m.group(1))
        return by_id.get(uid, m.group(1))

    return pattern.sub(_replace, text)


def _fallback_description(message: TelegramSourceMessage) -> str:
    """FR-CR-05-10 — deterministic stand-in description for drafts
    where the LLM had no meaningful context to summarise. Better
    than an empty `📝` field — gives the operator chat name + date
    so they can find the original conversation manually."""
    chat_label = message.chat_title or f"chat {message.chat_id}"
    when = (
        message.sent_at.strftime("%Y-%m-%d %H:%M")
        if message.sent_at
        else "—"
    )
    return f"обсуждалось в {chat_label} · {when}"


def _author_fallback_allowed(
    user_id: str | None, *, known_employees: list[dict]
) -> bool:
    """Decide whether to keep the legacy FR-CR-04-30 «author becomes
    owner when LLM extracted nothing» fallback for the given sender.

    Two-axis rule:

    - If ``known_employees`` is empty (the chat-members registry
      hasn't observed any traffic in this chat yet, or the table
      doesn't exist in the test fixture) → ALLOW the fallback.
      We have no signal that says the sender is a bot / forwarded
      post, so the conservative legacy behaviour wins.
    - If ``known_employees`` IS populated for this chat → require
      the sender to actually appear in it. A non-member sender is
      typically a bot account or a forwarded post from outside the
      chat, and auto-assigning to them produces the «CEO_office1
      bot owns this task» bug.

    Returns True when the author fallback may run, False to defer
    to the admin fallback."""
    if not user_id:
        return False
    if not known_employees:
        return True
    return any(
        e.get("slack_user_id") == str(user_id) for e in known_employees
    )


def _telegram_permalink(msg: TelegramSourceMessage) -> str | None:
    """Best-effort shareable link to the original Telegram message.

    Two paths in priority order:

      1. **View-supplied permalink (FR-CR-05-25).** Some
         ingestion pipelines pre-compute the
         ``t.me/c/<chat>/<msg>`` URL into a `message_link`
         column. Use it as-is — the view's URL is what Telegram
         itself produced and is correct for any chat shape.
      2. **Reconstruction.** Bot API supergroup ids land here in
         two forms:
           - `-1002061886148` (`-100` prefix) → strip it.
           - `-2061886148` (stripped form, some pipelines drop
             the prefix) → use abs value as-is.
         Private chats (positive chat_id) and basic groups
         (≤ 8-digit negative id) have no shareable URL form.
    """
    if getattr(msg, "permalink", None):
        return msg.permalink
    if msg.chat_id >= 0:
        return None
    public = abs(msg.chat_id)
    if public > 1_000_000_000_000:
        # Bot API form `-100xxxxxxxxxx` — strip the prefix.
        public -= 1_000_000_000_000
    elif public < 100_000_000:
        # Basic group (≤ 8-digit id) — no shareable URL.
        return None
    # Else: supergroup id stored without the `-100` prefix
    # (Supabase view convention) — use the abs value as-is.
    return f"https://t.me/c/{public}/{msg.message_id}"


def _known_members_for(session: Session, *, chat_id: int) -> list[dict[str, str]]:
    """Build the LLM owner-stage candidate list as the UNION of:

      - FR-CR-05-10 cross-channel team registry (`team_members`) —
        the authoritative directory the operator maintains via the
        `Team` Google Sheet. Active rows only.
      - FR-CR-05-07 per-chat members observed in this chat
        (`telegram_chat_members`) — hint-only fallback so we don't
        regress to «known nobody» on a fresh deploy where the team
        sheet hasn't been seeded.

    De-duped by `slack_user_id` (the opaque id field that carries
    either a numeric TG user_id or a Slack uid) — when the same
    person appears in both sources, the team-registry row wins
    because it carries the operator's curated `display_name` /
    `real_name`.

    Wrapped in a try/except so a missing migration (test fixture
    without the new tables) drops back to «no known employees»
    rather than aborting the ingest."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        from app.services.team_members import as_known_employees

        for row in as_known_employees(session):
            sid = row.get("slack_user_id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(row)
    except Exception as e:  # noqa: BLE001
        log.info("telegram_team_registry_unavailable", error=str(e))
    try:
        from app.services.telegram_members import members_as_known_employees

        for row in members_as_known_employees(session, chat_id=chat_id):
            sid = row.get("slack_user_id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(row)
    except Exception as e:  # noqa: BLE001
        log.info("telegram_known_members_unavailable", error=str(e))
    return out


class TelegramIngestService:
    """Pulls a batch of Telegram messages through the intent pipeline
    and writes confirmed tasks into the local DB.

    The service is driven externally — by a one-shot CLI
    (``ops/telegram_ingest.py``) or the history migration script.
    Each message either yields a Task (status = create_task) or is
    recorded as "no_action" so the next ingest pass skips it.
    """

    def __init__(
        self,
        *,
        classifier: IntentClassifier,
        orchestrator: Orchestrator,
        reader=None,
    ) -> None:
        self._classifier = classifier
        self._orchestrator = orchestrator
        # FR-CR-05-09 — optional read-only Telegram view reader used
        # to pull the adaptive context window (~10k chars of recent
        # chat history) before the LLM classify call. When None we
        # fall back to «no chat history» — same behaviour as before.
        self._reader = reader

    def process_one(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ) -> Task | None:
        """Back-compat wrapper around :meth:`process_all`. Returns the
        FIRST created Task or ``None`` when nothing was created. Most
        callers should switch to :meth:`process_all` to support
        multi-task messages (FR-CR-05-05)."""
        tasks = self.process_all(session, message)
        return tasks[0] if tasks else None

    def process_all(
        self,
        session: Session,
        message: TelegramSourceMessage,
        *,
        classification=None,
    ) -> list[Task]:
        """FR-CR-05-05: process a Telegram message and return EVERY
        Task it produced. A single message can carry multiple tasks
        («сделать презу к завтра и отчёт к пятнице» → 2 tasks). Each
        ``classification.tasks`` entry becomes its own Task row;
        they share the same ``processed_telegram_messages`` bookmark
        (pointed at the first Task — back-compat with single-task
        callers and the FR-CR-04-26 schema).

        ``classification`` is an optional pre-computed
        :class:`IntentClassification`. When provided, skip the
        internal classify call. See :meth:`prepare_drafts` for the
        same pattern.
        """
        existing = session.get(
            ProcessedTelegramMessage, (message.chat_id, message.message_id)
        )
        if existing is not None:
            return []
        if not message.is_textual:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        known_employees = _known_members_for(session, chat_id=message.chat_id)
        history_before: list[dict] = []
        if classification is None:
            history_before = _adaptive_context_for(
                self._reader,
                chat_id=message.chat_id,
                before_message_id=message.message_id,
            )
            window = _build_window(message, history_before=history_before)
            classification = self._classifier.classify(
                context=window,
                invocation_type=InvocationType.passive,
                known_employees=known_employees,
            )
        else:
            window = _build_window(message)

        if classification.intent != IntentType.create_task or not classification.tasks:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        admin_uid = _admin_fallback_owner_id()
        fallback_desc = _fallback_description(message)
        for td in classification.tasks:
            _resolve_owner(
                td,
                known_employees=known_employees,
                sender_user_id=str(message.user_id) if message.user_id else None,
                sender_user_name=message.user_name,
                admin_uid=admin_uid,
            )
            # FR-CR-05-10 — when the LLM produced no usable
            # description, fill in the deterministic «обсуждалось в
            # <chat> · <date>» so the operator at least sees where
            # the draft came from.
            if not (td.description or "").strip():
                td.description = fallback_desc

        snapshot = self._orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )
        author_id = window.source_message["user"]
        author_str = str(author_id) if author_id else None
        source = {
            "kind": TaskSourceKind.telegram.value,
            "conversation_id": str(message.chat_id),
            "message_ts": str(message.message_id),
            "thread_ts": str(message.reply_to) if message.reply_to else None,
            "permalink": _telegram_permalink(message),
        }

        out: list[Task] = []
        seen_titles_pa: set[str] = set()
        for td in classification.tasks:
            # Intra-message dedup — drop exact-title repeats inside
            # one message before paying for the cross-DB LLM check.
            t_lower = (td.title or "").strip().lower()
            if t_lower and t_lower in seen_titles_pa:
                log.info(
                    "telegram_ingest_skipped_intra_message_duplicate",
                    title=td.title,
                )
                continue
            seen_titles_pa.add(t_lower)

            # Cross-DB dedup against the last open tasks. Skip the
            # candidate silently when the LLM says it duplicates one —
            # the source-message bookmark below ensures we don't
            # re-classify it on the next pass.
            from app.services.task_dedup import check_duplicate

            dup = check_duplicate(
                session,
                candidate=td.model_dump(mode="json"),
                llm_backend=getattr(self._classifier, "backend", None),
            )
            if dup.is_duplicate:
                log.info(
                    "telegram_ingest_skipped_duplicate",
                    title=td.title,
                    duplicate_of=dup.duplicate_of_task_id,
                    reason=dup.reason,
                )
                continue

            # Each per-chunk inference + draft is its own row. The
            # IntentInference table doesn't carry the task draft body
            # so we just persist N copies — cheap, and keeps the
            # one-inference-per-Task invariant.
            single = type(classification)(
                intent=classification.intent,
                confidence=classification.confidence,
                reasoning=classification.reasoning,
                task=td,
            )
            inference = self._orchestrator.persist_inference(
                session,
                context_snapshot=snapshot,
                classification=single,
                invocation_type=InvocationType.passive,
            )
            draft = self._orchestrator.create_draft(
                session,
                inference=inference,
                classification=single,
                created_by_slack_user_id=author_str,
                slack_message_ts=str(message.message_id),
            )
            task = create_task_from_draft(
                session,
                draft=draft,
                source=source,
                context_snapshot_id=snapshot.id,
                fallback_author_slack_id=author_str,
            )
            out.append(task)

        session.add(
            ProcessedTelegramMessage(
                chat_id=message.chat_id,
                message_id=message.message_id,
                processed_at=datetime.now(timezone.utc),
                task_id=out[0].id if out else None,
            )
        )
        return out

    def prepare_draft(
        self,
        session: Session,
        message: TelegramSourceMessage,
    ):
        """Confirm-first variant of `process_one` (FR-CR-04-32).

        Same up-front classification as `process_one`, but stops at the
        ActionDraft and skips Task creation. Used when a task-shaped
        message arrives in a group / supergroup / channel — we DM a
        confirmation widget to the author + admins and only finalise
        into a Task on Accept.

        Returns the persisted ``ActionDraft`` (state = ``proposed``)
        or ``None`` when the message was already processed, has no
        usable text, or didn't classify as a task.

        Idempotent: a second call with the same (chat_id, message_id)
        returns ``None`` (the bookmark in
        ``processed_telegram_messages`` short-circuits us).
        """
        drafts = self.prepare_drafts(session, message)
        return drafts[0] if drafts else None

    def prepare_drafts(
        self,
        session: Session,
        message: TelegramSourceMessage,
        *,
        classification=None,
    ) -> list:
        """FR-CR-05-05 + FR-CR-04-32: confirm-first variant of
        :meth:`process_all`. One ``ActionDraft`` per detected task,
        each carrying its own ``_pending`` block so the Accept
        handler can finalise it independently. The listener posts
        one widget per draft.

        ``classification`` is an optional pre-computed
        :class:`IntentClassification`. When provided, skip the
        internal classify call and reuse it — avoids paying for a
        second classify pass in the migration's ``--debug`` mode and
        keeps the verdict deterministic across the «log» and the
        «persist» half of the same call.
        """
        existing = session.get(
            ProcessedTelegramMessage, (message.chat_id, message.message_id)
        )
        if existing is not None:
            return []
        if not message.is_textual:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        known_employees = _known_members_for(session, chat_id=message.chat_id)
        if classification is None:
            history_before = _adaptive_context_for(
                self._reader,
                chat_id=message.chat_id,
                before_message_id=message.message_id,
            )
            window = _build_window(message, history_before=history_before)
            classification = self._classifier.classify(
                context=window,
                invocation_type=InvocationType.passive,
                known_employees=known_employees,
            )
        else:
            window = _build_window(message)

        if classification.intent != IntentType.create_task or not classification.tasks:
            session.add(
                ProcessedTelegramMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    processed_at=datetime.now(timezone.utc),
                    task_id=None,
                )
            )
            return []

        admin_uid = _admin_fallback_owner_id()
        fallback_desc = _fallback_description(message)
        for td in classification.tasks:
            _resolve_owner(
                td,
                known_employees=known_employees,
                sender_user_id=str(message.user_id) if message.user_id else None,
                sender_user_name=message.user_name,
                admin_uid=admin_uid,
            )
            if not (td.description or "").strip():
                td.description = fallback_desc

        snapshot = self._orchestrator.persist_context_snapshot(
            session, window.to_snapshot_dict()
        )
        author_id = window.source_message["user"]
        author_str = str(author_id) if author_id else None

        out: list = []
        log.info(
            "telegram_prepare_drafts_loop_start",
            message_id=message.message_id,
            task_count=len(classification.tasks),
        )
        # Intra-message dedup: a multi-task LLM split occasionally
        # emits the same title twice for one source message
        # («Блерб для отправки Abundance» × 2). Filter exact-title
        # duplicates here so the operator's DM doesn't get two
        # identical widgets for the same input.
        seen_titles: set[str] = set()
        for td in classification.tasks:
            t_lower = (td.title or "").strip().lower()
            if t_lower and t_lower in seen_titles:
                log.info(
                    "telegram_prepare_drafts_skipped_intra_message_duplicate",
                    title=td.title,
                )
                continue
            seen_titles.add(t_lower)

            # Same dedup gate as `process_all`: skip the draft +
            # widget when the LLM thinks the candidate duplicates an
            # already-existing open Task. Source-message bookmark
            # below still gets written so we don't re-classify.
            from app.services.task_dedup import check_duplicate

            try:
                dup = check_duplicate(
                    session,
                    candidate=td.model_dump(mode="json"),
                    llm_backend=getattr(self._classifier, "backend", None),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "telegram_prepare_drafts_dedup_check_failed",
                    title=td.title[:80],
                    error=str(e),
                )
                dup = type("Tmp", (), {"is_duplicate": False, "duplicate_of_task_id": None, "reason": None})()
            if dup.is_duplicate:
                log.info(
                    "telegram_prepare_drafts_skipped_duplicate",
                    title=td.title,
                    duplicate_of=dup.duplicate_of_task_id,
                    reason=dup.reason,
                )
                continue

            try:
                single = type(classification)(
                    intent=classification.intent,
                    confidence=classification.confidence,
                    reasoning=classification.reasoning,
                    task=td,
                )
                inference = self._orchestrator.persist_inference(
                    session,
                    context_snapshot=snapshot,
                    classification=single,
                    invocation_type=InvocationType.passive,
                )
                draft = self._orchestrator.create_draft(
                    session,
                    inference=inference,
                    classification=single,
                    created_by_slack_user_id=author_str,
                    slack_message_ts=str(message.message_id),
                )
                payload = dict(draft.payload or {})
                # FR-CR-05-75 / 63 / 74 / 94 — apply the SAME
                # cosmetic + default fixes the persist layer
                # applies, so the draft widget the operator sees
                # already matches what the post-Accept Task
                # will look like (FR-CR-05-72/89 title cap +
                # capitalized first letter + default deadline
                # today 18:00 if LLM didn't extract).
                from app.persistence.tasks import normalize_task_title

                raw_title = (payload.get("title") or "").strip()
                if raw_title:
                    try:
                        payload["title"] = normalize_task_title(raw_title)
                    except ValueError:
                        # `normalize_task_title` raises on empty
                        # title; we already early-returned above
                        # via the strip + truthy check, but stay
                        # defensive.
                        pass
                if not (payload.get("due_date") or "").strip():
                    payload["due_date"] = date.today().isoformat()
                if not (payload.get("due_time") or "").strip():
                    payload["due_time"] = "18:00"
                # FR-CR-05-94 — operator regression: «По
                # переписке с 6660151534» — LLM left the raw
                # numeric Telegram uid in the description.
                # Resolve to the human-readable name from
                # team_members so the operator never sees a
                # bare uid.
                desc = payload.get("description")
                if isinstance(desc, str) and desc:
                    payload["description"] = _resolve_uids_in_text(
                        session, desc
                    )
                payload["_pending"] = {
                    "source_kind": "telegram",
                    "conversation_id": str(message.chat_id),
                    "message_ts": str(message.message_id),
                    "thread_ts": str(message.reply_to) if message.reply_to else None,
                    "permalink": _telegram_permalink(message),
                    "fallback_author": author_str,
                    "context_snapshot_id": snapshot.id,
                    "source_chat_id": message.chat_id,
                    "source_message_id": message.message_id,
                    # FR-CR-05-09 — keep the raw source text on the
                    # draft so `post_draft_confirmation` can fall
                    # back to an inline quote when forwardMessage
                    # fails (which it does for every historical-
                    # migration draft — the bot never observed those
                    # messages, so Telegram refuses to forward them).
                    "source_text": (message.text or "")[:10_000],
                }
                draft.payload = payload
                out.append(draft)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "telegram_prepare_drafts_persist_failed",
                    title=td.title[:80],
                    error=str(e),
                    error_type=type(e).__name__,
                )
                # Don't re-raise — keep processing the remaining
                # tasks in the message.

        log.info(
            "telegram_prepare_drafts_loop_done",
            message_id=message.message_id,
            tasks_in=len(classification.tasks),
            drafts_out=len(out),
        )
        session.flush()
        session.add(
            ProcessedTelegramMessage(
                chat_id=message.chat_id,
                message_id=message.message_id,
                processed_at=datetime.now(timezone.utc),
                task_id=None,
            )
        )
        return out

    def process_batch(
        self,
        session: Session,
        messages: list[TelegramSourceMessage],
    ) -> IngestReport:
        """Process a slice of messages, returning a counters report.

        Errors per-message are caught so one bad row doesn't abort
        the whole batch — we record the chat/msg id in the report
        and move on. The caller decides whether to commit or roll
        back the transaction.
        """
        report = IngestReport()
        for m in messages:
            report.seen += 1
            try:
                # Idempotency check goes BEFORE the is_textual branch
                # so a re-run of the same batch (e.g. after a partial
                # backfill) doesn't trip on
                # `processed_telegram_messages_pkey` for messages
                # whose bookmark was already written by a prior run.
                existing = session.get(
                    ProcessedTelegramMessage, (m.chat_id, m.message_id)
                )
                if existing is not None:
                    report.skipped_already_processed += 1
                    continue
                if not m.is_textual:
                    report.skipped_empty_text += 1
                    session.add(
                        ProcessedTelegramMessage(
                            chat_id=m.chat_id,
                            message_id=m.message_id,
                            processed_at=datetime.now(timezone.utc),
                            task_id=None,
                        )
                    )
                    continue
                task = self.process_one(session, m)
                if task is None:
                    report.no_action += 1
                else:
                    report.tasks_created += 1
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                if len(report.error_samples) < 5:
                    report.error_samples.append(
                        f"chat={m.chat_id} msg={m.message_id}: {e!s}"
                    )
                log.warning(
                    "telegram_ingest_message_failed",
                    chat_id=m.chat_id,
                    message_id=m.message_id,
                    error=str(e),
                )
                # Try to record this as processed so we don't loop on it forever.
                # If even this fails (e.g. transaction is broken), the
                # surrounding session_scope rollback will still leave the
                # row unprocessed — that's acceptable for an MVP.
                try:
                    session.add(
                        ProcessedTelegramMessage(
                            chat_id=m.chat_id,
                            message_id=m.message_id,
                            processed_at=datetime.now(timezone.utc),
                            task_id=None,
                        )
                    )
                    session.flush()
                except Exception:
                    session.rollback()
        return report
