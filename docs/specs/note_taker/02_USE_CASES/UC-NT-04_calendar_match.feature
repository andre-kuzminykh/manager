Feature: UC-NT-04 — Calendar match + title canonicalization (Fireflies only)
  Implements: US-NT-7
  Covers requirements: FR-NT-6.1, FR-NT-6.2, FR-NT-6.3, NFR-NT-I.2
  Tested by: T-NT-018, T-NT-019

  Background:
    Given GOOGLE_CALENDAR_CLIENT_ID/SECRET are configured
    And GOOGLE_CALENDAR_ID is comma-separated list of calendar IDs
    And CALENDAR_MATCH_ENABLED=true and CALENDAR_MATCH_WINDOW_MINUTES=30

  Scenario: Single calendar event matches in window
    Given Fireflies row.meeting_date = 2026-05-07T11:00 UTC
    And Google Calendar (primary) contains event 2026-05-07T11:05 with summary "Bain Capital Intro Call"
    When _step_match_calendar_title runs
    Then LLM picks "Bain Capital Intro Call" with high confidence
    And row.title is updated to "07/05 - Bain Capital Intro Call"

  Scenario: Multiple calendars searched
    Given GOOGLE_CALENDAR_ID = "primary,c_workspace1@calendar.google.com,c_workspace2@calendar.google.com"
    When _step_match_calendar_title runs
    Then events from all 3 calendars are merged (dedup by event id)
    And LLM evaluates the merged list
    And если хотя бы один calendar API failed — others continue, log warning

  Scenario: No event in window
    Given Fireflies row.meeting_date = 2026-05-07T01:00 UTC (night, no events)
    When _step_match_calendar_title runs
    Then no events found in any calendar
    And calendar_match_no_match is logged
    And the existing title is kept (LLM-derived OR Fireflies auto-stamp)

  Scenario: Multiple events in window — LLM picks best match
    Given window contains 3 events at the same time (concurrent meetings)
    When LLM evaluates participants/title relevance
    Then LLM picks the one whose participants overlap with row.participants
    And confidence > 0.5 required to apply

  Scenario: Push to Fireflies UI succeeds
    Given calendar match found a new title
    When update_transcript_title is called
    And Fireflies API returns {success: true}
    Then fireflies_title_pushed_to_remote success=True is logged
    And the title in Fireflies UI is updated

  Scenario: Push to Fireflies UI fails (read-only token)
    Given calendar match found a new title
    When update_transcript_title is called
    And Fireflies API returns {success: false, message: "Unauthorized: write scope required"}
    Then fireflies_update_title_returned_unsuccessful is logged with message
    And row.title in our DB stays canonical
    And Slack/TG/webhook publish using canonical title regardless

  Scenario: Title already starts with DD/MM
    Given Fireflies row.title is already "07/05 - Bruno Vanhoorickx"
    When _force_meeting_title_first_line evaluates
    Then it does NOT prepend DD/MM (skips because it's already there)
    And final first line of short_summary stays "07/05 - Bruno Vanhoorickx"
