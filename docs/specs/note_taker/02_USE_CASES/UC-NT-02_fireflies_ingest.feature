Feature: UC-NT-02 — Fireflies transcript ingestion
  Implements: US-NT-2, US-NT-7, US-NT-8, US-NT-9
  Covers requirements: FR-NT-1.3, FR-NT-1.4, FR-NT-2.3, FR-NT-4.1, FR-NT-6.1,
                       FR-NT-6.2, FR-NT-6.3, FR-NT-7.1, NFR-NT-P.1, NFR-NT-R.1
  Tested by: T-NT-003, T-NT-004, T-NT-018, T-NT-019, T-NT-020, T-NT-021

  Background:
    Given Fireflies API token is configured
    And listener is running with FIREFLIES_REALTIME_ENABLED=true
    And FIREFLIES_POLL_INTERVAL_SECONDS=60
    And FIREFLIES_POLL_BATCH_SIZE=50

  Scenario: New Fireflies transcript detected
    Given a Fireflies meeting "06/05 - Bain Capital" finished 30 minutes ago
    And Fireflies has finished transcription
    When listener polls Fireflies GraphQL on the next tick
    Then the new fireflies_id is detected as "not in DB"
    And listener_fireflies_poll_recording_start is logged
    And the pipeline starts and completes

  Scenario: Fireflies-provided transcript used (skip Whisper)
    Given a Fireflies row with transcript_url set
    When _step_transcribe runs
    Then Fireflies' own transcript is downloaded and stored in row.transcript_text
    And Whisper API is NOT called (cheaper, faster)

  Scenario: Calendar match success — title rewritten
    Given a Fireflies row with auto-stamp title "May 06, 02:33 PM"
    And meeting_date matches a Google Calendar event titled "Bain Capital <> Humanoid | Intro call" within ±30 min
    When _step_match_calendar_title runs
    Then row.title is updated to "06/05 - Bain Capital <> Humanoid | Intro call"
    And fireflies_calendar_match_title_updated is logged
    And update_transcript_title is called on Fireflies API
    And the new title appears in Fireflies UI (success=True)

  Scenario: Calendar no match — LLM-derived title used
    Given a Fireflies row with auto-stamp title "May 07, 01:48 PM"
    And no calendar event in ±30 min window
    When _derive_topic_title runs as fallback
    Then LLM derives a topic from transcript (e.g. "Термшит и условия инвестраунда")
    And row.title is set to the derived topic
    And calendar_match_no_match is logged

  Scenario: Push to Fireflies UI fails
    Given _step_match_calendar_title successfully updates row.title locally
    When update_transcript_title is called and Fireflies API returns success=false
    Then fireflies_update_title_returned_unsuccessful is logged with the response message
    And local DB title remains updated (downstream still uses canonical)
    And the rest of the pipeline continues uninterrupted

  Scenario: Fireflies sentences fallback when Whisper hallucinates
    Given Whisper transcribed audio but text contains subtitle credits markers
    When looks_like_whisper_hallucination returns True
    Then pipeline falls back to Fireflies-provided transcript
    And fireflies_whisper_fallback_to_sentences is logged
    And row.transcript_text is replaced with the Fireflies version
