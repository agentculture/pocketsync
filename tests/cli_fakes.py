"""Offline fakes for the CLI verb tests: a Runtime wired to fakes, plus write counters.

Everything here is synthetic. ``FakeRuntime`` replaces the network-facing pieces of
:class:`pocketsync.cli._runtime.Runtime` (Pocket, Mongo, gateway, broker) with
FakePocketSource, mongomock, a fake gateway and a fake events client.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Any

import mongomock

from pocketsync import config
from pocketsync.cli import _runtime
from pocketsync.enrich.gateway import EMBED_DIMS, Classification
from pocketsync.events import OutboxPublisher
from pocketsync.model import Recording, TranscriptSegment
from pocketsync.pocket.fake import FakePocketSource
from pocketsync.sinks.local import LocalSink
from pocketsync.sinks.mongo import MongoSink
from pocketsync.state import MongoStateStore

SECRET = "fake-webhook-secret"  # synthetic test value


def ts(days_ago: float = 0.0, minutes: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def rec(i: int, days_ago: float = 0.5, **kw: Any) -> Recording:
    at = ts(days_ago, minutes=i)
    base: dict[str, Any] = dict(
        id=f"r{i:04d}",
        title=f"Synthetic {i}",
        created_at=at,
        recording_at=at,
        updated_at=at,
        segments=[TranscriptSegment(start=0, end=1, text=f"hello {i}", speaker="A")],
        summary=f"Synthetic summary {i}.",
    )
    base.update(kw)
    return Recording(**base)


class AudioResp:
    headers = {"Content-Type": "audio/mpeg"}

    def __init__(self) -> None:
        self._io = io.BytesIO(b"synthetic-audio")

    def read(self, n: int = -1) -> bytes:
        return self._io.read(n)

    def __enter__(self) -> "AudioResp":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False


class FakeGateway:
    def __init__(self, tax: str = "tax-v1") -> None:
        self.tax = tax
        self.embeds = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embeds += 1
        return [[0.25] * EMBED_DIMS for _ in texts]

    def classify(self, text: str) -> Classification:
        return Classification(["meeting"], 0.9, {"meeting": 0.9}, {}, self.tax)


class FakeResult:
    ok = True
    reason = ""


class FakeEventsClient:
    is_connected = True

    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish_event(self, envelope: Any, topic: str, qos: int = 1, wait: float = 0) -> Any:
        self.published.append(envelope)
        return FakeResult()


class CountingSink:
    """Proxy counting every *committing* write (dry-run puts are not writes)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.writes = 0

    def put_recording(self, rec: Any, files: Any = None, *, dry_run: bool = False, **kw: Any):
        if not dry_run:
            self.writes += 1
        return self.inner.put_recording(rec, files, dry_run=dry_run, **kw)

    def tombstone(self, *a: Any, **kw: Any) -> Any:
        self.writes += 1
        return self.inner.tombstone(*a, **kw)

    def mark_merged(self, *a: Any, **kw: Any) -> Any:
        self.writes += 1
        return self.inner.mark_merged(*a, **kw)

    def set_enrichment(self, *a: Any, **kw: Any) -> Any:
        self.writes += 1
        return self.inner.set_enrichment(*a, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


_STATE_WRITES = (
    "set_watermark",
    "mark_seen",
    "record_run",
    "record_error",
    "increment",
    "set_last_webhook_at",
)


class CountingState:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.writes = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name in _STATE_WRITES:

            def wrapped(*a: Any, **kw: Any) -> Any:
                self.writes += 1
                return attr(*a, **kw)

            return wrapped
        return attr


class FakeRuntime(_runtime.Runtime):
    """Runtime whose network-facing collaborators are fakes."""

    def __init__(self, tmp_path: Any, recordings: list[Recording] | None = None, **env: str):
        values = {
            "POCKETSYNC_API_KEY": "pk_synthetic",  # synthetic
            "POCKETSYNC_MONGO_URI": "mongodb://localhost:27017",
            "POCKETSYNC_FILES_ROOT": str(tmp_path / "files"),
            "POCKETSYNC_WEBHOOK_SECRET": SECRET,
        }
        values.update(env)
        super().__init__(config.load(env={k: v for k, v in values.items() if v}))
        self.pocket = FakePocketSource(recordings or [])
        self.db = mongomock.MongoClient()["pocketsync"]
        self.files = CountingSink(LocalSink(self.files_root, opener=lambda u: AudioResp()))
        self.store = CountingSink(MongoSink(self.db))
        self.counting_state = CountingState(MongoStateStore(self.db))
        self.fake_gateway = FakeGateway()
        self.events_client = FakeEventsClient()
        self.ping_error: Exception | None = None
        self.probe_result: Any = {"embed": {"gemma2-embed": {"ready": True}}}

    def source(self) -> Any:
        self.settings.require("api_key")
        return self.pocket

    def files_sink(self) -> Any:
        return self.files

    def database(self) -> Any:
        self.settings.require("mongo_uri")
        return self.db

    def store_sink(self) -> Any:
        self.database()
        return self.store

    def state(self) -> Any:
        self.database()
        return self.counting_state

    def mongo_ping(self) -> None:
        self.database()
        if self.ping_error is not None:
            raise self.ping_error

    def gateway(self) -> Any:
        return self.fake_gateway

    def reconciler_options(self) -> dict[str, Any]:
        return {"sleep": lambda s: None, "page_delay": 0}

    def gateway_probe(self) -> dict[str, Any]:
        if isinstance(self.probe_result, Exception):
            raise self.probe_result
        return self.probe_result

    def publisher(self) -> Any:
        return self._once(
            "publisher",
            lambda: OutboxPublisher(
                self.files_root,
                client=self.events_client,
                envelope_factory=lambda ev: (ev, "pocketsync/recording/enriched"),
                connect_timeout=0.1,
                delivery_wait=0.1,
            ),
        )

    def total_writes(self) -> int:
        return self.files.writes + self.store.writes + self.counting_state.writes

    def files_on_disk(self) -> list[str]:
        root = self.files_root
        if not root.exists():
            return []
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def docs_in_db(self) -> int:
        return sum(self.db[name].count_documents({}) for name in self.db.list_collection_names())


def install(monkeypatch: Any, runtime: FakeRuntime) -> FakeRuntime:
    monkeypatch.setattr(_runtime, "build", lambda: runtime)
    return runtime
