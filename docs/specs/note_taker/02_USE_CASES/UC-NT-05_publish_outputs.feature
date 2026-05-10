Feature: UC-NT-05 — Publish outputs (Slack + TG + Doc + webhook)
  Implements: US-NT-3, US-NT-4, US-NT-5
  Covers requirements: FR-NT-9.1, FR-NT-9.2, FR-NT-9.3, FR-NT-9.4, FR-NT-9.5,
                       FR-NT-9.6, NFR-NT-U.1, NFR-NT-I.1, NFR-NT-S.2
  Tested by: T-NT-010, T-NT-011, T-NT-012, T-NT-013, T-NT-014

  Background:
    Given pipeline has produced row.short_summary (non-empty), row.detailed_summary,
      row.google_doc_url
    And SLACK_BOT_TOKEN, SLACK_MEETING_CHANNEL_ID, TELEGRAM_BOT_TOKEN,
      MEETING_WEBHOOK_URL are configured

  Scenario: Slack post — single chunk
    Given short_summary is 2 KB (under SLACK_TEXT_CHUNK_CHARS=3500)
    When _step_send_short_summary runs slack mirror
    Then chat.postMessage is called with the body
    And slack_mirror_chunk_posted chunk_index=1 chunk_total=1 thread_ts=None is logged
    And the message appears in channel D0ASY5QF6UX

  Scenario: Slack post — multi-chunk with threading
    Given short_summary is 8 KB (≈3 chunks of ≤3500 chars)
    When slack mirror posts the body
    Then chunk 1 lands in channel as parent (thread_ts=None)
    And chunks 2-3 land as thread replies (thread_ts=<parent_ts>)
    And each chunk logged separately

  Scenario: Slack mrkdwn link — apostrophes and angle brackets safe
    Given title contains "Genia Xasis <> Humanoid (don't forget)"
    When HTML→mrkdwn conversion runs
    Then `<>` inside link label is replaced with `‹›` (visually similar, link-safe)
    And `&#x27;` / `&#39;` / `&apos;` are decoded to `'`
    And the link renders as clickable in Slack

  Scenario: Telegram DM to admins
    Given TELEGRAM_ADMIN_USER_IDS = "222968032,700469400"
    When _send_short_summary runs
    Then for each admin uid that started the bot, the body is sent as DM
    And bodies > 4096 chars are split at paragraph boundaries
    And each chunk has parse_mode=HTML (first line clickable as link)

  Scenario: Telegram DM fails for non-/start-ed user
    Given uid 412243973 has not /start-ed the bot
    When sendMessage is called for that uid
    Then 400 "chat not found" is returned
    And telegram_card_dm_failed is logged at info level
    And the pipeline continues for other recipients

  Scenario: Google Doc creation in Shared Drive folder
    Given FIREFLIES_DOCS_FOLDER_ID points to a Shared Drive (driveId is set)
    When _step_doc_export runs
    Then drive.files().create(body={mimeType=document, parents=[folder]}, supportsAllDrives=true)
      creates a new doc owned by the Shared Drive
    And row.google_doc_url is set
    And the doc URL appears as the first-line link in short_summary

  Scenario: Google Doc creation fails — pipeline continues
    Given FIREFLIES_DOCS_FOLDER_ID points to a regular My Drive folder (no Shared Drive)
    When drive.files().create runs
    Then API returns 403 "storageQuotaExceeded" (SA cannot own files in My Drive)
    And docs_export_failed is logged
    And row.google_doc_url stays NULL
    And short_summary first line is plain text (no link wrap), Slack/TG still post

  Scenario: Webhook POST to n8n
    Given MEETING_WEBHOOK_URL is set
    And short_summary is non-empty
    When _send_short_summary completes Slack/TG and triggers webhook
    Then POST to URL with Content-Type: application/json
    And body contains: source, source_id, title, meeting_date, duration_seconds,
      short_summary, detailed_summary, google_doc_url, participants, tasks_count
    And response 2xx → meeting_webhook_posted ok=True is logged
    And response non-2xx → meeting_webhook_http_error is logged (no retry)

  Scenario: Webhook URL not set — silently skipped
    Given MEETING_WEBHOOK_URL is empty
    When pipeline reaches webhook step
    Then post_meeting_to_webhook returns False immediately
    And no log noise

  Scenario: Slack mirror skipped if short_summary empty
    Given short_summary is None or empty (e.g. caught by L5 guard)
    When _send_short_summary runs
    Then NO Slack post (gating: `channel and token and short_summary.strip()`)
    And NO TG DM
    And NO webhook

  Scenario: External BI consumer reads via DB view
    Given user `zoom_colleague` has SELECT grant on `meeting_summaries_published`
    When user queries: SELECT * FROM meeting_summaries_published WHERE source='zoom'
    Then returns only rows with non-empty short_summary
    And user CANNOT modify any data (read-only role)
