"""FR-CR-05-192ac — ID-locked tests for resource attendees filter.

Operator-pinned 2026-05-22:
  «надо не писать "- London, UK-5-Artem's Office (1)" и подобный офис»

Google Calendar Humanoid attaches meeting room "resources" (Humanoid
Office - London, UK-5-Artem's Office (1)) as attendees. Они не люди.
Filter drops these BEFORE label resolution, гарантируя только реальные
люди попадают в `candidate.attendees` и downstream «Участники:» line.

Three detection signals (ANY hit → dropped):
  1) item['resource'] is True
  2) email ends with .resource.calendar.google.com
  3) displayName/label contains: "humanoid office", "artem's office",
     " office - ", "meeting room", "переговорная"
"""
from __future__ import annotations


def _is_resource_attendee(item):
    """Port of `app.agenda.service._is_resource_attendee` — source
    of truth here for the regression gate."""
    if isinstance(item, str):
        s = item.strip().lower()
        if not s:
            return False
        if s.endswith(".resource.calendar.google.com"):
            return True
        if any(m in s for m in (
            "humanoid office", "artem's office", " office - ",
            "meeting room", "переговорная",
        )):
            return True
        return False
    if not isinstance(item, dict):
        return False
    if item.get("resource") is True:
        return True
    email = (item.get("email") or "").strip().lower()
    if email.endswith(".resource.calendar.google.com"):
        return True
    label = (
        (item.get("displayName") or item.get("name") or "").strip().lower()
    )
    if any(m in label for m in (
        "humanoid office", "artem's office", " office - ",
        "meeting room", "переговорная",
    )):
        return True
    return False


def test_fr_cr_05_192ac_drops_resource_true_field() -> None:
    """Calendar API official `resource: true` — самый прямой сигнал."""
    assert _is_resource_attendee({
        "email": "room1@example.com",
        "displayName": "Conference Room",
        "resource": True,
    }) is True


def test_fr_cr_05_192ac_drops_resource_email_domain() -> None:
    """Google Calendar resource emails оканчиваются на
    `.resource.calendar.google.com`."""
    assert _is_resource_attendee({
        "email": "humanoid.com_3137383933353936383834@resource.calendar.google.com",
        "displayName": "Humanoid Office - London",
    }) is True


def test_fr_cr_05_192ac_drops_humanoid_office_label() -> None:
    """Resource без resource:true и без resource-домена — но displayName
    содержит маркер «Humanoid Office»."""
    assert _is_resource_attendee({
        "email": "anything@thehumanoid.ai",
        "displayName": "Humanoid Office - London",
    }) is True


def test_fr_cr_05_192ac_drops_artem_office_label() -> None:
    """«UK-5-Artem's Office (1)» — нумерованная переговорка."""
    assert _is_resource_attendee({
        "email": "uk5office@thehumanoid.ai",
        "displayName": "UK-5-Artem's Office (1)",
    }) is True


def test_fr_cr_05_192ac_drops_meeting_room_label() -> None:
    """Generic «Meeting Room» / «Переговорная»."""
    assert _is_resource_attendee({
        "displayName": "Meeting Room Alpha",
    }) is True
    assert _is_resource_attendee({
        "displayName": "Переговорная 5 этаж",
    }) is True


def test_fr_cr_05_192ac_keeps_real_person_attendee() -> None:
    """Реальные люди — НЕ drop'аются. Все три сигнала отрицательны."""
    assert _is_resource_attendee({
        "email": "1@thehumanoid.ai",
        "displayName": "Артем Соколов",
        "resource": False,
    }) is False
    assert _is_resource_attendee({
        "email": "alina@thehumanoid.ai",
        "displayName": "Alina Kolpakova",
    }) is False
    assert _is_resource_attendee({
        "email": "oponomarenko@cohengresser.com",
        "displayName": "Ольга Пономаренко",
    }) is False


def test_fr_cr_05_192ac_handles_string_attendee() -> None:
    """Когда attendee приходит просто строкой (legacy / unresolved),
    тот же фильтр применяется к строке."""
    # Строка-resource → drop
    assert _is_resource_attendee("Humanoid Office - London") is True
    assert _is_resource_attendee("UK-5-Artem's Office (1)") is True
    assert _is_resource_attendee(
        "humanoid.com_xxx@resource.calendar.google.com"
    ) is True
    # Строка с реальным именем → keep
    assert _is_resource_attendee("Артем Соколов") is False
    assert _is_resource_attendee("Дмитрий Седов") is False
    # Empty string
    assert _is_resource_attendee("") is False


def test_fr_cr_05_192ac_handles_invalid_input() -> None:
    """None / int / list / другие types → False (no false positive)."""
    assert _is_resource_attendee(None) is False
    assert _is_resource_attendee(42) is False
    assert _is_resource_attendee([]) is False
    assert _is_resource_attendee({}) is False  # пустой dict
