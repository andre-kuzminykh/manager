Feature: UC-NT-03 — Quality gates (defensive layers)
  Implements: US-NT-6
  Covers requirements: FR-NT-3.1, FR-NT-3.2, FR-NT-3.3, FR-NT-3.4, FR-NT-3.5,
                       NFR-NT-U.1
  Tested by: T-NT-015, T-NT-016, T-NT-017

  Background:
    Given pipeline has 5 quality gates: L1 (file size), L2 (Whisper hallucination),
      L3 (thin transcript), L4 (empty detailed_summary), L5 (no-content summary)

  Scenario: L1 — Audio file too small / corrupt
    Given downloaded audio is 0 bytes or exceeds zoom_audio_max_bytes
    When _step_download_audio runs
    Then row.last_error = "audio download failed or exceeded cap"
    And pipeline stops, retry on next tick

  Scenario: L2 — Whisper hallucination detected (subtitle credits)
    Given Whisper output contains "Редактор субтитров" >= 3 times in first 1000 chars
    When looks_like_whisper_hallucination evaluates
    Then it returns True
    And pipeline falls back: Zoom→VTT, Fireflies→provided transcript
    And whisper_fallback_to_vtt or fireflies_whisper_fallback_to_sentences is logged

  Scenario: L2 — Whisper hallucination detected (low unique-word ratio)
    Given Whisper output has 100+ words but unique-word ratio < 8%
    When looks_like_whisper_hallucination evaluates
    Then it returns True (loop pattern detected)
    And fallback logic kicks in

  Scenario: L2 — Whisper hallucination detected (bigram loop)
    Given Whisper output has 50+ words and one 2-word phrase repeats >= 20 times
    When looks_like_whisper_hallucination evaluates
    Then it returns True
    And fallback logic kicks in

  Scenario: L3 — Transcript too thin to summarize
    Given row.transcript_text is < 800 chars
    When _step_detailed_summary starts and is_transcript_unsummarizable is called
    Then it returns (True, "transcript too short (XXX chars)")
    And row.tasks_extracted = true, row.last_error = NULL
    And pipeline stops; no detailed_summary, no Slack, no TG, no webhook
    And zoom_pipeline_skipped_thin_transcript is logged

  Scenario: L3 — Transcript dominated by subtitle credits
    Given row.transcript_text has 1500+ chars but ≥2 subtitle markers in first 2KB
    When is_transcript_unsummarizable is called
    Then it returns (True, "transcript dominated by subtitle credits (N hits)")
    And pipeline stops the same way

  Scenario: L4 — LLM returns empty detailed_summary
    Given _step_detailed_summary calls LLM and gets empty response
    When the result is checked
    Then row.last_error = "detailed summary returned empty"
    And pipeline stops, retry on next tick

  Scenario: L5 — LLM short_summary marked as "no content"
    Given _step_short_summary LLM output contains "содержательная часть отсутствует"
    When is_summary_no_content is called
    Then it returns (True, "содержательная часть отсутствует")
    And row.tasks_extracted = true, row.last_error = NULL
    And no Slack post, no TG card, no webhook fire
    And zoom_pipeline_skipped_no_content_summary is logged with the matched phrase

  Scenario: All gates pass for a normal meeting
    Given a 30-min Zoom meeting with 15 KB clean transcript
    When pipeline runs
    Then no quality gate triggers
    And the meeting publishes to Slack + TG + Google Doc + webhook
