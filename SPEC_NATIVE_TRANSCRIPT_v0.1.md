# SPEC v0.1 — Native transcripts first (FR-NT-TR)

> **Дата:** 2026-06-04 · **Эпик:** `FR-NT-TR` (transcript source)
> **Связано:** [SPEC_NOTE_TAKER_v0.1.md](./SPEC_NOTE_TAKER_v0.1.md) (FR-NT-2.x),
> [AUDIT.md](./AUDIT.md) §1 (FR-NT-2.3 — FF native transcript не реюзался).
> **Флаги:** `ZOOM_PREFER_NATIVE_TRANSCRIPT`, `FIREFLIES_PREFER_NATIVE_TRANSCRIPT` (default false),
> `NATIVE_TRANSCRIPT_MIN_CHARS` (default 100 — порог приёмки нативного транскрипта).
> **Статус:** ✅ реализовано — `should_use_native` (app/services/transcription.py),
> gate в обоих `_step_transcribe`, тесты `tests/requirements/test_native_transcript_select.py` (6).

## 1. Задача

Сейчас primary-транскрипт делает **Whisper** (operator-pinned FR-CR-05-153 reverted),
а нативные транскрипты сервисов используются только как fallback:
- Zoom: `fetch_vtt_transcript` — лишь когда Whisper пустой/галлюцинирует (FR-CR-05-148);
- Fireflies: `fetch_transcript_text` (GraphQL `sentences`) — лишь когда аудио > 25 МБ (FR-CR-05-115).

**Новое поведение:** primary-транскрипт = **нативный сервисный** (Zoom VTT / Fireflies
sentences). Whisper — **fallback**, когда нативного нет/он пустой. **Только Zoom** дополнительно
делает один английский STT-проход + merge (существующая билингва, без изменений).

Цель: убрать Whisper как основной (стоимость + искажения имён), опираться на транскрипт сервиса.

## 2. Решения

| # | Решение |
|---|---|
| D1 | Zoom primary = VTT (`fetch_vtt_transcript`) при `ZOOM_PREFER_NATIVE_TRANSCRIPT=true` |
| D2 | Fireflies primary = `fetch_transcript_text` при `FIREFLIES_PREFER_NATIVE_TRANSCRIPT=true` |
| D3 | Если нативный пустой / короче порога → **fallback на Whisper** (текущий путь целиком) |
| D4 | Zoom: после primary — билингва (детектор → англ. Whisper-проход → merge), **как сейчас** |
| D5 | Fireflies: билингвы нет, чистый нативный (или Whisper-fallback) |
| D6 | Оба флага **default false** → поведение байт-в-байт прежнее, пока оператор не включит |

## 3. Поведение (gate + fallback)

```
_step_transcribe (Zoom):
  if ZOOM_PREFER_NATIVE_TRANSCRIPT:
      native = fetch_vtt_transcript(_find_vtt_download_url(row))
      if should_use_native(prefer=True, native): 
          transcript = native; source="native_vtt"          # Whisper НЕ запускается
      else:
          transcript = <текущий Whisper-путь + VTT-fallback-on-hallucination>
  else:
      transcript = <текущий Whisper-путь>                    # без изменений
  → bilingual restoration (D4, как сейчас)

_step_transcribe (Fireflies):
  if FIREFLIES_PREFER_NATIVE_TRANSCRIPT:
      native = fetch_transcript_text(row.fireflies_id)
      if should_use_native(prefer=True, native):
          transcript = native; source="native_ff"
      else:
          transcript = <текущий Whisper-путь>
  else:
      transcript = <текущий Whisper-путь>
```

`should_use_native(prefer, native_text, min_native_chars=100)` — чистая функция:
`prefer AND native_text.strip() AND len ≥ min` → True. Иначе False (→ Whisper).

## 4. Инварианты безопасности

- **I1.** Флаг off → ровно прежний код (Whisper-first). Ноль изменений по умолчанию.
- **I2.** Флаг on, но нативного нет/пустой/короткий → Whisper-fallback. Транскрипт всегда получается.
- **I3.** Билингва Zoom не зависит от источника primary — работает поверх любого (native или Whisper).
- **I4.** Источник пишется в лог/трейс (`transcript_source = native_vtt | native_ff | whisper`) для аудита.
- **I5.** Идемпотентность шага сохраняется: `row.transcribed && row.transcript_text` → no-op.

## 5. Что НЕ меняется

- Билингва-restorer (детектор/англ-проход/merge) — без правок.
- Whisper-bias-prompt, чанкинг, VTT-fallback-on-hallucination — остаются для Whisper-пути.
- Downstream (detailed/short/tasks/doc/webhook) — без изменений, читает `row.transcript_text`.

## 6. Тесты

- `test_native_transcript_select.py`:
  - `should_use_native(True, "<100+ chars>")` → True;
  - `should_use_native(True, "")` / короткий / None → False (→ Whisper);
  - `should_use_native(False, "<long>")` → False (флаг off).
- (интеграция, на проде/shadow) smoke: включить флаг на одной встрече, сверить `transcript_source` в трейсе + что Whisper не вызывался.

## 7. Открытые вопросы

- Порог `min_native_chars` (старт 100) — тюним, если VTT иногда отдаёт обрывки.
- FF `fetch_transcript_text` требует, чтобы транскрипт у Fireflies был готов (realtime может опаздывать) — если пусто, D3 уводит на Whisper; мониторим долю fallback.
- Качество Zoom VTT vs Whisper по именам — сравнить на первых встречах (англ. merge должен компенсировать).
