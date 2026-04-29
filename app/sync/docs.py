"""FR-CR-05-43 — Google Docs export for meeting summaries.

Creates a new Google Doc, dumps the detailed-summary text into
its body, returns ``(doc_id, doc_url)``. Optionally moves the
new doc into a configured Drive folder.

Same service-account credentials as the Sheets sync — just
needs an extra OAuth scope (``documents`` and ``drive`` for the
parent-folder move). The ``factories`` module wires it up.
"""
from __future__ import annotations

from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger

log = get_logger(__name__)

GOOGLE_SCOPES_DOCS = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


class DocsExportService:
    """Synchronous Google Docs writer.

    `export_summary(title, body, parent_folder_id=...)` creates
    a new doc, writes the body, returns the doc id + share URL.

    Failures: every step is retried on transient HttpError
    (network, 5xx). 4xx propagates to the caller — usually means
    the service account is missing the right scope or doesn't
    have permission to write to the parent folder.
    """

    def __init__(self, *, credentials: Any) -> None:
        self._docs = build(
            "docs", "v1", credentials=credentials, cache_discovery=False
        )
        self._drive = build(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _create_doc(self, title: str) -> dict[str, Any]:
        return self._docs.documents().create(body={"title": title}).execute()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _insert_text(self, doc_id: str, text: str) -> None:
        # Single batchUpdate that inserts the entire body at
        # index 1 (right after the doc-start marker).
        self._docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": text,
                        }
                    }
                ]
            },
        ).execute()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _move_to_folder(self, doc_id: str, folder_id: str) -> None:
        # Find the doc's current parents to remove them, then add
        # the configured folder as the new parent.
        meta = (
            self._drive.files()
            .get(fileId=doc_id, fields="parents")
            .execute()
        )
        prev = ",".join(meta.get("parents") or [])
        self._drive.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents=prev,
            fields="id, parents",
        ).execute()

    def export_summary(
        self,
        *,
        title: str,
        body: str,
        parent_folder_id: str = "",
    ) -> tuple[str, str]:
        """Create a doc with `title`, write `body`, optionally
        move into the configured Drive folder. Returns
        ``(doc_id, share_url)``.

        Share URL format is the standard
        ``https://docs.google.com/document/d/<id>/edit``."""
        doc = self._create_doc(title=title or "Meeting summary")
        doc_id = doc["documentId"]
        if body:
            try:
                self._insert_text(doc_id, body)
            except HttpError as e:
                log.warning(
                    "docs_insert_text_failed",
                    doc_id=doc_id,
                    error=str(e),
                )
        if parent_folder_id:
            try:
                self._move_to_folder(doc_id, parent_folder_id)
            except HttpError as e:
                log.warning(
                    "docs_move_to_folder_failed",
                    doc_id=doc_id,
                    folder=parent_folder_id,
                    error=str(e),
                )
        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return doc_id, url


__all__ = ["DocsExportService", "GOOGLE_SCOPES_DOCS"]
