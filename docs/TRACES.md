# Pipeline trace catalog (FR-CR-05-128)

Every meeting-pipeline step (Fireflies + Zoom) emits structured `structlog`
events you can grep over the listener / migrate output to follow a single
recording end-to-end. This file is the canonical inventory: if a behaviour
should be traceable from logs and the relevant event is missing here,
that's a bug.

## Quick recipes

### Walk through one recording from start to finish
```sh
sudo docker logs manager-bot-1 2>&1 \
  | grep -E "(fireflies|zoom)_step_(started|done|failed)" \
  | grep "<recording-id>"
```
Each step is bracketed by `*_step_started` ... `*_step_done duration_ms=N`
(or `*_step_failed` on exception). Steps appear in this order:

```
download → transcribe → detailed_summary → match_counterparties
        → extract_tasks → verify_tasks → doc_export
        → short_summary → send_short_summary → post_task_cards
```

### Latest 10 meetings end-of-pipeline summaries
```sh
sudo docker logs manager-bot-1 2>&1 | grep "_pipeline_summary" | tail -10
```
Single line per recording with: `transcript_chars`, `detailed_chars`,
`short_chars`, `tasks_count`, `tasks_titles`, `tasks_owners`,
`counterparties_count`, `google_doc_url`, `errors`.

### Verify Whisper biasing fired
```sh
sudo docker logs manager-bot-1 2>&1 \
  | grep -E "(fireflies|zoom)_whisper_bias_prompt_built" \
  | tail
```
Each line carries `prompt_chars=N` (cap is `_SHORT_SUMMARY_ONE_MESSAGE_LIMIT
≈ 800` chars in the bias prompt). 0 means the operator's directories are
empty or the bias call failed silently; check
`whisper_bias_(team|counterparty)_query_failed` next.

### See what counterparty matcher actually returned
```sh
sudo docker logs manager-bot-1 2>&1 \
  | grep "counterparty_match_" \
  | grep "<recording-id>"
```
Three events per recording:
- `counterparty_match_call_started` — fields: `directory_size`,
  `shortlist_size`, `shortlist_sample` (first 8 rows by id),
  `transcript_chars`, `transcript_preview` (first 240 chars), `model`,
  `reasoning_effort`, `prompt_chars`.
- `counterparty_match_llm_returned` — `raw_ids_count`, `raw_ids_sample`,
  `valid_ids_count`, `invalid_ids` (LLM hallucinations dropped).
- `counterparty_match_done` (or `*_skipped_empty_directory`) — final outcome.

### See what task-extraction LLM emitted before owner resolution
```sh
sudo docker logs manager-bot-1 2>&1 \
  | grep "_task_extraction_llm_returned" \
  | grep "<recording-id>"
```
Carries `raw_count`, `raw_titles[:25]`, `raw_owners[:25]` so you can
sanity-check the LLM's output BEFORE the pipeline's owner-resolution +
admin-fallback munging.

### See per-task owner routing decisions
```sh
sudo docker logs manager-bot-1 2>&1 \
  | grep "fireflies_task_owner_resolved" \
  | grep "<recording-id>"
```
Per-task line: `title`, `llm_owner`, `final_owner`, `resolution`
(`llm` | `hallucinated_uid_dropped` | `admin_fallback_null_owner` |
`hallucinated_uid_dropped_then_admin_fallback`).

### Catch tasks the verifier added
```sh
sudo docker logs manager-bot-1 2>&1 \
  | grep "_task_verification_done" \
  | grep "<recording-id>"
```
Fields: `existing_count`, `newly_added`, `new_titles[:25]`, `new_owners[:25]`.

---

## Event inventory

### Pipeline orchestration
| Event | Fields | When |
|---|---|---|
| `fireflies_step_started` / `zoom_step_started` | `step`, `<recording-id>` | Every step bracket open. |
| `fireflies_step_done` / `zoom_step_done` | `step`, `duration_ms`, `<recording-id>` | Every step bracket close (success). |
| `fireflies_step_failed` / `zoom_step_failed` | `step`, `duration_ms`, `error`, `<recording-id>` | Step raised. |
| `fireflies_pipeline_summary` / `zoom_pipeline_summary` | `transcript_chars`, `detailed_chars`, `short_chars`, `tasks_count`, `tasks_titles`, `tasks_owners`, `counterparties`, `counterparties_count`, `google_doc_url`, `errors`, `title`, `<recording-id>` | End of `process_one`. |
| `zoom_recording_step_failed` | `step`, `err`, `zoom_id` | Inner-step early-return inside Zoom's loop. |
| `fireflies_recording_processed` / `zoom_recording_processed` | full report | After `process_one`. |

### Step 1 — audio download
| Event | Fields | When |
|---|---|---|
| (no dedicated event) | — | Tracked via `_step_started`/`_step_done step=download`. |

### Step 2 — Whisper transcription
| Event | Fields | When |
|---|---|---|
| `fireflies_whisper_bias_prompt_built` / `zoom_whisper_bias_prompt_built` | `prompt_chars`, `<recording-id>` | Bias prompt assembled (FR-CR-05-127). |
| `fireflies_whisper_bias_prompt_failed` / `zoom_whisper_bias_prompt_failed` | `error`, `<recording-id>` | Bias-builder threw; transcription continues without bias. |
| `whisper_bias_team_query_failed` | `error` | DB query for team_members failed inside bias builder. |
| `whisper_bias_counterparty_query_failed` | `error` | DB query for counterparties failed inside bias builder. |
| `fireflies_audio_chunked_for_whisper` / `zoom_audio_chunked_for_whisper` | `size`, `chunks`, `<recording-id>` | Audio > 24 MB → ffmpeg-split into N chunks. |
| `whisper_call_failed` | `error`, `filename` | OpenAI Whisper threw (quota / network / file). |

### Step 3 — detailed summary (LLM)
No dedicated events — only `_step_done step=detailed_summary` with `duration_ms`.

### Step 4 — counterparty matching
| Event | Fields | When |
|---|---|---|
| `counterparty_match_skipped_empty_directory` | `transcript_chars` | `counterparties` table is empty → no LLM call. |
| `counterparty_match_call_started` | `directory_size`, `shortlist_size`, `shortlist_sample`, `transcript_chars`, `transcript_preview`, `model`, `reasoning_effort`, `prompt_chars` | LLM call about to fire. |
| `counterparty_match_llm_returned` | `raw_ids_count`, `raw_ids_sample`, `valid_ids_count`, `invalid_ids` | After LLM returned, before persisting. |
| `counterparty_match_llm_failed` | `error` | LLM threw. |
| `counterparty_match_done` | `matched_names` | Final result. |
| `fireflies_counterparty_match_done` / `zoom_counterparty_match_done` | `matched`, `<recording-id>` | Pipeline-side after-match log. |
| `fireflies_counterparty_match_unexpected_error` / `zoom_counterparty_match_unexpected_error` | `error`, `<recording-id>` | Caught + non-fatal (pipeline continues without 🔗 block). |

### Step 4b — counterparty enrollment widgets (FR-CR-05-133)
| Event | Fields | When |
|---|---|---|
| `counterparty_enrollment_posted` | `unique_mentions`, `recipients`, `posted`, `skipped_existing`, `failed`, `<recording-id>` | After `_step_enroll_unresolved` finishes a pass. `skipped_existing` counts UNIQUE-constraint hits (rerun-idempotent). |
| `counterparty_enrollment_send_failed` | `prompt_id`, `uid`, `error` | Telegram `sendMessage` raised for one widget; row stays in DB without `yesno_message_id`. |
| `counterparty_enrollment_yes` | `prompt_id`, `mention` | Operator clicked Yes; row → `awaiting_context`. |
| `counterparty_enrollment_no` | `prompt_id`, `mention` | Operator clicked No; row → `declined`; no Counterparty write. |
| `counterparty_enrollment_skip` | `prompt_id`, `mention`, `counterparty_id` | Operator clicked Skip in stage-2; row → `completed_skipped`; Counterparty hub created without satellite. |
| `counterparty_enrollment_completed_with_context` | `prompt_id`, `mention`, `counterparty_id`, `context_chars` | Operator sent text/voice context; row → `completed_added`; Counterparty hub + `telegram_enrollment` satellite created. |
| `counterparty_enrollment_yes_edit_failed` / `..._no_edit_failed` / `..._skip_edit_failed` / `..._context_edit_failed` | `prompt_id`, `error` | Telegram `editMessageText` raised; state transition still committed. |

### Step 5 — task extraction (first pass)
| Event | Fields | When |
|---|---|---|
| `fireflies_team_registry_unavailable` | `error` | `team_members` query failed → empty list passed to LLM. |
| `fireflies_task_extraction_llm_returned` / `zoom_task_extraction_llm_returned` | `model`, `raw_count`, `raw_titles[:25]`, `raw_owners[:25]`, `<recording-id>` | LLM returned; before owner-resolution and persistence. |
| `fireflies_task_extraction_llm_failed` / `zoom_task_extraction_llm_failed` | `model`, `reasoning_effort`, `error`, `<recording-id>` | LLM threw. |
| `fireflies_task_extraction_returned_empty` / `zoom_task_extraction_returned_empty` | `model`, `detailed_chars`, `hint`, `<recording-id>` | LLM returned `[]` — disambiguation: procedural meeting vs. broken prompt/model. |
| `fireflies_task_owner_resolved` | `title`, `llm_owner`, `final_owner`, `resolution`, `<recording-id>` | Per-task routing decision. |
| `fireflies_task_create_failed` / `zoom_task_create_failed` | `title`, `error` | Task INSERT raised; pipeline continues with the rest. |
| `fireflies_topic_title_derivation_failed` | `error`, `<recording-id>` | Auto-stamp title rewrite failed. |
| `fireflies_topic_title_derived` | `old`, `new`, `<recording-id>` | Auto-stamp title was rewritten by the LLM. |

### Step 6 — verifier pass
| Event | Fields | When |
|---|---|---|
| `fireflies_task_verification_done` / `zoom_task_verification_done` | `existing_count`, `newly_added`, `new_titles[:25]`, `new_owners[:25]`, `<recording-id>` | Verifier finished. |
| `fireflies_task_verification_failed` / `zoom_task_verification_failed` | `error`, `<recording-id>` | Verifier LLM threw. |
| `fireflies_task_verification_unexpected_error` / `zoom_task_verification_unexpected_error` | `error`, `<recording-id>` | Caught + non-fatal. |
| `fireflies_task_verify_create_failed` / `zoom_task_verify_create_failed` | `title`, `error` | Task INSERT during verifier pass raised. |

### Step 7 — Google Doc export
| Event | Fields | When |
|---|---|---|
| `fireflies_doc_export_failed` | `recording_id`, `error` | Docs API threw. |

### Step 8 — short summary (TG)
| Event | Fields | When |
|---|---|---|
| `fireflies_short_summary_failed` | `recording_id`, `error` | Short-summary build failed. |
| `fireflies_short_summary_compact_todo` / `zoom_short_summary_compact_todo` | `full_chars`, `limit`, `<recording-id>` | Verbose To-Do > 4000 chars → compact (title-only) fallback (FR-CR-05-128). |
| `fireflies_short_summary_send_failed` | `uid`, `error` | Telegram DM `sendMessage` raised for one admin. |
| `zoom_short_summary_dm_failed` | `uid`, `error` | Same for Zoom path. |

### Step 9 — per-task DM cards
| Event | Fields | When |
|---|---|---|
| `fireflies_task_card_post_failed` / `zoom_task_card_post_failed` | `task_id`, `error` | `post_initial_card` raised. |
| `fireflies_post_task_cards_unexpected_error` / `zoom_post_task_cards_unexpected_error` | `error`, `<recording-id>` | Caught + non-fatal. |

### Pipeline-level extract loop errors (Zoom-only orchestration shape)
| Event | Fields | When |
|---|---|---|
| `zoom_extract_tasks_unexpected_error` | `error`, `zoom_id` | Caught around `_step_extract_tasks`. |

### Slack-side audio (legacy non-meeting path)
| Event | Fields | When |
|---|---|---|
| `slack_file_download_failed` | `url`, `error` | Slack file download raised. |
| `slack_audio_too_large` | `url`, `bytes`, `cap` | Slack audio > 25 MB cap. |

---

## Adding new traces

When you add a new pipeline step or LLM round-trip:
1. Wrap the step in `_trace_step("fireflies"|"zoom", "<step-name>", **ctx)`
   so the standard `started`/`done`/`failed` triplet fires automatically.
2. Add per-call transparency logs (`*_llm_returned` style) right before
   you filter / persist the model output, so the operator can replay
   the run later.
3. Record the new event in the table above. PR is incomplete without it.
