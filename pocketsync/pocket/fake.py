"""In-memory :class:`PocketSource` for tests: pagination, 429s, edits, deletes and merges."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.model import Recording
from pocketsync.pocket.base import MAX_PAGE_LIMIT, AudioNotFound, RateLimited, RecordingPage


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakePocketSource:
    """Seeded, deterministic Pocket stand-in. Not thread-safe."""

    def __init__(self, recordings: list[Recording] | None = None) -> None:
        self._recs: dict[str, Recording] = {}
        self.deleted: set[str] = set()
        self.merged: dict[str, str] = {}  # src id -> dst id
        self.calls: list[str] = []
        self.no_audio: set[str] = set()  # ids Pocket has no audio file for (404)
        self._rate_limit_calls = 0
        self._retry_after: float | None = None
        self._clock = 0
        self.seed(recordings or [])

    def seed(self, recordings: list[Recording]) -> None:
        for rec in recordings:
            self._recs[rec.id] = copy.deepcopy(rec)
            self.deleted.discard(rec.id)

    def set_rate_limit(self, n_calls: int, retry_after: float | None = None) -> None:
        """Make the next ``n_calls`` API calls raise :class:`RateLimited`."""
        self._rate_limit_calls = n_calls
        self._retry_after = retry_after

    def edit(self, recording_id: str, **fields: object) -> Recording:
        """Change fields on a recording and bump ``updated_at``."""
        rec = self._require(recording_id)
        for key, value in fields.items():
            if not hasattr(rec, key):
                raise AttributeError(key)
            setattr(rec, key, value)
        if "updated_at" not in fields:
            self._clock += 1
            rec.updated_at = _now() if rec.updated_at is None else self._bump(rec.updated_at)
        return copy.deepcopy(rec)

    def _bump(self, prev: str) -> str:
        now = _now()
        return now if now > prev else f"{prev}~{self._clock}"

    def delete(self, recording_id: str) -> None:
        self._require(recording_id)
        del self._recs[recording_id]
        self.deleted.add(recording_id)

    def merge(self, src: str, dst: str) -> None:
        """Merge ``src`` into ``dst``: ``src`` disappears and is recorded in ``merged``."""
        self._require(src)
        self._require(dst)
        del self._recs[src]
        self.merged[src] = dst
        self.edit(dst)

    def _require(self, recording_id: str) -> Recording:
        if recording_id not in self._recs:
            raise CliError(EXIT_USER_ERROR, "Pocket resource not found", "Check the recording id.")
        return self._recs[recording_id]

    def _tick(self, name: str) -> None:
        self.calls.append(name)
        if self._rate_limit_calls > 0:
            self._rate_limit_calls -= 1
            raise RateLimited(self._retry_after)

    def list_recordings(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> RecordingPage:
        self._tick("list_recordings")
        if not 1 <= limit <= MAX_PAGE_LIMIT or page < 1:
            raise CliError(EXIT_USER_ERROR, "bad page/limit", "Use page>=1, 1<=limit<=100.")
        items = []
        for rec in self._recs.values():
            day = (rec.recording_at or rec.created_at or "")[:10]
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                continue
            items.append(rec)
        items.sort(key=lambda r: (r.recording_at or r.created_at or "", r.id))
        chunk = items[(page - 1) * limit : page * limit]
        return RecordingPage(
            recordings=[copy.deepcopy(r) for r in chunk],
            page=page,
            has_more=page * limit < len(items),
            total=len(items),
        )

    def get_recording(
        self,
        recording_id: str,
        include_transcript: bool = True,
        include_summarizations: bool = True,
    ) -> Recording:
        self._tick("get_recording")
        rec = copy.deepcopy(self._require(recording_id))
        if not include_transcript:
            rec.segments = []
        if not include_summarizations:
            rec.summary = ""
            rec.action_items = []
        return rec

    def get_audio_url(self, recording_id: str, expires_in: int = 3600) -> str:
        self._tick("get_audio_url")
        self._require(recording_id)
        if not 60 <= expires_in <= 86400:
            raise CliError(EXIT_USER_ERROR, "expires_in must be 60..86400", "Adjust expires_in.")
        if recording_id in self.no_audio:
            raise AudioNotFound("recording audio file not found")
        return f"http://localhost/audio/{recording_id}?expires_in={expires_in}"
