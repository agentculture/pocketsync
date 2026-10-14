"""The read-only :class:`PocketSource` protocol and its adapter-level types.

Nothing outside ``pocketsync/pocket`` may see Pocket's wire JSON; implementations map it to
:class:`pocketsync.model.Recording`. The protocol deliberately has no write methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.model import Recording

MAX_PAGE_LIMIT = 100


class RateLimited(CliError):
    """HTTP 429 from Pocket. ``retry_after`` is seconds, when the server said so."""

    def __init__(self, retry_after: float | None = None, message: str = "") -> None:
        text = message or "Pocket rate limit hit"
        if retry_after is not None:
            text += f" (retry after {retry_after:g}s)"
        super().__init__(
            code=EXIT_ENV_ERROR,
            message=text,
            remediation="Wait and retry; lower the sync frequency.",
        )
        self.retry_after = retry_after


@dataclass
class RecordingPage:
    """One page of recordings plus pagination state."""

    recordings: list[Recording] = field(default_factory=list)
    page: int = 1
    has_more: bool = False
    total: int | None = None


@runtime_checkable
class PocketSource(Protocol):
    """Read-only view of a Pocket account."""

    def list_recordings(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> RecordingPage:
        """List recordings (dates are ``YYYY-MM-DD``; ``limit`` is at most 100)."""
        ...

    def get_recording(
        self,
        recording_id: str,
        include_transcript: bool = True,
        include_summarizations: bool = True,
    ) -> Recording:
        """Fetch one recording, optionally with transcript and summarizations."""
        ...

    def get_audio_url(self, recording_id: str, expires_in: int = 3600) -> str:
        """Return a pre-signed audio download URL valid for ``expires_in`` (60-86400) seconds."""
        ...
