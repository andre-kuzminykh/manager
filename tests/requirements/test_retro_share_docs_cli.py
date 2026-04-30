"""FR-CR-05-90 — `ops/retro_share_docs.py` retro-share CLI.

Operator workflow: «сделай так чтобы отчеты которые
генерируются в google doc были сразу доступны для
редактирования всем у кого есть ссылка». New docs land
shared on creation (FR-CR-05-59 auto-share). This CLI
covers the OLD docs that were created before the auto-share
deploy or where the share call silently 4xx'd.
"""
from __future__ import annotations

import pytest

from app.models import MeetingRecording


def _mk_recording(s, **kw) -> MeetingRecording:
    base = dict(
        fireflies_id="ff-1",
        title="Meeting",
        google_doc_id="doc-abc",
        google_doc_url="https://docs.google.com/document/d/doc-abc/edit",
        audio_downloaded=True,
        transcribed=True,
        detailed_summarised=True,
        doc_exported=True,
        tasks_extracted=True,
    )
    base.update(kw)
    r = MeetingRecording(**base)
    s.add(r)
    s.flush()
    return r


def test_retro_share_dry_run_skips_api_calls(
    patched_session_scope, SessionFactory, monkeypatch
):
    """`--dry-run` lists doc ids without touching Drive."""
    import ops.retro_share_docs as mod

    with SessionFactory() as s:
        _mk_recording(s, fireflies_id="ff-1", google_doc_id="doc-1")
        _mk_recording(s, fireflies_id="ff-2", google_doc_id="doc-2")
        s.commit()

    share_calls: list[dict] = []

    class _StubDocs:
        def _share_anyone_with_link(self, doc_id, *, role):
            share_calls.append({"doc_id": doc_id, "role": role})

    monkeypatch.setattr(
        mod, "build_docs_factory", lambda settings: lambda: _StubDocs()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.retro_share_docs", "--dry-run"])
    rc = mod.main()
    assert rc == 0
    assert share_calls == []


def test_retro_share_invokes_share_with_writer_role_by_default(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Default role is `writer` → anyone-with-link can edit."""
    import ops.retro_share_docs as mod

    with SessionFactory() as s:
        _mk_recording(s, fireflies_id="ff-a", google_doc_id="doc-A")
        _mk_recording(s, fireflies_id="ff-b", google_doc_id="doc-B")
        s.commit()

    share_calls: list[dict] = []

    class _StubDocs:
        def _share_anyone_with_link(self, doc_id, *, role):
            share_calls.append({"doc_id": doc_id, "role": role})

    monkeypatch.setattr(
        mod, "build_docs_factory", lambda settings: lambda: _StubDocs()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.retro_share_docs"])
    rc = mod.main()
    assert rc == 0
    # One call per recording, all with role=writer.
    assert len(share_calls) == 2
    assert {c["doc_id"] for c in share_calls} == {"doc-A", "doc-B"}
    assert all(c["role"] == "writer" for c in share_calls)


def test_retro_share_role_flag_overrides_default(
    patched_session_scope, SessionFactory, monkeypatch
):
    """`--role reader` produces read-only sharing instead of writer."""
    import ops.retro_share_docs as mod

    with SessionFactory() as s:
        _mk_recording(s, fireflies_id="ff-r", google_doc_id="doc-r")
        s.commit()

    share_calls: list[dict] = []

    class _StubDocs:
        def _share_anyone_with_link(self, doc_id, *, role):
            share_calls.append({"doc_id": doc_id, "role": role})

    monkeypatch.setattr(
        mod, "build_docs_factory", lambda settings: lambda: _StubDocs()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(
        mod.sys, "argv", ["ops.retro_share_docs", "--role", "reader"]
    )
    rc = mod.main()
    assert rc == 0
    assert share_calls == [{"doc_id": "doc-r", "role": "reader"}]


def test_retro_share_skips_recordings_without_google_doc_id(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Recordings whose `google_doc_id` is null (export step
    never ran or failed) must not show up in the work list —
    nothing to share for those."""
    import ops.retro_share_docs as mod

    with SessionFactory() as s:
        _mk_recording(s, fireflies_id="ff-with", google_doc_id="doc-here")
        # Recording without a doc → excluded by SQL filter.
        _mk_recording(
            s, fireflies_id="ff-without",
            google_doc_id=None,
            google_doc_url=None,
            doc_exported=False,
        )
        s.commit()

    share_calls: list[dict] = []

    class _StubDocs:
        def _share_anyone_with_link(self, doc_id, *, role):
            share_calls.append({"doc_id": doc_id, "role": role})

    monkeypatch.setattr(
        mod, "build_docs_factory", lambda settings: lambda: _StubDocs()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.retro_share_docs"])
    rc = mod.main()
    assert rc == 0
    assert len(share_calls) == 1
    assert share_calls[0]["doc_id"] == "doc-here"


def test_retro_share_continues_on_per_doc_failure(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Drive 4xx on one doc must not abort the run — others
    keep getting shared. Non-zero exit code surfaces the
    failures so the cron caller can alert."""
    import ops.retro_share_docs as mod

    with SessionFactory() as s:
        _mk_recording(s, fireflies_id="ff-1", google_doc_id="doc-ok")
        _mk_recording(s, fireflies_id="ff-2", google_doc_id="doc-bad")
        _mk_recording(s, fireflies_id="ff-3", google_doc_id="doc-also-ok")
        s.commit()

    share_calls: list[str] = []

    class _StubDocs:
        def _share_anyone_with_link(self, doc_id, *, role):
            share_calls.append(doc_id)
            if doc_id == "doc-bad":
                raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(
        mod, "build_docs_factory", lambda settings: lambda: _StubDocs()
    )
    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.retro_share_docs"])
    rc = mod.main()
    # Non-zero because at least one doc failed.
    assert rc == 1
    # All 3 docs were attempted (the failure didn't abort).
    assert share_calls == ["doc-ok", "doc-bad", "doc-also-ok"]


def test_retro_share_returns_2_when_credentials_unavailable(monkeypatch):
    """Bad config → exit 2 so cron knows to alert separately
    from per-doc failures."""
    import ops.retro_share_docs as mod

    monkeypatch.setattr(
        mod, "build_docs_factory", lambda settings: None
    )
    monkeypatch.setattr(mod.sys, "argv", ["ops.retro_share_docs"])
    rc = mod.main()
    assert rc == 2


def test_fireflies_pipeline_calls_export_summary_with_writer_default():
    """FR-CR-05-59 invariant — pin that the Fireflies
    pipeline does NOT pass `share_role=...` to
    `export_summary`, so the default `writer` kicks in.
    Defending this against an accidental refactor that
    might add `share_role=None` and silently revert all new
    docs to private."""
    from app.fireflies import pipeline as p
    import inspect

    src = inspect.getsource(p.FirefliesPipeline._step_doc_export)
    # The call must NOT carry an explicit `share_role` arg.
    assert "share_role" not in src
    # It must call export_summary with title/body/parent_folder_id.
    assert "export_summary" in src
    assert "parent_folder_id" in src
