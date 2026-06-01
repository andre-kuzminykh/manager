"""FR-CR-05-233 — push ALL tasks to the Sheet, not just strategic ones.

operator 2026-06-01: «ты только стратегические записываешь в таблицу шит
из слака? надо все». The feeder now appends every titled draft/task; the
strategic-only filter is kept behind `sheet_sync_all_directions=False`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.intent import ActionDraft, ActionDraftState, IntentInference, IntentType
from app.models.sheet_sync import GsExportedSource, GsSheetIntegration
from app.sheet_sync.feeder import feed_new_strategic


class _FakeClient:
    def __init__(self):
        self.appended: list[list[str]] = []

    def append_rows(self, rows):
        self.appended.extend(rows)

    def ensure_structure(self, **kwargs):
        pass


def _seed(session):
    integ = GsSheetIntegration(
        spreadsheet_id="ss", sheet_id=0, sheet_title="t", status="active",
    )
    session.add(integ)
    session.flush()
    inf = IntentInference(
        intent=IntentType.create_task, confidence=1.0, invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    now = datetime.now(timezone.utc)
    for direction, title in [("other", "Reschedule the call"),
                             ("investors", "Follow up with XTX")]:
        session.add(ActionDraft(
            inference_id=inf.id, intent=IntentType.create_task,
            state=ActionDraftState.proposed,
            payload={"title": title, "direction": direction},
            created_at=now,
        ))
    session.flush()
    return integ


def test_feeder_appends_all_directions_by_default(session):
    integ = _seed(session)
    client = _FakeClient()
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    n = feed_new_strategic(
        session, client, integration_id=integ.id, since_dt=since, llm=None,
    )
    # default sheet_sync_all_directions=True → BOTH the 'other' and the
    # strategic draft are pushed.
    assert n == 2
    assert len(client.appended) == 2
    # both recorded as exported so they're not re-appended next tick
    assert session.query(GsExportedSource).count() == 2


def test_feeder_strategic_only_when_flag_off(session, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "sheet_sync_all_directions", False, raising=False,
    )
    integ = _seed(session)
    client = _FakeClient()
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    n = feed_new_strategic(
        session, client, integration_id=integ.id, since_dt=since, llm=None,
    )
    # flag off → only the strategic ('investors') draft is pushed.
    assert n == 1
    assert len(client.appended) == 1


def test_feeder_added_at_is_real_message_time(session):
    """FR-CR-05-233 — the 'Added at' column reflects the source Slack
    message time (here 2026-05-29), not the export/creation time (today)."""
    integ = GsSheetIntegration(
        spreadsheet_id="ss", sheet_id=0, sheet_title="t", status="active",
    )
    session.add(integ)
    session.flush()
    inf = IntentInference(
        intent=IntentType.create_task, confidence=1.0, invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    msg_dt = datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc)
    session.add(ActionDraft(
        inference_id=inf.id, intent=IntentType.create_task,
        state=ActionDraftState.proposed,
        payload={"title": "X", "direction": "other",
                 "_pending": {"message_ts": str(msg_dt.timestamp())}},
        created_at=datetime.now(timezone.utc),  # "exported today"
    ))
    session.flush()
    client = _FakeClient()
    feed_new_strategic(
        session, client, integration_id=integ.id,
        since_dt=datetime(2000, 1, 1, tzinfo=timezone.utc), llm=None,
    )
    assert len(client.appended) == 1
    added_at = client.appended[0][-1]  # trailing 'Added at' column
    assert added_at.startswith("2026-05-29"), added_at


def test_export_tasks_wires_real_added_at_helpers():
    """The cron exporter (ops.sheet_sync_export_tasks) must use the shared
    real-message-time helpers, not draft.created_at."""
    import ops.sheet_sync_export_tasks as exp
    from app.sheet_sync import feeder

    assert exp._draft_added_at is feeder._draft_added_at
    assert exp._task_added_at is feeder._task_added_at
    src = (
        __import__("pathlib").Path(exp.__file__).read_text(encoding="utf-8")
    )
    assert "_draft_added_at(d)" in src and "_task_added_at(t)" in src
    # the old created-at formatting for 'added' must be gone
    assert 'added = d.created_at.strftime' not in src
    assert 'added = t.created_at.strftime' not in src
