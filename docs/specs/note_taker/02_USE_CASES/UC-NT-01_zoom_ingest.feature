Feature: UC-NT-01 — Zoom recording ingestion
  Implements: US-NT-1, US-NT-8, US-NT-9
  Covers requirements: FR-NT-1.1, FR-NT-1.2, FR-NT-2.1, FR-NT-2.2, FR-NT-4.1,
                       FR-NT-7.1, NFR-NT-P.1, NFR-NT-R.1
  Tested by: T-NT-001, T-NT-002, T-NT-020, T-NT-021

  Background:
    Given Zoom S2S OAuth credentials are configured (account_id, client_id, client_secret)
    And listener is running with ZOOM_REALTIME_ENABLED=true
    And ZOOM_POLL_INTERVAL_SECONDS=60
    And ZOOM_POLL_BATCH_SIZE=50

  Scenario: New Zoom recording detected and processed end-to-end
    Given a Zoom meeting "Strategic Investors" finished 10 minutes ago
    And Zoom has finished processing the cloud recording (audio_url is set)
    When listener polls Zoom API on the next tick
    Then the new zoom_id is detected as "not in DB"
    And listener_zoom_poll_recording_start event is logged
    And the pipeline starts: download → transcribe → ... → publish
    And after the pipeline completes, row.tasks_extracted = true and row.last_error IS NULL

  Scenario: Recording not yet ready (audio_url missing)
    Given a Zoom meeting "Daily standup" finished 1 minute ago
    And Zoom is still processing the recording (audio_url is empty)
    When listener polls and starts pipeline
    Then _step_download_audio sets row.last_error = "no audio_url on Zoom record"
    And row.tasks_extracted = false
    And on the next tick (60s later) the listener retries
    And eventually (when audio_url appears) the pipeline completes successfully

  Scenario: Audio download fails (network error)
    Given audio_url is set but download returns 500 error
    When _step_download_audio runs
    Then row.last_error = "audio download failed or exceeded cap"
    And on the next tick the download is retried
    And after 3 consecutive failures, the row is left in error state for ops review

  Scenario: Recording outside polling window (older than last 50)
    Given a Zoom recording with date 14 days ago not in DB
    And there are 100+ newer recordings already in DB
    When listener polls last 50
    Then the old recording is NOT in the response
    And listener does not process it
    But operator can run `ops/migrate_zoom --zoom-id <uuid>` to manually trigger

  Scenario: Required-email filter excludes recordings
    Given ZOOM_REQUIRED_EMAIL is set to "artem@thehumanoid.ai"
    And a recording exists where Artem is neither host nor participant
    When listener polls
    Then the recording is filtered out from `list_recordings` results
    And it is never added to DB

  Scenario: Participants extracted via LLM
    Given a Zoom recording with transcript_text >800 chars
    And team_members table contains: ["Irina Shipilova", "Alina Kolpakova", "Артем Соколов"]
    When _kickoff_team_participants_async runs in parallel with _step_detailed_summary
    Then LLM returns the team participants who actually spoke or were addressed
    And row.participants holds canonical real_names + external email participants

  Scenario: Counterparties resolved
    Given a Zoom recording with transcript mentioning "Bosch", "Schaeffler", "Vista Equity Partners"
    When _step_match_counterparties runs
    Then mentions are extracted via LLM (single call, model=fireflies_tasks_model)
    And resolved against counterparties table in 5 parallel batches × 20 mentions
    And matched mentions create rows in counterparty_mentions
    And unresolved mentions trigger enrollment widget for admin
