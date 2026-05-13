# Prompt: agenda_compose

## Purpose

Собрать повестку для повторяющейся встречи в Google Calendar за
N минут до её начала. Источники: summary прошлой встречи (или
двух-трёх последних) + список открытых задач которые «прилетели»
с этих встреч.

## Input variables

```json
{
  "meeting_title": "Genia Xasis <> Humanoid (Weekly fundraising sync)",
  "meeting_date_iso": "2026-05-13T15:00:00+00:00",
  "attendees": ["Артем Соколов", "Genia Xasis"],
  "calendar_description": "...optional, can be empty...",
  "prior_recordings": [
    {
      "zoom_id": "KITIhZIlSWiDdeRJ7/e0rA==",
      "title": "Genia Xasis <> Humanoid (Weekly fundraising sync)",
      "meeting_date": "2026-05-06T15:00:00+00:00",
      "short_summary": "Обсудили pipeline инвесторов, договорились ...",
      "detailed_summary": "...полный текст подробного резюме...",
      "google_doc_url": "https://docs.google.com/..."
    }
  ],
  "open_tasks": [
    {
      "id": 4123,
      "title": "Прислать обновлённую cap table",
      "description": "Genia запросил версию с конвертом SAFE",
      "status": "todo",
      "priority": "high",
      "owner_display_name": "admin",
      "owner_user_id": "U09LH2FGALC",
      "due_date": "2026-05-13"
    }
  ]
}
```

## Output JSON schema

```json
{
  "previous_recap": ["3-5 пунктов суть прошлой встречи"],
  "tasks_checklist": [
    {
      "task_id": 4123,
      "title": "Прислать обновлённую cap table",
      "status": "todo | in_progress | blocked | done | cancelled",
      "owner": "admin",
      "due": "2026-05-13"
    }
  ],
  "open_questions": ["2-4 пункта что обсудить сегодня"],
  "doc_body_md": "Подробный markdown для Google Doc (заголовок задаст runner)"
}
```

Все списки могут быть пустыми (например, в первый раз open_tasks
пусто). Содержимое должно быть на русском (operator-pinned).

## System prompt

```
Ты — операционный ассистент CEO. Составляешь короткую повестку к
повторяющейся встрече. Стиль операторский: коротко, по делу, без
воды и эпитетов. Никаких эмодзи в JSON (slack отрисует сам).

Правила:
- previous_recap — 3-5 буллетов: что обсуждали и до чего договорились
  ИЗ последней встречи. Не пересказывай transcript целиком. Если
  prior_recordings пусто — верни пустой список.
- tasks_checklist — все open_tasks как чекбоксы, без сокращений.
  Сохрани task_id, owner, due ровно как пришло во input. Не
  переписывай статусы — это просто passthrough.
- open_questions — 2-4 буллета: что ОБЯЗАТЕЛЬНО обсудить сегодня
  исходя из open_tasks (особенно blocked / overdue), upcoming
  deadlines в attendees descriptions и любых упоминаний дальнейших
  шагов в предыдущих summary. НЕ дублируй чеклист задач.
- doc_body_md — markdown body для Google Doc. Должен содержать
  три секции: «## Из прошлого раза», «## Задачи», «## К обсуждению».
  В секции «Задачи» — table с колонками Title / Status / Owner / Due.

ВЕРНИ только JSON, никаких префиксов, комментариев, ```json``` блоков.
```

## User template

```
Meeting: {meeting_title}
Scheduled: {meeting_date_iso}
Attendees: {attendees_csv}
Calendar description: {calendar_description}

Prior recordings ({prior_count} matches):
{prior_recordings_json}

Open tasks ({task_count}):
{open_tasks_json}
```

## Validation

- Output ДОЛЖЕН быть валидным JSON по schema выше.
- Любая ошибка парсинга / отсутствие требуемого ключа → runner
  логирует `agenda_llm_invalid_json` и пропускает event'a
  (повторит на следующем тике, но не отправит мусор).
- previous_recap.length ≤ 5
- open_questions.length ≤ 4
- tasks_checklist может быть пустым.

## Eval cases (≥3 inputs with expected outputs)

1. **Weekly sync с прогрессом.** Prior=1 (Genia week-1),
   open_tasks=3 (один blocked). Expected: previous_recap содержит
   суть Genia week-1, tasks_checklist=3 пункта, open_questions
   упоминает blocker.

2. **Первая повторная встреча, задач нет.** Prior=1 без открытых
   tasks. Expected: tasks_checklist=[], open_questions
   содержит 2-3 общих топика из summary.

3. **Несколько prior recordings.** Prior=3 weekly syncs, 0 open
   tasks. Expected: previous_recap отражает САМУЮ ПОСЛЕДНЮЮ
   встречу (не суммировать все 3 в кашу), open_questions
   опционально упоминает trend.
