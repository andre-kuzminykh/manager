"""FR-CR-05-193c — ID-locked tests для Step 3 (deterministic apply)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock


def test_fr_cr_05_193c_text_replace_word_boundary() -> None:
    """Word-boundary regex replace: 'Дима возьмёт' → 'Дима Дроздов возьмёт',
    но 'Димаху' (substring) НЕ меняется."""
    from app.services.entity_apply import apply_text_replacements
    text = "Дима возьмёт outreach и Димаху это понравится"
    result = apply_text_replacements(
        text, replacements=[{"raw": "Дима", "canonical": "Дима Дроздов"}],
    )
    assert "Дима Дроздов возьмёт" in result
    assert "Димаху" in result  # substring untouched


def test_fr_cr_05_193c_2_no_cascade_when_raw_is_prefix_of_canonical() -> None:
    """FR-CR-05-193c-2 — обнаруженный prod bug: «Артем»→«Артем Соколов»
    НЕ должен превращаться в «Артем Соколов Соколов» если применяется вместе
    с «Артема»→«Артем Соколов»."""
    from app.services.entity_apply import apply_text_replacements
    text = "Артем приехал. Артема ждали все."
    result = apply_text_replacements(
        text,
        replacements=[
            {"raw": "Артем", "canonical": "Артем Соколов"},
            {"raw": "Артема", "canonical": "Артем Соколов"},
        ],
    )
    # ОБА должны стать «Артем Соколов», но НЕ «Артем Соколов Соколов»
    assert "Артем Соколов приехал" in result
    assert "Артем Соколов ждали все" in result
    assert "Соколов Соколов" not in result


def test_fr_cr_05_193c_2_no_cascade_org() -> None:
    """То же для org: «Schaeffler»→«Schaeffler AG» + «Шаффлер»→«Schaeffler»
    не должно превращать «Schaeffler» в «Schaeffler AG AG»."""
    from app.services.entity_apply import apply_text_replacements
    text = "Звонок с Шаффлер и потом Schaeffler"
    result = apply_text_replacements(
        text,
        replacements=[
            {"raw": "Шаффлер", "canonical": "Schaeffler"},
            {"raw": "Schaeffler", "canonical": "Schaeffler AG"},
        ],
    )
    # «Шаффлер» становится «Schaeffler», но НЕ дальше через 2-й replacement
    assert "Schaeffler AG AG" not in result


def test_fr_cr_05_193c_owner_lookup_sets_user_id() -> None:
    """`apply_task_owner(task_data, tm_real_name, session)` находит
    TeamMember и копирует slack_user_id / telegram_user_id."""
    from app.services.entity_apply import apply_task_owner
    mock_session = MagicMock()
    mock_tm = MagicMock(
        id=42, real_name="Дима Дроздов",
        slack_user_id="U081HGB2ANS",
        telegram_user_id=162194995,
        notes="ВСЕ ЧТО СВЯЗАНО С ФОНДАМИ",
    )
    mock_session.query().filter_by().first.return_value = mock_tm
    task_data = {"title": "Outreach to fund X", "raw_owner_mention": "Дима"}
    result = apply_task_owner(
        task_data, tm_real_name="Дима Дроздов", session=mock_session,
    )
    assert result["owner_display_name"] == "Дима Дроздов"
    assert result["owner_user_id"] in ("U081HGB2ANS", "162194995", "162194995")


def test_fr_cr_05_193c_delegate_marker_applied() -> None:
    """Owner=Артем Соколов с notes 'DELEGATE_TASKS_TO: Irina Shipilova' →
    swap owner на Ирину. matcher_meta.status='delegated' + original_owner."""
    from app.services.entity_apply import apply_task_owner, parse_notes_dsl
    mock_session = MagicMock()
    artem = MagicMock(real_name="Артем Соколов",
                       notes="Только стратегические. DELEGATE_TASKS_TO: Irina Shipilova")
    irina = MagicMock(real_name="Irina Shipilova",
                      slack_user_id="U080TG", notes="",
                      telegram_user_id=700469400)
    # First lookup returns Артем, second (delegate) returns Ирина
    def query_side(model):
        m = MagicMock()
        def filter_side(*args, **kwargs):
            mm = MagicMock()
            mm.first.side_effect = [artem, irina]
            return mm
        m.filter_by = filter_side
        m.filter = filter_side
        return m
    mock_session.query = query_side
    task = {"title": "x", "raw_owner_mention": "Артем"}
    result = apply_task_owner(task, tm_real_name="Артем Соколов",
                              session=mock_session)
    # Owner swapped
    assert result.get("owner_display_name") == "Irina Shipilova"
    assert result.get("matcher_meta", {}).get("status") == "delegated"
    assert result.get("matcher_meta", {}).get("original_owner") == "Артем Соколов"


def test_fr_cr_05_193c_do_not_call_skip() -> None:
    """Owner with notes 'DO_NOT_CALL' → owner_display_name=None,
    matcher_meta.status='skipped_do_not_call'."""
    from app.services.entity_apply import apply_task_owner
    mock_session = MagicMock()
    elena = MagicMock(real_name="Радионова Елена",
                      notes="Не вызывай никогда. DO_NOT_CALL.")
    mock_session.query().filter_by().first.return_value = elena
    task = {"title": "x", "raw_owner_mention": "Елена"}
    result = apply_task_owner(task, tm_real_name="Радионова Елена",
                              session=mock_session)
    assert result.get("owner_display_name") is None
    assert result.get("matcher_meta", {}).get("status") == "skipped_do_not_call"
    assert result.get("matcher_meta", {}).get("original_owner") == "Радионова Елена"


def test_fr_cr_05_193c_idempotent_apply() -> None:
    """Повторный apply того же `replacements` к уже-replaced тексту:
    no-op (Дима Дроздов остаётся Дима Дроздов, не Дима Дроздов Дроздов)."""
    from app.services.entity_apply import apply_text_replacements
    text = "Дима Дроздов сделал"  # уже canonical
    repls = [{"raw": "Дима", "canonical": "Дима Дроздов"}]
    once = apply_text_replacements(text, replacements=repls)
    twice = apply_text_replacements(once, replacements=repls)
    assert once == twice
    assert "Дима Дроздов Дроздов" not in twice


def test_fr_cr_05_193c_unmatched_mentions_preserved() -> None:
    """raw 'Бианка' не в replacements → остаётся как есть в тексте."""
    from app.services.entity_apply import apply_text_replacements
    text = "Бианка приедет завтра, Дима подготовит"
    result = apply_text_replacements(
        text, replacements=[{"raw": "Дима", "canonical": "Дима Дроздов"}],
    )
    assert "Бианка" in result
    assert "Дима Дроздов" in result


def test_fr_cr_05_193c_error_safe_fallback() -> None:
    """Exception внутри apply (DB unavailable etc.) → log + возврат raw
    данных. Pipeline не падает."""
    from app.services.entity_apply import apply_task_owner
    mock_session = MagicMock()
    mock_session.query.side_effect = RuntimeError("DB down")
    task = {"title": "x", "raw_owner_mention": "Дима"}
    result = apply_task_owner(task, tm_real_name="Дима Дроздов",
                              session=mock_session)
    # Не raise, owner=None, matcher_meta.status='apply_error'
    assert result.get("owner_display_name") is None
    assert result.get("matcher_meta", {}).get("status") in (
        "apply_error", "no_match"
    )


def test_fr_cr_05_193c_step3_wall_time_under_100ms() -> None:
    """NFR-CR-05-193-3: apply pure Python < 100ms для типичного meeting
    (~50 replacements, ~5KB text)."""
    from app.services.entity_apply import apply_text_replacements
    text = "Дима " * 500  # ~2500 chars
    repls = [
        {"raw": f"X{i}", "canonical": f"Y{i}"} for i in range(50)
    ] + [{"raw": "Дима", "canonical": "Дима Дроздов"}]
    start = time.perf_counter()
    apply_text_replacements(text, replacements=repls)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100, f"Step 3 too slow: {elapsed_ms:.1f}ms"
