"""Landing engine: one canonical path from a Pocket recording id to files plus a Mongo doc.

``land`` is the only function that turns "recording X changed" into stored data. The webhook
handler and the reconciler both call it with a bare recording id, so the resulting documents
are identical regardless of which path noticed the change. It never talks to the enrichment
gateway: a content change only leaves ``enrichment.status = pending`` for the worker.

Signature choice: ``land(recording_id, source, *, files_sink, store_sink, ...)`` -- the two
sinks are explicit (files first, because the store records the file refs the files sink
returns). Fan-out to further sinks is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pocketsync.cli._errors import CliError
from pocketsync.model import Recording
from pocketsync.pocket.base import PocketSource
from pocketsync.sinks.base import AudioDownloadError, PutResult, Sink

AUDIO_URL_TTL = 3600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LandResult:
    """Outcome of one landing. ``planned`` is only populated for a dry run."""

    recording_id: str
    content_hash: str
    files_changed: bool = False
    store_changed: bool = False
    audio_missing: bool = False
    audio_error: str | None = None
    dry_run: bool = False
    planned: list[str] = field(default_factory=list)


def land(
    recording_id: str,
    source: PocketSource,
    *,
    files_sink: Sink,
    store_sink: Sink,
    dry_run: bool = False,
    with_audio: bool = True,
) -> LandResult:
    """Fetch ``recording_id`` canonically and land it in both sinks.

    Pocket errors fetching the recording propagate (nothing is written). A failure fetching
    the audio URL or downloading the audio does not: text lands, ``audio_missing`` /
    ``audio_error`` are set on the result, and the next landing retries the audio (the files
    sink reports ``has_audio``, which the reconciler checks). A signed audio URL is requested
    only when the files sink has no audio yet, since audio never changes for a recording.
    ``with_audio=False`` lands text only. In dry-run mode nothing is written and no audio URL
    is requested; ``planned`` describes the writes that would happen.
    """
    rec = source.get_recording(recording_id, include_transcript=True, include_summarizations=True)

    audio_url: str | None = None
    audio_error: str | None = None
    if with_audio and not dry_run:
        audio_url, audio_error = _audio_url_if_missing(source, files_sink, recording_id, rec.id)

    f, download_error = _put_files(files_sink, rec, dry_run, audio_url)
    audio_error = download_error or audio_error
    # Hand the store the file refs. When nothing changed locally the stored refs are already
    # right (and an unchanged local result omits the meta ref), so only pass them if the store
    # has never seen this recording.
    pass_files = f.changed or store_sink.get_state(rec.id) is None
    s = store_sink.put_recording(rec, f.files if pass_files else None, dry_run=dry_run)

    result = LandResult(
        recording_id=rec.id,
        content_hash=s.content_hash,
        files_changed=f.changed,
        store_changed=s.changed,
        audio_missing=audio_error is not None,
        audio_error=audio_error,
        dry_run=dry_run,
    )
    if dry_run:
        if f.changed:
            result.planned.append(f"files: write {rec.id} text files + fetch audio")
        if s.changed:
            result.planned.append(f"store: upsert {rec.id} (hash {s.content_hash[:12]})")
    return result


def _audio_url_if_missing(
    source: PocketSource, files_sink: Sink, recording_id: str, rec_id: str
) -> tuple[str | None, str | None]:
    """A fresh signed audio URL when the files sink has no audio yet: ``(url, error)``."""
    fstate = files_sink.get_state(rec_id)
    if fstate and fstate.has_audio:
        return None, None
    try:
        return source.get_audio_url(recording_id, expires_in=AUDIO_URL_TTL), None
    except CliError as exc:
        return None, exc.message


def _put_files(
    files_sink: Sink, rec: Recording, dry_run: bool, audio_url: str | None
) -> tuple[PutResult, str | None]:
    """Write to the files sink; a failed audio download lands text only: ``(result, error)``."""
    try:
        return files_sink.put_recording(rec, dry_run=dry_run, audio_url=audio_url), None
    except AudioDownloadError as exc:
        return files_sink.put_recording(rec, dry_run=dry_run, audio_url=None), exc.message


def _apply(sink: Sink, recording_id: str, action) -> str:
    """Run ``action(sink)`` if the sink knows the id; unknown ids are reported, not errors."""
    if sink.get_state(recording_id) is None:
        return "unknown"
    action(sink)
    return "applied"


def handle_deleted(
    recording_id: str, *, files_sink: Sink, store_sink: Sink, now: str | None = None
) -> dict[str, str]:
    """Tombstone ``recording_id`` in both sinks. Never removes files or docs.

    Returns ``{"files": "applied"|"unknown", "store": ...}``. A delete for a recording we
    never landed is not an error -- it is reported as ``"unknown"``.
    """
    when = now or _now()
    return {
        "files": _apply(files_sink, recording_id, lambda s: s.tombstone(recording_id, when)),
        "store": _apply(store_sink, recording_id, lambda s: s.tombstone(recording_id, when)),
    }


def handle_merged(
    src_id: str, dst_id: str, *, files_sink: Sink, store_sink: Sink, now: str | None = None
) -> dict[str, str]:
    """Mark ``src_id`` as merged into ``dst_id`` in both sinks. Never removes data.

    Same ``"applied"`` / ``"unknown"`` reporting as :func:`handle_deleted`.
    """
    when = now or _now()
    return {
        "files": _apply(files_sink, src_id, lambda s: s.mark_merged(src_id, dst_id, when)),
        "store": _apply(store_sink, src_id, lambda s: s.mark_merged(src_id, dst_id, when)),
    }
