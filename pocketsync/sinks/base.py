"""The backend-neutral :class:`Sink` protocol shared by the local and Mongo sinks.

No backend-specific types appear here. Paths are always *relative* to the sink's own root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.model import Recording


@dataclass(frozen=True)
class FileRef:
    """A landed file: ``rel_path`` (relative to sink root), ``size`` in bytes, ``sha256`` hex."""

    rel_path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"rel_path": self.rel_path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileRef:
        return cls(rel_path=d["rel_path"], size=int(d["size"]), sha256=d["sha256"])


@dataclass
class FileSet:
    """File refs for one recording; any member may be ``None`` (not present / not fetched)."""

    transcript_json: FileRef | None = None
    transcript_md: FileRef | None = None
    summary_md: FileRef | None = None
    audio: FileRef | None = None
    meta: FileRef | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (v.to_dict() if v else None) for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileSet:
        return cls(**{k: FileRef.from_dict(v) if v else None for k, v in d.items() if k in _SLOTS})

    def with_refs(self, **refs: FileRef | None) -> FileSet:
        """A copy with the given slots replaced (unknown slot names raise ``TypeError``)."""
        data: dict[str, FileRef | None] = {name: getattr(self, name) for name in _SLOTS}
        data.update(refs)
        return FileSet(**data)


_SLOTS = ("transcript_json", "transcript_md", "summary_md", "audio", "meta")


@dataclass
class PutResult:
    """Outcome of :meth:`Sink.put_recording`.

    ``changed`` is False for a no-op (same content hash). ``locations`` maps logical names
    (``dir``, ``meta``, ...) to sink-relative locations; ``files`` carries refs when known.
    """

    changed: bool
    content_hash: str
    locations: dict[str, str] = field(default_factory=dict)
    files: FileSet = field(default_factory=FileSet)


class AudioDownloadError(CliError):
    """Streaming the audio bytes failed. Text can still land; a later landing retries."""

    def __init__(self, message: str) -> None:
        super().__init__(EXIT_ENV_ERROR, message, "Retry later; the next landing retries audio.")


@dataclass
class RecordState:
    """What a sink knows about a recording (for cursor/idempotence and tombstones)."""

    content_hash: str
    deleted_in_pocket: bool = False
    merged_into: str | None = None
    has_audio: bool = False
    audio_absent: bool = False  # Pocket said it has no audio file (until it changes the recording)


@runtime_checkable
class Sink(Protocol):
    """A destination for recordings. All writes are idempotent by ``content_hash``."""

    def put_recording(
        self,
        rec: Recording,
        files: FileSet | None = None,
        *,
        dry_run: bool = False,
        audio_url: str | None = None,
    ) -> PutResult:
        """Land ``rec``. Same content hash -> no-op (``changed=False``).

        ``files`` lets a caller hand over refs already landed elsewhere (backends that only
        record metadata use it; the local sink writes its own). ``audio_url`` is a pre-signed
        URL to stream audio from, for sinks that store audio bytes. ``dry_run`` writes nothing.
        """
        ...

    def get_state(self, recording_id: str) -> RecordState | None:
        """Return the stored state, or ``None`` if the recording was never landed."""
        ...

    def tombstone(self, recording_id: str, when: str) -> None:
        """Mark deleted-in-Pocket at ISO time ``when``. Never purges data."""
        ...

    def mark_merged(self, src_id: str, dst_id: str, when: str) -> None:
        """Mark ``src_id`` as merged into ``dst_id``. Never purges data."""
        ...
