"""FR-CB2-5.1/5.2 — CEO Brain Bot runner.

Lifecycle wrapper that decides which threads to spin up:

  * ``CEO_BRAIN_ENABLED=false``  → no threads.
  * ``CEO_BRAIN_ARCHIVE_ONLY=true`` (or no Anthropic key) → archive
    thread only.
  * Both flags positive + key present → archive + responder.

Sprint 1 ships the archive thread; the responder is a placeholder
that logs a warning until Sprint 2.
"""
from __future__ import annotations

import threading
from typing import Any

from app.config import Settings, get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


class CeoBrainRunner:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self.archive_thread: threading.Thread | None = None
        self.responder_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @classmethod
    def from_env(cls) -> "CeoBrainRunner":
        return cls(settings=get_settings())

    def start(self) -> None:
        s = self._settings
        if not s.ceo_brain_enabled:
            log.info(
                "ceo_brain_disabled_by_env",
                hint="set CEO_BRAIN_ENABLED=true",
            )
            return
        # FR-CB2-2.x — archive thread.
        self._start_archive_thread()

        if s.ceo_brain_archive_only:
            log.info(
                "ceo_brain_archive_only_mode",
                hint="responder disabled by CEO_BRAIN_ARCHIVE_ONLY=true",
            )
            return

        # FR-CB2-5.3 — responder requires an Anthropic key.
        if not s.ceo_brain_anthropic_api_key:
            log.warning(
                "ceo_brain_responder_no_anthropic_key",
                hint=(
                    "set CEO_BRAIN_ANTHROPIC_API_KEY to enable the "
                    "Claude responder; archive thread runs alone"
                ),
            )
            return

        self._start_responder_thread()

    def stop(self) -> None:
        self._stop_event.set()

    def _start_archive_thread(self) -> None:
        from app.ceo_brain.socket_client import open_socket_connection

        def _loop() -> None:
            log.info("ceo_brain_archive_thread_starting")
            client = open_socket_connection()
            if client is None:
                log.warning(
                    "ceo_brain_socket_unavailable",
                    hint=(
                        "CEO_BRAIN_SLACK_APP_TOKEN / "
                        "CEO_BRAIN_SLACK_BOT_TOKEN unset or "
                        "slack_sdk missing — archive thread idles"
                    ),
                )
            # The actual event-pump runs inside slack_sdk.socket_mode;
            # here we just block until stop_event.
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=5)

        t = threading.Thread(
            target=_loop, name="ceo-brain-archive", daemon=True,
        )
        t.start()
        self.archive_thread = t

    def _start_responder_thread(self) -> None:
        # Sprint 2 will populate this with the real Claude bridge.
        # For Sprint 1 we just spawn an idle thread so callers can
        # detect the responder is "on" via `responder_thread`.
        def _loop() -> None:
            log.info(
                "ceo_brain_responder_thread_placeholder",
                hint="Sprint 2 wires real responder",
            )
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=5)

        t = threading.Thread(
            target=_loop, name="ceo-brain-responder", daemon=True,
        )
        t.start()
        self.responder_thread = t


__all__ = ["CeoBrainRunner"]
