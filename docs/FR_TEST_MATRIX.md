# FR → Test матрица (автогенерация)

> grep `FR-*` по `tests/requirements/*.py` + все `SPEC*.md`. См. [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md) §9.

## Сводка

| Метрика | Значение |
|---|---|
| Тест-файлов | 132 (114 с FR) |
| ✅ Покрыты (SPEC.md ∩ tests) | 218 |
| ⚠️ В SPEC.md без теста | 28 |
| 📘 В companion-спеках + есть тест | 19 |
| 🔴 Orphan (в тестах, нигде в спеках) | 20 |

## ⚠️ FR в SPEC.md без тест-ссылки

_Описаны, но нет явной ссылки из теста. Часть — мета-FR (193-1..6 — зонтичные), часть реально без покрытия (194/198 — manual smoke)._

- `FR-CR-04-11`
- `FR-CR-04-16`
- `FR-CR-05-06`
- `FR-CR-05-30`
- `FR-CR-05-38`
- `FR-CR-05-42`
- `FR-CR-05-43`
- `FR-CR-05-50`
- `FR-CR-05-54`
- `FR-CR-05-55`
- `FR-CR-05-56`
- `FR-CR-05-96`
- `FR-CR-05-98`
- `FR-CR-05-109`
- `FR-CR-05-114`
- `FR-CR-05-140`
- `FR-CR-05-146`
- `FR-CR-05-146e`
- `FR-CR-05-163`
- `FR-CR-05-177`
- `FR-CR-05-178`
- `FR-CR-05-193-1`
- `FR-CR-05-193-2`
- `FR-CR-05-193-4`
- `FR-CR-05-193-5`
- `FR-CR-05-193-6`
- `FR-CR-05-194`
- `FR-CR-05-198`

## 🔴 Orphan — в тестах, но НИ В ОДНОЙ спеке

_Опечатки в ID, либо sub-FR не задокументированы, либо устаревшие тесты. Требуют ревизии:_

- `FR-CR-05` → test_sheets_sync_hooks.py
- `FR-CR-05-60` → test_team_members.py
- `FR-CR-05-61` → test_google_tasks_pull.py
- `FR-CR-05-62` → test_intent_pipeline.py
- `FR-CR-05-64` → test_google_tasks_pull.py
- `FR-CR-05-65` → test_telegram_cards.py
- `FR-CR-05-66` → test_evening_status.py, test_morning_cards.py
- `FR-CR-05-68` → test_telegram_conversations.py
- `FR-CR-05-69` → test_telegram_handlers.py
- `FR-CR-05-71` → test_intent_pipeline.py
- `FR-CR-05-80` → test_telegram_cards.py
- `FR-CR-05-130` → test_counterparties.py, test_zoom.py
- `FR-CR-05-131` → test_counterparties.py
- `FR-CR-05-146a` → test_audio_transcription.py
- `FR-CR-05-146b` → test_audio_transcription.py
- `FR-CR-05-146c` → test_zoom.py
- `FR-CR-05-171` → test_counterparty_briefs.py
- `FR-CR-05-182` → test_fireflies.py
- `FR-CR-05-183` → test_zoom.py
- `FR-CR-05-184` → test_send_todo_trailer.py

## 📘 Companion-спеки (FR + тест, не в main SPEC.md)

| FR-ID | Спека | Тесты |
|---|---|---|
| `FR-CB-1` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB-2` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB2-1` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB2-2` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB2-3` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB2-4` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB2-5` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB2-6` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB2-200` | SPEC_CEO_BRAIN_BOT_v0.1.md | test_ceo_brain.py |
| `FR-CB-3` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB-4` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB-5` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CR-05-164` | SPEC_MEETING_AGENDA_v0.1.md | test_fireflies.py |
| `FR-CR-05-165` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md, SPEC_MEETING_AGENDA_v0.1.md | test_agenda.py |
| `FR-CR-05-166` | SPEC_MEETING_AGENDA_v0.1.md | test_agenda.py |
| `FR-CR-05-168` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB-6` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB-7` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |
| `FR-CB-8` | SPEC_COUNTERPARTY_BRIEFS_v0.1.md | test_counterparty_briefs.py |

## ✅ Полная матрица (SPEC.md FR → тесты)

| FR-ID | Тест-файлы |
|---|---|
| `FR-CR-1` | test_cr01_finalize.py, test_cr01_nfr.py, test_cr01_owners_workload.py, test_rate_limiter.py |
| `FR-CR-2` | test_cr01_digest.py, test_cr01_nfr.py, test_cr01_owners_workload.py |
| `FR-CR-02-1` | test_cr02_spec_coverage.py, test_cr03_mention_vs_passive.py, test_mention_fallback_date.py, test_shared_helpers.py |
| `FR-CR-02-2` | test_bootstrap.py, test_cr02_spec_coverage.py |
| `FR-CR-02-3` | test_apply_payload_to_task.py, test_cr02_spec_coverage.py, test_shared_helpers.py, test_widget_morph_and_dm.py |
| `FR-CR-02-4` | test_cr02_spec_coverage.py, test_widget_morph_and_dm.py |
| `FR-CR-02-5` | test_cr02_spec_coverage.py |
| `FR-CR-02-6` | test_cr02_spec_coverage.py, test_dm_threading.py |
| `FR-CR-02-7` | test_cr02_spec_coverage.py |
| `FR-CR-3` | test_cr01_lifecycle.py, test_cr01_nfr.py |
| `FR-CR-03-1` | test_cr03_employees_admin.py, test_shared_helpers.py |
| `FR-CR-03-2` | test_admin_review_paths.py, test_cr03_admin_review.py, test_cr03_employees_admin.py |
| `FR-CR-03-3` | test_cr03_admin_digest.py, test_cr03_admin_review.py, test_cr03_weekly_plan.py |
| `FR-CR-03-4` | test_cr03_admin_review.py, test_cr03_thread_reminders.py |
| `FR-CR-03-5` | test_admin_review_paths.py, test_cr03_admin_review.py, test_task_edit_button.py |
| `FR-CR-03-6` | test_cr03_weekly_plan.py, test_weekly_plan_handlers.py |
| `FR-CR-03-7` | test_cr03_completion_artifact.py |
| `FR-CR-03-8` | test_cr03_admin_digest.py |
| `FR-CR-03-9` | test_cr03_thread_reminders.py |
| `FR-CR-03-10` | test_dm_threading.py, test_widget_morph_and_dm.py |
| `FR-CR-4` | test_cr01_lifecycle.py |
| `FR-CR-04-1` | test_bootstrap.py, test_classifier_error_paths.py, test_intent_graph.py, test_intent_pipeline.py, test_owner_focused_prompt.py |
| `FR-CR-04-2` | test_intent_graph.py, test_intent_pipeline.py, test_nfr_01_05.py |
| `FR-CR-04-3` | test_date_resolver.py, test_intent_graph.py, test_mention_fallback_date.py |
| `FR-CR-04-04` | test_fireflies.py |
| `FR-CR-04-4` | test_owner_focused_prompt.py |
| `FR-CR-04-5` | test_date_resolver.py, test_intent_pipeline.py |
| `FR-CR-04-6` | test_cr03_admin_review.py, test_cr03_mention_vs_passive.py, test_mention_passive_symmetry.py, test_passive_draft_card.py |
| `FR-CR-04-7` | test_mention_follow_up_for_assumed_owner.py, test_mention_passive_symmetry.py |
| `FR-CR-04-8` | test_audio_transcription.py |
| `FR-CR-04-9` | test_classifier_error_paths.py, test_passive_pipeline_runs_always.py, test_prefilter_override.py |
| `FR-CR-04-10` | test_task_edit_button.py |
| `FR-CR-04-12` | test_owner_employees_table.py |
| `FR-CR-04-13` | test_task_extras.py |
| `FR-CR-04-14` | test_daily_plan.py |
| `FR-CR-04-15` | test_task_recurring.py |
| `FR-CR-04-17` | test_employees_workspace_sync.py |
| `FR-CR-04-18` | test_cr01_finalize.py, test_priority_emoji.py, test_task_recurring.py |
| `FR-CR-04-19` | test_fr_06_10_explicit.py, test_meetings_disabled.py, test_prefilter_override.py |
| `FR-CR-04-20` | test_cr01_handlers.py, test_cr01_lifecycle.py, test_task_cancel_delete.py |
| `FR-CR-04-21` | test_cr03_completion_artifact.py, test_task_cancel_delete.py |
| `FR-CR-04-22` | test_employees_per_channel_sync.py, test_owner_hallucination_guard.py |
| `FR-CR-04-23` | test_resync_sheet_cli.py, test_sheets_sync_hooks.py |
| `FR-CR-04-24` | test_owners_from_employees.py |
| `FR-CR-04-25` | test_daily_plan.py |
| `FR-CR-04-26` | test_sheets_sync_hooks.py, test_task_source_kind.py, test_telegram_bot.py, test_telegram_ingest.py, test_units_support.py |
| `FR-CR-04-27` | test_telegram_listener.py |
| `FR-CR-04-28` | test_telegram_handlers.py, test_telegram_listener.py |
| `FR-CR-04-29` | test_telegram_conversations.py, test_telegram_notifications.py |
| `FR-CR-04-30` | test_telegram_ingest.py |
| `FR-CR-04-31` | test_telegram_cards.py |
| `FR-CR-04-32` | test_telegram_conversations.py, test_telegram_ingest.py, test_telegram_listener.py |
| `FR-CR-5` | test_cr01_finalize.py, test_cr01_lifecycle.py |
| `FR-CR-05-01` | test_cr01_digest.py, test_telegram_notifications.py |
| `FR-CR-05-02` | test_subscriber_updates.py |
| `FR-CR-05-03` | test_telegram_notifications.py |
| `FR-CR-05-04` | test_telegram_notifications.py |
| `FR-CR-05-05` | test_intent_pipeline.py, test_telegram_ingest.py |
| `FR-CR-05-07` | test_telegram_members.py |
| `FR-CR-05-08` | test_telegram_bot.py |
| `FR-CR-05-09` | test_intent_pipeline.py, test_telegram_cards.py, test_telegram_ingest.py |
| `FR-CR-05-10` | test_team_members.py, test_telegram_cards.py, test_telegram_ingest.py, test_telegram_listener.py |
| `FR-CR-05-11` | test_sheets_pull.py |
| `FR-CR-05-12` | test_intent_pipeline.py, test_team_members.py |
| `FR-CR-05-13` | test_intent_pipeline.py, test_task_dedup.py, test_team_members.py, test_telegram_cards.py |
| `FR-CR-05-14` | test_telegram_conversations.py, test_telegram_listener.py |
| `FR-CR-05-15` | test_sheets_pull.py, test_telegram_ingest.py |
| `FR-CR-05-16` | test_telegram_bot.py, test_telegram_conversations.py |
| `FR-CR-05-17` | test_telegram_ingest.py |
| `FR-CR-05-18` | test_telegram_bot.py, test_telegram_cards.py |
| `FR-CR-05-19` | test_telegram_bot.py |
| `FR-CR-05-20` | test_telegram_bot.py |
| `FR-CR-05-21` | test_team_members.py, test_telegram_ingest.py, test_telegram_members.py |
| `FR-CR-05-22` | test_intent_pipeline.py |
| `FR-CR-05-23` | test_team_members.py |
| `FR-CR-05-24` | test_team_members.py |
| `FR-CR-05-25` | test_team_members.py, test_telegram_ingest.py |
| `FR-CR-05-26` | test_telegram_bot.py, test_telegram_cards.py |
| `FR-CR-05-27` | test_team_members.py, test_telegram_members.py |
| `FR-CR-05-28` | test_telegram_listener.py |
| `FR-CR-05-29` | test_team_members.py |
| `FR-CR-05-31` | test_intent_pipeline.py |
| `FR-CR-05-32` | test_telegram_conversations.py |
| `FR-CR-05-33` | test_telegram_cards.py |
| `FR-CR-05-34` | test_telegram_bot.py |
| `FR-CR-05-35` | test_telegram_listener.py |
| `FR-CR-05-36` | test_telegram_listener.py |
| `FR-CR-05-37` | test_telegram_conversations.py |
| `FR-CR-05-39` | test_fireflies.py, test_task_source_kind.py |
| `FR-CR-05-40` | test_evening_status.py |
| `FR-CR-05-41` | test_morning_cards.py |
| `FR-CR-05-44` | test_telegram_listener.py |
| `FR-CR-05-45` | test_telegram_listener.py |
| `FR-CR-05-46` | test_intent_pipeline.py |
| `FR-CR-05-47` | test_telegram_cards.py |
| `FR-CR-05-48` | test_evening_status.py |
| `FR-CR-05-49` | test_evening_status.py, test_morning_cards.py |
| `FR-CR-05-51` | test_telegram_listener.py |
| `FR-CR-05-52` | test_intent_pipeline.py |
| `FR-CR-05-53` | test_fireflies.py |
| `FR-CR-05-57` | test_fireflies.py |
| `FR-CR-05-58` | test_fireflies.py, test_telegram_cards.py |
| `FR-CR-05-59` | test_fireflies.py, test_retro_share_docs_cli.py |
| `FR-CR-05-63` | test_cr01_lifecycle.py, test_cr02_spec_coverage.py, test_cr03_mention_vs_passive.py, test_followup_flow.py, test_fr_06_10_explicit.py, test_fr_11_12_persistence.py, test_google_tasks_pull.py, test_mention_fallback_always_creates.py, test_mention_passive_symmetry.py, test_zoom.py |
| `FR-CR-05-72` | test_fr_11_12_persistence.py, test_resync_sheet_cli.py |
| `FR-CR-05-77` | test_intent_pipeline.py |
| `FR-CR-05-78` | test_task_dedup.py |
| `FR-CR-05-79` | test_intent_pipeline.py |
| `FR-CR-05-82` | test_intent_pipeline.py |
| `FR-CR-05-83` | test_evening_status.py, test_telegram_digest_cron.py |
| `FR-CR-05-84` | test_morning_cards.py, test_telegram_digest_cron.py |
| `FR-CR-05-85` | test_telegram_digest_cron.py |
| `FR-CR-05-86` | test_resync_sheet_cli.py, test_sheets_sync_hooks.py |
| `FR-CR-05-87` | test_intent_graph.py, test_intent_pipeline.py |
| `FR-CR-05-88` | test_intent_pipeline.py |
| `FR-CR-05-89` | test_intent_pipeline.py, test_resync_sheet_cli.py |
| `FR-CR-05-90` | test_retro_share_docs_cli.py |
| `FR-CR-05-91` | test_evening_status.py, test_morning_cards.py, test_wipe_tasks_cli.py |
| `FR-CR-05-92` | test_intent_pipeline.py |
| `FR-CR-05-93` | test_intent_pipeline.py |
| `FR-CR-05-94` | test_intent_pipeline.py |
| `FR-CR-05-95` | test_intent_pipeline.py |
| `FR-CR-05-97` | test_task_dedup.py |
| `FR-CR-05-99` | test_intent_pipeline.py |
| `FR-CR-05-100` | test_intent_pipeline.py, test_task_dedup.py |
| `FR-CR-05-101` | test_intent_graph.py, test_intent_pipeline.py, test_task_dedup.py |
| `FR-CR-05-102` | test_task_dedup.py |
| `FR-CR-05-103` | test_intent_pipeline.py |
| `FR-CR-05-104` | test_intent_pipeline.py, test_llm_backends.py, test_task_dedup.py |
| `FR-CR-05-105` | test_telegram_ingest.py |
| `FR-CR-05-106` | test_llm_backends.py |
| `FR-CR-05-107` | test_llm_backends.py |
| `FR-CR-05-108` | test_intent_pipeline.py |
| `FR-CR-05-110` | test_task_dedup.py, test_telegram_listener.py |
| `FR-CR-05-111` | test_task_dedup.py |
| `FR-CR-05-112` | test_intent_pipeline.py |
| `FR-CR-05-113` | test_telegram_cards.py |
| `FR-CR-05-115` | test_fireflies.py |
| `FR-CR-05-116` | test_task_source_kind.py, test_zoom.py |
| `FR-CR-05-117` | test_fireflies.py, test_telegram_cards.py |
| `FR-CR-05-118` | test_task_source_kind.py, test_zoom.py |
| `FR-CR-05-119` | test_ephemeral_task_extraction.py, test_fireflies.py |
| `FR-CR-05-120` | test_fireflies.py, test_intent_pipeline.py, test_llm_backends.py, test_zoom.py |
| `FR-CR-05-121` | test_fireflies.py, test_zoom.py |
| `FR-CR-05-122` | test_zoom.py |
| `FR-CR-05-123` | test_fireflies.py |
| `FR-CR-05-124` | test_counterparties.py |
| `FR-CR-05-125` | test_counterparties.py |
| `FR-CR-05-126` | test_counterparties.py, test_fireflies.py |
| `FR-CR-05-127` | test_agenda_lite_compose.py, test_fireflies.py, test_zoom.py |
| `FR-CR-05-128` | test_counterparties.py, test_fireflies.py |
| `FR-CR-05-129` | test_counterparties.py, test_fireflies.py, test_summary_canonicalize.py, test_zoom.py |
| `FR-CR-05-132` | test_counterparties.py |
| `FR-CR-05-133` | test_counterparty_enrollment.py |
| `FR-CR-05-134` | test_fireflies.py, test_team_members.py, test_zoom.py |
| `FR-CR-05-136` | test_calendar_match.py |
| `FR-CR-05-137` | test_slack_mirror.py |
| `FR-CR-05-138` | test_counterparty_enrollment.py, test_counterparty_enrollment_batch.py |
| `FR-CR-05-139` | test_fireflies.py, test_zoom.py |
| `FR-CR-05-141` | test_slack_mirror.py |
| `FR-CR-05-142` | test_fireflies.py |
| `FR-CR-05-142a` | test_fireflies.py, test_team_members.py |
| `FR-CR-05-142b` | test_counterparties.py, test_fireflies.py, test_team_members.py |
| `FR-CR-05-143` | test_zoom.py |
| `FR-CR-05-144` | test_calendar_match.py |
| `FR-CR-05-145` | test_team_members.py |
| `FR-CR-05-147` | test_slack_mirror.py |
| `FR-CR-05-148` | test_audio_transcription.py, test_zoom.py |
| `FR-CR-05-149` | test_slack_mirror.py |
| `FR-CR-05-150` | test_slack_mirror.py |
| `FR-CR-05-151` | test_entity_pipeline_e2e.py, test_zoom.py |
| `FR-CR-05-152` | test_calendar_match.py |
| `FR-CR-05-153` | test_audio_transcription.py |
| `FR-CR-05-154` | test_fireflies.py |
| `FR-CR-05-167` | test_agenda.py, test_counterparty_briefs.py, test_fireflies.py, test_zoom.py |
| `FR-CR-05-169` | test_zoom.py |
| `FR-CR-05-170` | test_ceo_brain.py |
| `FR-CR-05-172` | test_zoom.py |
| `FR-CR-05-173` | test_zoom.py |
| `FR-CR-05-174` | test_zoom.py |
| `FR-CR-05-176` | test_fireflies.py |
| `FR-CR-05-179` | test_fireflies.py |
| `FR-CR-05-180` | test_ceo_brain.py |
| `FR-CR-05-181` | test_fireflies.py, test_zoom.py |
| `FR-CR-05-185` | test_ephemeral_task_extraction.py, test_fireflies.py, test_task_due.py |
| `FR-CR-05-189b` | test_send_todo_trailer.py |
| `FR-CR-05-191` | test_summary_canonicalize.py |
| `FR-CR-05-191b` | test_summary_canonicalize.py |
| `FR-CR-05-192` | test_populate_counterparties_canonical.py |
| `FR-CR-05-192aa` | test_agenda_direction_filter.py |
| `FR-CR-05-192ab` | test_agenda_last_prior_only.py |
| `FR-CR-05-192ac` | test_agenda_attendees_filter.py |
| `FR-CR-05-192k` | test_ephemeral_task_extraction.py |
| `FR-CR-05-192q` | test_quality_checklist.py |
| `FR-CR-05-192r` | test_apply_delegate_marker.py, test_ephemeral_task_extraction.py, test_task_owner_strict.py |
| `FR-CR-05-192t` | test_min_meeting_duration.py |
| `FR-CR-05-192u` | test_agenda.py, test_agenda_lite_compose.py |
| `FR-CR-05-192v` | test_ceo_brain_archive_best_effort.py, test_ceo_brain_classifier_wiring.py |
| `FR-CR-05-192w` | test_ceo_brain_classifier_wiring.py |
| `FR-CR-05-192y` | test_zoom_ff_runner_wiring.py |
| `FR-CR-05-193` | test_reasoning_extract_prompt.py |
| `FR-CR-05-193a` | test_reasoning_extract_prompt.py |
| `FR-CR-05-193b` | test_entity_matcher_orgs.py, test_entity_matcher_people.py, test_team_member_canonical.py |
| `FR-CR-05-193c` | test_entity_apply.py, test_entity_rewrite.py |
| `FR-CR-05-193d` | test_counterparty_aliases.py, test_team_member_notes_dsl.py |
| `FR-CR-05-193e` | test_entity_resolution_cache.py |
| `FR-CR-05-193f` | test_backfill_counterparty_aliases.py |
| `FR-CR-05-193g` | test_entity_pipeline_e2e.py |
| `FR-CR-05-193h` | test_task_owner_strict.py |
| `FR-CR-05-193-3` | test_entity_apply.py |
| `FR-CR-05-195` | test_fireflies_duration_units.py |
| `FR-CR-05-196` | test_retry_cap_and_sentinels.py |
| `FR-CR-05-197` | test_retry_cap_and_sentinels.py |
| `FR-CR-05-199` | test_slack_publish_all_tasks.py |
| `FR-CR-05-199b` | test_slack_publish_all_tasks.py |
| `FR-CR-05-199c` | test_team_member_canonical.py |
| `FR-CR-6` | test_cr01_digest.py |
| `FR-CR-7` | test_cr01_lifecycle.py |
