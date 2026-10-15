"""Core data model: :class:`Recording` and its content hash.

Nothing outside the Pocket adapter may depend on Pocket's wire format; the
adapter maps API responses into these dataclasses. Pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Keys that change on every fetch and must never influence the content hash.
VOLATILE_KEYS = frozenset({"fetched_at"})
#: Top-level fields left out of the content hash (deviation d2). ``raw`` is the adapter's copy
#: of Pocket's wire response; the hash covers the mapped content fields (including
#: ``updated_at``), so wire-only changes do not re-embed or re-emit an event.
HASH_EXCLUDED_FIELDS = frozenset({"raw"})


@dataclass
class TranscriptSegment:
    """One transcript segment.

    ``start``/``end`` are offsets in seconds, ``text`` the spoken text,
    ``speaker`` Pocket's speaker id and ``speaker_name`` its display name.
    """

    start: float | None = None
    end: float | None = None
    text: str = ""
    speaker: str | None = None
    speaker_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "speaker_name": self.speaker_name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TranscriptSegment:
        return cls(
            start=d.get("start"),
            end=d.get("end"),
            text=d.get("text", ""),
            speaker=d.get("speaker"),
            speaker_name=d.get("speaker_name", d.get("speakerName")),
        )


@dataclass
class ActionItem:
    """An action item extracted by Pocket: ``text`` plus completion flag and optional owner."""

    text: str = ""
    done: bool = False
    owner: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "done": self.done, "owner": self.owner}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActionItem:
        return cls(text=d.get("text", ""), done=bool(d.get("done", False)), owner=d.get("owner"))


@dataclass
class Recording:
    """A Pocket recording with its transcript, summary and action items.

    Fields: ``id``; ``title``; ``description``; ISO-8601 timestamps
    ``created_at``/``updated_at``/``recording_at``; ``duration`` (seconds);
    ``language``; ``state`` (opaque Pocket string); ``folder_id``; ``tags``;
    ``speakers`` (display names); ``segments`` (transcript);
    ``summary`` (markdown); ``action_items``; ``raw`` (adapter-kept source
    metadata, hashed except for volatile keys); ``fetched_at`` (volatile,
    never hashed).
    """

    id: str
    title: str = ""
    description: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    recording_at: str | None = None
    duration: float | None = None
    language: str | None = None
    state: str | None = None
    folder_id: str | None = None
    tags: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    segments: list[TranscriptSegment] = field(default_factory=list)
    summary: str = ""
    action_items: list[ActionItem] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "recording_at": self.recording_at,
            "duration": self.duration,
            "language": self.language,
            "state": self.state,
            "folder_id": self.folder_id,
            "tags": list(self.tags),
            "speakers": list(self.speakers),
            "segments": [s.to_dict() for s in self.segments],
            "summary": self.summary,
            "action_items": [a.to_dict() for a in self.action_items],
            "raw": self.raw,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Recording:
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            description=d.get("description", ""),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            recording_at=d.get("recording_at"),
            duration=d.get("duration"),
            language=d.get("language"),
            state=d.get("state"),
            folder_id=d.get("folder_id"),
            tags=list(d.get("tags") or []),
            speakers=list(d.get("speakers") or []),
            segments=[TranscriptSegment.from_dict(s) for s in d.get("segments") or []],
            summary=d.get("summary", ""),
            action_items=[ActionItem.from_dict(a) for a in d.get("action_items") or []],
            raw=dict(d.get("raw") or {}),
            fetched_at=d.get("fetched_at"),
        )

    def content_hash(self) -> str:
        return content_hash(self)


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def canonical_json(rec: Recording) -> str:
    """Canonical JSON of ``rec`` minus volatile keys and ``raw``: sorted keys, compact."""
    content = {k: v for k, v in rec.to_dict().items() if k not in HASH_EXCLUDED_FIELDS}
    return json.dumps(
        _strip_volatile(content), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def content_hash(rec: Recording) -> str:
    """SHA-256 hex digest of the canonical form; stable across key order and fetch time."""
    return hashlib.sha256(canonical_json(rec).encode("utf-8")).hexdigest()
