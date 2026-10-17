"""Handoff events: the ``events.jsonl`` outbox and the events-cli publisher.

The outbox is a public contract that culture-rules workflows build against.
It is an append-only JSON Lines file at ``<files root>/events.jsonl``. One line
is appended per new ``(recording_id, content_hash)`` pair, after enrichment
(plus the occasional ``reclassified`` line, below).

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

Reclassified events. A re-enrichment that changes a recording's labels while its
``content_hash`` stays the same (a taxonomy change, or an operator ``classify``
requeue) appends one ``pocketsync.recording.reclassified`` line. It uses the same
envelope minus ``change``; ``enrichment`` adds the labels before the change, the
requeue generation and a digest of the new labels::

    {
      "event_id": "<uuid4>",
      "type": "pocketsync.recording.reclassified",
      "recording_id": "<pocket recording id>",
      "content_hash": "<unchanged content hash>",
      "sinks": {"mongo": {...}, "files": {...}},            # as above
      "enrichment": {"labels": ["<new label>"], "previous_labels": ["<old label>"],
                     "taxonomy_version": "...", "model": "...",
                     "generation": <int>, "labels_digest": "<16 hex>"},
      "emitted_at": "<RFC 3339 UTC timestamp>"
    }

It is deduped on ``(recording_id, content_hash, generation, taxonomy_version,
labels_digest)``, so a retried write never publishes twice, while a later requeue
or taxonomy change that lands on a different label set does. ``labels_digest`` is
order-insensitive (see :func:`labels_digest`). The ``enriched`` event and its
dedupe are unchanged: one per ``(recording_id, content_hash)``.

Publishing is separate from appending. :class:`OutboxPublisher` drains
unpublished lines through ``events_cli.EventClient.publish_event`` at QoS 1.
QoS 0 would bypass durable capture, so it is never used. Progress lives in a
sidecar, ``events.jsonl.published``, holding the byte offset of the first
unpublished line. It is replaced atomically (temp + ``os.replace``) only after
a successful publish. A failed publish stops the drain and leaves the rest
pending for the next drain. Delivery is at-least-once; consumers dedupe on the
envelope ``id``, which equals ``event_id``.

Delivery guarantees and limits. Delivery is at-least-once: a crash between a
broker publish and the sidecar update re-publishes that event on the next
drain, so consumers must dedupe on the envelope ``id``. The outbox is
single-writer: two processes appending at once could both pass the dedupe
check, so run one sync writer per files root.

Before publishing, the publisher waits (bounded, ``connect_timeout`` seconds,
default 5) for the client to report ``is_connected``, because ``EventClient``
connects asynchronously and a publish issued straight after construction
reports ``no_conn``. A timeout counts as broker-down: everything stays pending
and ``DrainSummary.error`` is set. Each publish also passes ``wait`` so the
broker's acknowledgement is confirmed before the offset advances.

``events-cli`` is an optional extra (``pocketsync[events]``) and is imported
lazily, only when a publish is attempted without an injected client.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

EVENT_TYPE = "pocketsync.recording.enriched"
RECLASSIFIED_EVENT_TYPE = "pocketsync.recording.reclassified"
OUTBOX_NAME = "events.jsonl"
PUBLISHED_SUFFIX = ".published"
SOURCE = "agent://pocketsync"  # events-cli requires an absolute URI
QOS = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def labels_digest(labels: Any) -> str:
    """Order-insensitive 16-hex digest of a label set (part of the reclassified dedupe key)."""
    blob = json.dumps(sorted(set(labels or [])), separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
        self._seen_reclassified: set[tuple[Any, ...]] = set()
        self._ids: set[str] = set()
        for _, _, ev in _read_lines(self.path):
            if ev.get("type") == RECLASSIFIED_EVENT_TYPE:
                enr = ev.get("enrichment") or {}
                self._seen_reclassified.add(
                    (
                        ev.get("recording_id"),
                        ev.get("content_hash"),
                        enr.get("generation"),
                        enr.get("taxonomy_version"),
                        enr.get("labels_digest"),
                    )
                )
            else:
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
        self._write(event)
        self._seen.add(key)
        self._ids.add(recording_id)
        return event

    def append_reclassified(
        self,
        recording_id: str,
        content_hash: str,
        *,
        labels: list[str],
        previous_labels: list[str] | None,
        generation: int,
        mongo: Mapping[str, str] | None = None,
        files: Mapping[str, str] | None = None,
        taxonomy_version: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one reclassified event; ``None`` if this exact label change was emitted."""
        digest = labels_digest(labels)
        key = (recording_id, content_hash, int(generation), taxonomy_version, digest)
        if key in self._seen_reclassified:
            return None
        event = {
            "event_id": str(uuid.uuid4()),
            "type": RECLASSIFIED_EVENT_TYPE,
            "recording_id": recording_id,
            "content_hash": content_hash,
            "sinks": {"mongo": dict(mongo or {}), "files": dict(files or {})},
            "enrichment": {
                "labels": list(labels),
                "previous_labels": list(previous_labels or []),
                "taxonomy_version": taxonomy_version,
                "model": model,
                "generation": int(generation),
                "labels_digest": digest,
            },
            "emitted_at": _now(),
        }
        self._write(event)
        self._seen_reclassified.add(key)
        self._ids.add(recording_id)
        return event

    def _write(self, event: Mapping[str, Any]) -> None:
        """One durable line: write + flush + fsync, healing a torn tail first."""
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
        *,
        connect_timeout: float = 5.0,
        delivery_wait: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(root) / OUTBOX_NAME
        self.offset_path = Path(str(self.path) + PUBLISHED_SUFFIX)
        self._client = client
        self._factory = envelope_factory
        self._connect_timeout = connect_timeout
        self._delivery_wait = delivery_wait
        self._clock = clock
        self._sleep = sleep

    def _await_connection(self, client: Any) -> bool:
        """Wait up to ``connect_timeout`` for ``client.is_connected``."""
        if not hasattr(client, "is_connected"):
            return True
        deadline = self._clock() + self._connect_timeout
        while not client.is_connected:
            if self._clock() >= deadline:
                return False
            self._sleep(0.02)
        return True

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
            if not self._await_connection(client):
                raise TimeoutError(f"broker not connected after {self._connect_timeout}s")
            factory = self._factory or _default_envelope
            for _, end, event in todo:
                envelope, topic = factory(event)
                result = client.publish_event(envelope, topic, qos=QOS, wait=self._delivery_wait)
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
