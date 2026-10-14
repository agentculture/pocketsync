"""Handoff events: the ``events.jsonl`` outbox and the events-cli publisher.

The outbox is a public contract that culture-rules workflows build against.
It is an append-only JSON Lines file at ``<files root>/events.jsonl``. One line
is appended per new ``(recording_id, content_hash)`` pair, after enrichment.

Event schema (one JSON object per line)::

    {
      "event_id": "<uuid4>",
      "type": "pocketsync.recording.enriched",
      "recording_id": "<pocket recording id>",
      "content_hash": "<hash of the synced content>",
      "change": "created" | "updated",
      "sinks": {
        "mongo": {"collection": "<name>", "doc_id": "<id>"},
        "files": {"<name>": "<path relative to the files root>"}
      },
      "enrichment": {"labels": ["<type label>"], "taxonomy_version": "...",
                     "model": "..."},
      "emitted_at": "<RFC 3339 UTC timestamp>"
    }

``change`` is ``created`` the first time a recording id is emitted and
``updated`` afterwards. A repeated ``(recording_id, content_hash)`` is never
re-appended. Appends are one ``write`` + flush + fsync, so a crash leaves at
worst a torn final line, which is ignored on load and healed on next append.

Publishing is separate from appending. :class:`OutboxPublisher` drains
unpublished lines through ``events_cli.EventClient.publish_event`` at QoS 1.
QoS 0 would bypass durable capture, so it is never used. Progress lives in a
sidecar, ``events.jsonl.published``, holding the byte offset of the first
unpublished line. It is replaced atomically (temp + ``os.replace``) only after
a successful publish. A failed publish stops the drain and leaves the rest
pending for the next drain. Delivery is at-least-once; consumers dedupe on the
envelope ``id``, which equals ``event_id``.

``events-cli`` is an optional extra (``pocketsync[events]``) and is imported
lazily, only when a publish is attempted without an injected client.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

EVENT_TYPE = "pocketsync.recording.enriched"
OUTBOX_NAME = "events.jsonl"
PUBLISHED_SUFFIX = ".published"
SOURCE = "pocketsync"
QOS = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_lines(path: Path) -> list[tuple[int, int, dict[str, Any]]]:
    """Return ``(start, end, event)`` for each complete, parseable line."""
    if not path.exists():
        return []
    data = path.read_bytes()
    out: list[tuple[int, int, dict[str, Any]]] = []
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl == -1:
            break  # torn final line (crash mid-append)
        end = nl + 1
        try:
            obj = json.loads(data[pos:nl])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            out.append((pos, end, obj))
        pos = end
    return out


class Outbox:
    """Append-only, deduplicating ``events.jsonl`` writer."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.path = self.root / OUTBOX_NAME
        self._seen: set[tuple[str, str]] = set()
        self._ids: set[str] = set()
        for _, _, ev in _read_lines(self.path):
            self._seen.add((ev.get("recording_id"), ev.get("content_hash")))
            self._ids.add(ev.get("recording_id"))

    def append(
        self,
        recording_id: str,
        content_hash: str,
        *,
        mongo: Mapping[str, str] | None = None,
        files: Mapping[str, str] | None = None,
        labels: list[str] | None = None,
        taxonomy_version: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one event; return it, or ``None`` if ``(id, hash)`` was already emitted."""
        key = (recording_id, content_hash)
        if key in self._seen:
            return None
        event = {
            "event_id": str(uuid.uuid4()),
            "type": EVENT_TYPE,
            "recording_id": recording_id,
            "content_hash": content_hash,
            "change": "updated" if recording_id in self._ids else "created",
            "sinks": {"mongo": dict(mongo or {}), "files": dict(files or {})},
            "enrichment": {
                "labels": list(labels or []),
                "taxonomy_version": taxonomy_version,
                "model": model,
            },
            "emitted_at": _now(),
        }
        line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        # Heal a torn tail so the new line starts on its own line.
        if self.path.exists() and self.path.stat().st_size:
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = b"\n" + line
        with open(self.path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._seen.add(key)
        self._ids.add(recording_id)
        return event


@dataclass(frozen=True)
class DrainSummary:
    published: int
    pending: int
    error: str | None = None


def _default_client() -> Any:
    try:
        from events_cli.client import EventClient  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via sys.modules block
        raise RuntimeError(
            "events-cli is not installed; install the extra: pip install 'pocketsync[events]'"
        ) from exc
    return EventClient()


def _default_envelope(event: Mapping[str, Any]) -> tuple[Any, str]:
    from events_cli.core.envelope import Envelope  # noqa: PLC0415
    from events_cli.core.topics import type_to_topic  # noqa: PLC0415

    env = Envelope.new(event["type"], SOURCE, data=dict(event), id=event["event_id"])
    return env, type_to_topic(event["type"])


class OutboxPublisher:
    """Drain unpublished outbox lines to the broker at QoS 1."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        client: Any = None,
        envelope_factory: Callable[[Mapping[str, Any]], tuple[Any, str]] | None = None,
    ) -> None:
        self.path = Path(root) / OUTBOX_NAME
        self.offset_path = Path(str(self.path) + PUBLISHED_SUFFIX)
        self._client = client
        self._factory = envelope_factory

    def _offset(self) -> int:
        try:
            return int(self.offset_path.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _store_offset(self, offset: int) -> None:
        tmp = self.offset_path.with_name(self.offset_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(offset))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.offset_path)

    def drain(self) -> DrainSummary:
        offset = self._offset()
        todo = [(s, e, ev) for s, e, ev in _read_lines(self.path) if s >= offset]
        if not todo:
            return DrainSummary(0, 0)
        published = 0
        error: str | None = None
        client = self._client
        try:
            if client is None:
                client = _default_client()
            factory = self._factory or _default_envelope
            for _, end, event in todo:
                envelope, topic = factory(event)
                result = client.publish_event(envelope, topic, qos=QOS)
                if not getattr(result, "ok", False):
                    error = f"publish refused: {getattr(result, 'reason', 'unknown')}"
                    break
                self._store_offset(end)
                published += 1
        except Exception as exc:  # broker down / events-cli missing: leave pending
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if client is not None and self._client is None and hasattr(client, "close"):
                client.close()
        return DrainSummary(published, len(todo) - published, error)
