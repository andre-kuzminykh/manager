# SPEC v0.1 — Entity-form consistency across a meeting (FR-CR-05-242)

> **Дата:** 2026-06-03
> **Проблема:** одна сущность в разных формах в одном посте («Siva» в саммари vs
> «Ceva Logistics» в задаче) — встреча «Алина, Ирина».

## 1. Требование

В пределах одной встречи **каждая сущность имеет РОВНО одну каноническую форму**
во всех поверхностях: детальном саммари, коротком саммари и задачах.

## 2. Источник правды — детальное саммари

```
транскрипт
  │
  ▼
detailed_summary  ──►  canonicalize_summary_text  ──►  карта замен {found: canonical}
  │  (канонизировано ОДИН раз: people-roster-guard + counterparty vector+критик)
  │
  ├──►  short_summary = суммаризация detailed   (FR-CR-05-241: НЕ канонизируется
  │                                              повторно — наследует формы detailed)
  │
  └──►  tasks: после своей канонизации к ним ПРИНУДИТЕЛЬНО применяется
            та же карта замен detailed (canonicalize_text, longest-first,
            cascade-safe)  → задачи в тех же формах, что detailed
```

**Ключевое:** канонизация сущностей — **единый источник** (detailed). Короткое и
задачи к нему приводятся, а не канонизируются независимо.

## 3. Реализация

| Шаг | Что |
|---|---|
| `_step_detailed_summary` | канонизирует detailed → сохраняет карту `applied` на `row.__dict__["_zm_detail_canon_map"]` (zoom) / `_ff_detail_canon_map` (ff). |
| `_step_short_summary` | строит короткое ИЗ `row.detailed_summary` (уже канонического), **без** повторной канонизации (FR-CR-05-241). |
| `_step_canonicalize_task_names` | после LLM-канонизации применяет карту detailed к `title`/`description` через `canonicalize_text` → форсит формы detailed. Лог: `..._task_canonical_rewrite_applied forced_from_detailed=N`. |

People-резолвер (`resolve_people_to_team_members`) — с **roster-guard** (FR-CR-05-191 v4):
сотрудник подставляется только если он участник встречи (внешний «Андре» ≠ тиммейт
«Андрей Кузьминых»).

## 4. Гарантии / границы

- **Гарантируется:** любой entity, который detailed канонизировал (`found→canonical`),
  будет в задачах в форме `canonical`.
- **Не покрывается:** формы, которые detailed оставил «как услышано» (raw) и которые
  не попали в карту замен — они остаются raw и в задачах (но одинаково raw, т.к.
  задачи приводятся к detailed). Полное покрытие сырых форм — через кириллические
  алиасы каталога (FR-CR-05-238/241), не через этот механизм.

## 5. Тесты

`tests/requirements/test_task_summary_consistency.py` — карта detailed применяется к
тексту задач (longest-first, cascade-safe, identity-skip); `test_people_roster_guard.py`
— сотрудник не подставляется, если не участник.

## 6. Открытые вопросы

- Идеал: задачи **извлекать из канонического detailed**, а не из транскрипта — тогда
  консистентность 100% by construction (сейчас — пост-фактум приведение). v2.
- Схлопнуть `canonicalize_task_names` (LLM против справочника) в применение карты
  detailed целиком — убрать второй LLM-проход. v2.
