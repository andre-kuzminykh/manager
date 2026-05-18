"""FR-CB2-1.4 — HTTP Events variant signature verification.

Socket Mode is the preferred transport; this HMAC helper is kept
so a future deployment can switch to publicly-hosted Slack Events
without rewriting validation.
"""
from __future__ import annotations

import hashlib
import hmac


def verify_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
) -> bool:
    """Return True iff ``X-Slack-Signature`` matches ``v0=…``
    over ``v0:<timestamp>:<body>``."""
    if not signing_secret or not timestamp or not signature:
        return False
    base = f"v0:{timestamp}:".encode("utf-8") + (body or b"")
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


__all__ = ["verify_signature"]
