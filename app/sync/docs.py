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
        """Create an empty Google Doc in the SA's own Drive.
        Used when no `parent_folder_id` is configured.

        WARNING: service accounts in non-Workspace projects have
        ZERO storage quota of their own, so this path will 403
        with «caller does not have permission». Set
        `FIREFLIES_DOCS_FOLDER_ID` to a folder shared with the
        SA — `_create_doc_in_folder` works around the quota
        problem by creating the doc inside that folder
        directly.
        """
        return self._docs.documents().create(body={"title": title}).execute()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _create_doc_in_folder(
        self, *, title: str, parent_folder_id: str
    ) -> dict[str, Any]:
        """FR-CR-05-55/56 — create the Google Doc directly
        inside `parent_folder_id` via the Drive API. Two
        scenarios this is correct for:

        - **Workspace + Shared Drive (Team Drive)**: the folder
          lives in a Shared Drive. The created file is owned by
          the SHARED DRIVE, not the SA — pooled storage, no
          per-SA quota. `supportsAllDrives=True` is mandatory
          for any Drive call that touches Shared Drive content.
        - **Workspace + DWD impersonation**: SA acts as a real
          user. The doc is owned by that user.

        Personal Google accounts WITHOUT Workspace will 403
        with `storageQuotaExceeded` here regardless — service
        accounts in personal-account contexts have zero Drive
        quota and there's no «pooled» drive to use. Operator
        should create a Shared Drive in their Workspace,
        create the folder there, and share it with the SA as
        Editor.
        """
        file = (
            self._drive.files()
            .create(
                body={
                    "name": title or "Meeting summary",
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": [parent_folder_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {"documentId": file["id"]}

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
        # `supportsAllDrives=True` so the call works for files
        # that live in a Shared Drive.
        meta = (
            self._drive.files()
            .get(
                fileId=doc_id,
                fields="parents",
                supportsAllDrives=True,
            )
            .execute()
        )
        prev = ",".join(meta.get("parents") or [])
        self._drive.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents=prev,
            fields="id, parents",
            supportsAllDrives=True,
        ).execute()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _share_anyone_with_link(self, doc_id: str, *, role: str) -> None:
        """FR-CR-05-59 — make the doc readable / editable by
        anyone with the link. The Fireflies short summary in
        Telegram carries the doc URL, and the operator wants
        teammates to be able to open it without a per-person
        share dance.

        `role` is `'reader'` / `'writer'` / `'commenter'`.
        Default the caller passes is `'writer'` so the doc is
        fully editable in place — Telegram users tap the link
        and can immediately fix typos / annotate.
        """
        self._drive.permissions().create(
            fileId=doc_id,
            body={"type": "anyone", "role": role},
            supportsAllDrives=True,
            sendNotificationEmail=False,
        ).execute()

    def export_summary(
        self,
        *,
        title: str,
        body: str,
        parent_folder_id: str = "",
        share_role: str | None = "writer",
    ) -> tuple[str, str]:
        """Create a doc with `title`, write `body`, optionally
        in the configured Drive folder, and (by default) share
        it as anyone-with-link **writer**. Returns
        ``(doc_id, share_url)``.

        FR-CR-05-55 — when `parent_folder_id` is provided, the
        doc is created INSIDE that folder via the Drive API,
        not in the SA's Drive. This is the only path that works
        for service accounts in non-Workspace projects (they
        have no storage quota of their own and `documents.
        create` 403s with «caller does not have permission»).

        FR-CR-05-59 — `share_role` defaults to `'writer'` so
        the doc is openly editable for everyone the URL is
        shared with. Pass None to skip sharing (doc keeps the
        Shared Drive's default permissions).

        Share URL format is the standard
        ``https://docs.google.com/document/d/<id>/edit``."""
        if parent_folder_id:
            doc = self._create_doc_in_folder(
                title=title or "Meeting summary",
                parent_folder_id=parent_folder_id,
            )
        else:
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
        if share_role:
            try:
                self._share_anyone_with_link(doc_id, role=share_role)
            except HttpError as e:
                log.warning(
                    "docs_share_anyone_with_link_failed",
                    doc_id=doc_id,
                    role=share_role,
                    error=str(e),
                )
        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return doc_id, url


__all__ = ["DocsExportService", "GOOGLE_SCOPES_DOCS"]
