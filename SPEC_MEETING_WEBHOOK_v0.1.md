# SPEC v0.1 — Meeting Webhook (n8n delivery)

> **Дата:** 2026-06-03
> **FR:** FR-CR-05-160
> **Код:** `app/services/meeting_webhook.py`, call-sites `app/zoom/pipeline.py` + `app/fireflies/pipeline.py`
> **Потребитель:** n8n (`https://thehumanoid.app.n8n.cloud/webhook/<id>`)

---

## 1. Назначение

После того как Zoom/Fireflies-пайплайн сформировал саммари митинга, он
POST-ит его JSON-ом на внешний вебхук (n8n), где коллега обрабатывает
данные на своей стороне. Это «вывод коллеге через webhook».

---

## 2. Контракт доставки

**Endpoint:** `MEETING_WEBHOOK_URL` (env). Пусто → доставка выключена (no-op).

**Метод:** `POST`, `Content-Type: application/json; charset=utf-8`, timeout 10s.

**Гейт отправки (ВСЕ условия):**
1. `MEETING_WEBHOOK_URL` непустой.
2. `short_summary` непустой.
3. `summary_has_body(short_summary)` == True — у саммари есть **реальное тело**, а не только заголовок + «Участники:» + пробелы (FR-CR-05-160, 2026-06-03). Иначе → `meeting_webhook_skipped_empty_body`, не шлём.

**Payload (зафиксирован, см. `test_meeting_webhook.py`):**

| Поле | Тип | Примечание |
|---|---|---|
| `source` | str | `"zoom"` \| `"fireflies"` |
| `source_id` | str | `zoom_id` / `fireflies_id` |
| `title` | str | заголовок митинга |
| `meeting_date` | ISO str \| null | |
| `duration_seconds` | int \| null | |
| `short_summary` | str | |
| `detailed_summary` | str \| null | |
| `google_doc_url` | str \| null | |
| `participants` | list[str] | |
| `tasks_count` | int \| null | **ЧИСЛО задач, НЕ список.** Заголовков/owner/дедлайнов в payload НЕТ. |

⚠️ **Ограничение v1:** в payload нет самого списка задач и нет транскрипта.
Если потребителю нужен список задач — это осознанное расширение контракта
(обновить payload + `test_fr_cr_05_160_payload_shape_locked`).

**Возврат / ошибки:** функция возвращает `True` на 2xx, `False` на любой
ошибке. Ошибки логируются и **никогда не роняют пайплайн** (вебхук —
best-effort, после Slack-mirror).

---

## 3. Где запускается

Вебхук дёргается ТОЛЬКО из Zoom/Fireflies-пайплайна, а он крутится в
контейнере `manager-zoom-ff-1` (`python -m ops.zoom_fireflies_runner`).
Поэтому `MEETING_WEBHOOK_URL` обязан быть в env именно этого деплоя —
не только в основном боте.

См. `AGENTS.md` §2 про раскол деплоев `manager-*` vs `slack-task-*`.

---

## 4. Мониторинг (анти-регрессия)

**Инцидент 2026-06-03:** вебхук молчал недели. `MEETING_WEBHOOK_URL` был
выставлен только на старом деплое `slack-task-*` (который не обрабатывает
митинги), а новый `manager-zoom-ff-1` переменную при пересоздании не
получил. Ошибок не было — поэтому никто не заметил.

**Меры:**

1. **Лог `meeting_webhook_skipped_no_url`** (WARNING) — пишется, когда
   митинг САММАРИЗИРОВАН, но `MEETING_WEBHOOK_URL` пуст. То есть «было что
   отправить, но некуда». Делает тихий misconfig видимым.
2. **Алерт:** мониторинг должен триггерить, если за сутки появляется хоть
   одна строка `meeting_webhook_skipped_no_url` ИЛИ если при наличии
   обработанных митингов нет ни одного `meeting_webhook_posted ok=True`.
3. **Документация:** `MEETING_WEBHOOK_URL` зафиксирован в `.env.example`
   (раньше его не было нигде в репе — корневая причина потери при
   передеплое).

**Сигнальные строки в логах:**
- `meeting_webhook_posted ok=True` — успешная доставка.
- `meeting_webhook_http_error` / `meeting_webhook_unexpected_error` — сбой доставки.
- `meeting_webhook_skipped_no_url` — есть саммари, но URL не задан (**misconfig**).

---

## 5. Проверка вручную (smoke-test)

Реальный POST из боевого контейнера (помечен `[TEST]`):

```bash
docker exec manager-zoom-ff-1 python -c "
from app.config import get_settings
from app.services.meeting_webhook import post_meeting_to_webhook
from datetime import datetime, timezone
s = get_settings()
ok = post_meeting_to_webhook(
    webhook_url=s.meeting_webhook_url, source='zoom',
    source_id='healthcheck-TEST', title='[TEST] webhook check',
    meeting_date=datetime.now(timezone.utc), duration_seconds=60,
    short_summary='ping', detailed_summary='test',
    google_doc_url=None, participants=['Andre'], tasks_count=0)
print('WEBHOOK OK' if ok else 'WEBHOOK FAILED')
"
```

`WEBHOOK OK` → доставка работает, payload ушёл в n8n.

---

## 5a. Known issues (вскрыто 2026-06-03 на разборе «Алина, Ирина»)

Разбор по трейсам `/app/traces/zoom-<id>.jsonl` вскрыл цепочку багов. Часть
исправлена в этой ветке, часть — задокументирована для отдельной работы.

| # | Баг | Статус |
|---|---|---|
| B1 | **Бессодержательные саммари уходят в Slack и на вебхук** (заголовок + «Участники:» без сути). Корень — Whisper-галлюцинация / тонкий VTT. | ✅ Фикс: гейт `summary_has_body` на вебхуке (+ лог `meeting_webhook_skipped_empty_body`). Аналогичный гейт нужен и на Slack-публикации (`maybe_auto_publish`) — TODO. |
| B2 | **Вебхук вызывается ИЗ недокоммиченной транзакции** (внутри `_send_short_summary`). Если транзакция откатывается (как у «Алина, Ирина»), коллеге уходит огрызок от встречи, которой в БД не осталось. | ⚠️ TODO: выносить вебхук ЗА commit; слать только финальное, персистентное саммари. |
| B3 | **Откат транзакции теряет всю встречу** — строка `zoom_recordings` исчезла, хотя пайплайн отлогировал успех. Точная причина не видна: боевой раннер — кастомный файл `/home/andre/manager-zff/zoom_fireflies_runner.py`, которого нет в репе. | ⚠️ TODO: смотреть кастомный раннер; doc_export сделать гарантированно не-фатальным. |
| B4 | **Counterparty-резолвер форс-матчит фонетический мусор.** Из рваного VTT: `Klef→Ross Cliff`, `Mazon→Amazon`, `Almiraya→Mirae`, `TCB→TCP`, `Truer→TruArrow`, `K Stix→Styx` — ~половина «матчей» выдумана, вопреки инструкции «ставь null без уверенного соответствия». Плюс сам vector-каталог замусорен фонетическими дублями (3× Tether: `Tesor/«Тезору»/«Тезор»`). | ⚠️ TODO: confidence-гейт в `counterparty_catalog_resolver`; чистка каталога от дублей. |
| B5 | **Галлюцинация Whisper не подавляет публикацию.** `looks_like_whisper_hallucination`/откат на VTT срабатывают, но дальше тонкий VTT всё равно суммаризируется и публикуется. | Частично закрыт B1-гейтом; полноценно — гейт «hallucinated → не публиковать» на уровне шага. |
| B6 | **AUTO_SEND_TO_SLACK_ENABLED и MEETING_WEBHOOK_URL терялись при пересоздании контейнера** (рантайм-env не в скрипте `recreate-*.sh`). | ✅ Задокументировано (`.env.example`, `AGENTS.md`); env вписаны в `recreate-manager-zoom-ff-1.sh`. Системно — бейкать все env в скрипт. |

## 6. Открытые вопросы

- Нужен ли потребителю список задач/транскрипт в payload (см. §2 ограничение)?
- Ретраи при 5xx (сейчас одна попытка, best-effort)? Для v1 — не нужно.
- Подпись/секрет в заголовке для верификации на стороне n8n (сейчас нет)?
