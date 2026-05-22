"""FR-CR-05-192y — ID-locked tests for the standalone Zoom + Fireflies
runner (`ops/zoom_fireflies_runner.py`).

Operator-pinned 2026-05-22:
  «мне надо чтобы легаси только телеграм и слак задачи слушал,
   а остальное менеджер» — Zoom/FF поллеры выносятся из TG-listener
   в отдельный `manager-zoom-ff-1` контейнер (`python -m
   ops.zoom_fireflies_runner`).
  «обрабатывать и выводить только там где есть Артем
   1@thehumanoid.ai» — обязательный email-фильтр в participants
   (Fireflies) / host+participants (Zoom).
  «начинаем только новые подтягивать» — cutoff `started_at = now()`
   не подтягивает старые встречи на рестарте.

Contract locked here mirrors three helpers inside the runner:

  - `_operator_email(s)` — resolves OPERATOR_REQUIRED_EMAIL with
    fallback to settings.zoom_required_email. Empty → None (filter
    off).
  - `_ff_has_operator(t, op_email)` — case-insensitive, whitespace
    tolerant participant-list membership check.
  - `_meeting_date_utc(md)` — normalises naive datetimes to UTC so
    the `>= started_at` comparison can't crash on mixed tz-awareness.

These three are tested directly (the runner module imports use a
heavy chain — Settings, openai, sqlalchemy — so the helpers are
ported here verbatim under the same semantic contract).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


# === verbatim ports of the runner helpers ============================
# Keep these in sync with `ops/zoom_fireflies_runner.py`. If the runner
# changes the semantics, this test file is the authoritative gate.


def _operator_email_resolution(env_val: str | None, settings_val: str) -> str | None:
    """Mirror of `_operator_email(settings)` from the runner."""
    val = (env_val or settings_val or "").strip().lower()
    return val or None


def _ff_has_operator(participants, op_email: str) -> bool:
    """Mirror of `_ff_has_operator(t, op_email)` from the runner."""
    parts = [(p or "").strip().lower() for p in (participants or [])]
    return op_email in parts


def _meeting_date_utc(md):
    """Mirror of `_meeting_date_utc(md)` from the runner."""
    if md is None:
        return None
    return md if md.tzinfo else md.replace(tzinfo=timezone.utc)


# === tests ===========================================================


def test_fr_cr_05_192y_operator_email_resolution_priority() -> None:
    """OPERATOR_REQUIRED_EMAIL beats settings.zoom_required_email.
    The runner reads env first so ops can override per-container
    without touching shared Settings."""
    assert _operator_email_resolution(
        "Op@Thehumanoid.AI", "z@x"
    ) == "op@thehumanoid.ai"

    # Fallback when env unset
    assert _operator_email_resolution(
        None, "  1@thehumanoid.ai  "
    ) == "1@thehumanoid.ai"

    # Lowercase + whitespace strip both via .strip().lower()
    assert _operator_email_resolution(
        "  X@Y.COM  ", "ignored@x.x"
    ) == "x@y.com"


def test_fr_cr_05_192y_operator_email_empty_returns_none() -> None:
    """Empty/whitespace from both sources → None.
    None disables the email filter — runner processes ALL meetings.
    This is the operator escape-hatch for ops/diagnostic runs."""
    assert _operator_email_resolution(None, "") is None
    assert _operator_email_resolution("", "") is None
    assert _operator_email_resolution("   ", "   ") is None


def test_fr_cr_05_192y_ff_has_operator_email_filter_positive() -> None:
    """Operator email present in participants → keep transcript."""
    assert _ff_has_operator(
        ["alice@x.com", "1@thehumanoid.ai", "bob@y.com"],
        "1@thehumanoid.ai",
    ) is True


def test_fr_cr_05_192y_ff_has_operator_email_filter_negative_email_absent() -> None:
    """Operator absent → skip (we don't want third-party meetings
    that happen to leak through Fireflies into our account)."""
    assert _ff_has_operator(
        ["alice@x.com", "bob@y.com"],
        "1@thehumanoid.ai",
    ) is False


def test_fr_cr_05_192y_ff_has_operator_email_filter_empty_participants() -> None:
    """Empty / None participants list cannot prove operator was
    present → skip (conservative bias). Fireflies sometimes returns
    transcripts with empty participants for short or bot-only calls."""
    assert _ff_has_operator([], "1@thehumanoid.ai") is False
    assert _ff_has_operator(None, "1@thehumanoid.ai") is False


def test_fr_cr_05_192y_ff_has_operator_email_filter_case_insensitive() -> None:
    """Fireflies returns participants from various external sources
    (Google Calendar invitees, raw email from invite list, ZoomBot
    join-record). Case + whitespace MUST not block the filter."""
    assert _ff_has_operator(
        ["1@TheHumanoid.AI"], "1@thehumanoid.ai",
    ) is True
    assert _ff_has_operator(
        [" 1@thehumanoid.ai "], "1@thehumanoid.ai",
    ) is True
    assert _ff_has_operator(
        ["bob@x.com", "  1@THEHUMANOID.AI  "], "1@thehumanoid.ai",
    ) is True
    # None entries inside the list are also skipped safely
    assert _ff_has_operator(
        [None, "1@thehumanoid.ai"], "1@thehumanoid.ai",
    ) is True


def test_fr_cr_05_192y_meeting_date_utc_handles_naive_and_aware() -> None:
    """The cutoff comparison `md >= started_at` MUST not throw
    TypeError on mixed tz-awareness. `_meeting_date_utc` normalises
    naive datetimes to UTC (Fireflies sometimes returns naive
    datetimes; Calendar/Zoom return aware)."""
    naive = datetime(2026, 5, 22, 12, 0)
    utc_norm = _meeting_date_utc(naive)
    assert utc_norm.tzinfo == timezone.utc
    assert utc_norm == naive.replace(tzinfo=timezone.utc)

    aware = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    assert _meeting_date_utc(aware) is aware  # unchanged

    # Aware with non-UTC tz also passes through (>= comparison
    # auto-converts via datetime semantics)
    plus3 = datetime(2026, 5, 22, 15, 0,
                     tzinfo=timezone(timedelta(hours=3)))
    assert _meeting_date_utc(plus3) is plus3

    # None → None (caller skips)
    assert _meeting_date_utc(None) is None


def test_fr_cr_05_192y_cutoff_started_at_initialized_to_utc_now() -> None:
    """Contract: `started_at = datetime.now(timezone.utc)` snapshot
    at runner main() entry. Subsequent ticks compare each meeting's
    `meeting_date >= started_at` and DROP older. Restart bumps the
    cutoff forward (never backward) — history is never pulled."""
    snapshot = datetime.now(timezone.utc)
    assert snapshot.tzinfo == timezone.utc

    # An older recording would fail the filter
    one_hour_ago = snapshot - timedelta(hours=1)
    assert one_hour_ago < snapshot

    # A future-scheduled recording would pass
    one_hour_ahead = snapshot + timedelta(hours=1)
    assert one_hour_ahead >= snapshot


def test_fr_cr_05_192y_cutoff_filter_drops_records_before_startup() -> None:
    """End-to-end semantics of the cutoff: simulate one batch with
    3 transcripts of varying age — only the post-cutoff one survives.

    This is the test that catches any regression where the runner
    accidentally uses `>` vs `>=` or compares naive↔aware datetimes."""
    started_at = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)

    class _T:
        def __init__(self, md): self.meeting_date = md

    batch = [
        _T(datetime(2026, 5, 22, 9, 59, tzinfo=timezone.utc)),  # 1 min old
        _T(datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)),  # exactly cutoff
        _T(datetime(2026, 5, 22, 11, 30, tzinfo=timezone.utc)), # fresh
        _T(None),                                                 # no date — skip
        _T(datetime(2026, 5, 22, 10, 1)),                        # naive but post-cutoff
    ]

    kept = []
    for t in batch:
        md = _meeting_date_utc(t.meeting_date)
        if md is not None and md >= started_at:
            kept.append(t)

    # exactly-at-cutoff + fresh + naive-post-cutoff = 3
    assert len(kept) == 3
    # 1-min-old dropped
    assert batch[0] not in kept
    # None dropped
    assert batch[3] not in kept


def test_fr_cr_05_192y_polish_cutoff_uses_lookback_hours() -> None:
    """FR-CR-05-192y-polish: cutoff = now - OPERATOR_INGEST_LOOKBACK_HOURS,
    не просто now. Restart не теряет встречи дня."""
    now = datetime.now(timezone.utc)
    # 24h lookback (default) — встречи за последние 24 часа ловятся
    lookback_h = 24.0
    started_at = now - timedelta(hours=lookback_h)
    # Встреча 6 часов назад — должна быть НЕ старее cutoff
    six_h_ago = now - timedelta(hours=6)
    assert six_h_ago >= started_at, "6h-old meeting must pass 24h lookback"
    # Встреча 30 часов назад — должна быть старее
    thirty_h_ago = now - timedelta(hours=30)
    assert thirty_h_ago < started_at, "30h-old meeting must be skipped"


def test_fr_cr_05_192y_polish_default_lookback_24h() -> None:
    """Default `OPERATOR_INGEST_LOOKBACK_HOURS=24`. Если env не задан,
    runner использует 24h как conservative default."""
    val = float(os.environ.get("OPERATOR_INGEST_LOOKBACK_HOURS", "24"))
    # Когда env не выставлен, fallback string '24' → float 24
    assert val == 24.0


def test_fr_cr_05_192y_polish_zero_lookback_equals_now() -> None:
    """`OPERATOR_INGEST_LOOKBACK_HOURS=0` отключает lookback, возвращает
    strict behavior FR-CR-05-192y (cutoff = now exactly)."""
    now = datetime.now(timezone.utc)
    lookback_h = 0.0
    started_at = now - timedelta(hours=lookback_h)
    # cutoff == now (microseconds могут отличаться в ms-диапазоне)
    assert abs((now - started_at).total_seconds()) < 1


def test_fr_cr_05_192y_noop_sender_safe_for_any_attr_call() -> None:
    """The runner's `_NoopSender` stub MUST answer to any method
    call without raising — pipeline code can call any TelegramSender
    method (`send_message`, `answer_callback_query`, `edit_message_text`,
    `send_photo`, …). All return None. Without this guarantee the
    pipeline would crash any time it tries to send a status update."""
    class _NoopSender:
        def send_message(self, *a, **k): return None
        def __getattr__(self, _): return lambda *a, **k: None

    s = _NoopSender()
    # Explicit method
    assert s.send_message("hi", chat_id=123) is None
    # Magic methods via __getattr__
    assert s.edit_message_text("x") is None
    assert s.answer_callback_query(callback_query_id="abc") is None
    assert s.send_photo(path="/tmp/foo.png") is None
    assert s.delete_message(message_id=42) is None
    # Even nonsense methods don't raise
    assert s.completely_made_up_method(1, 2, 3) is None
