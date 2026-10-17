"""Factory seam: build the Pocket source, sinks, state, gateway lanes and outbox from Settings.

Every operational verb gets its collaborators from :func:`build` (via :func:`get`), never by
constructing clients itself. Tests monkeypatch :data:`build` (or hand a ready
:class:`Runtime` subclass to it) to inject the fake Pocket source, mongomock, a fake gateway
and a temporary files root, so nothing reaches the network.

Objects are built lazily and cached per runtime: a verb that only needs Pocket never imports
pymongo, and ``sink`` + ``state`` share one Mongo client. Secrets are only ever passed to the
client constructors; nothing here prints or logs them.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from pocketsync import config
from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError

#: Seconds before a Mongo server-selection attempt gives up (doctor/status stay snappy).
MONGO_TIMEOUT_MS = 5000
#: Seconds for the gateway reachability probe.
GATEWAY_PROBE_TIMEOUT = 5.0


def capabilities_url(gateway_url: str) -> str:
    """``GET`` target for the gateway probe: ``/capabilities`` at the gateway root.

    ``gateway_url`` is the OpenAI-style base (``.../v1``); the lobes gateway serves an
    unauthenticated ``/capabilities`` document one level up, so probing it checks
    reachability without spending a model call or needing the API key.
    """
    base = gateway_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/capabilities"


class Runtime:
    """Lazily-built collaborators for one CLI invocation."""

    def __init__(self, settings: config.Settings | None = None) -> None:
        self.settings = settings if settings is not None else config.load()
        self._cache: dict[str, Any] = {}
        # One lock per key, so a slow build (Mongo server selection) blocks only callers of
        # that accessor; cached values are read without any lock. Re-entrant: an accessor
        # may build another (store_sink -> database) and the threaded web server can hit
        # the same accessor from two requests at once.
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _once(self, key: str, make: Callable[[], Any]) -> Any:
        try:
            return self._cache[key]
        except KeyError:
            pass
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.RLock())
        with lock:
            if key not in self._cache:
                self._cache[key] = make()
            return self._cache[key]

    # -- paths ---------------------------------------------------------------------------

    @property
    def files_root(self) -> Path:
        return Path(self.settings.files_root).expanduser()

    # -- Pocket --------------------------------------------------------------------------

    def source(self) -> Any:
        """The Pocket source (requires ``POCKETSYNC_API_KEY``)."""

        def make() -> Any:
            from pocketsync.pocket.http import PocketHttpSource

            self.settings.require("api_key")
            return PocketHttpSource(self.settings.api_key or "")

        return self._once("source", make)

    # -- sinks / state -------------------------------------------------------------------

    def files_sink(self) -> Any:
        def make() -> Any:
            from pocketsync.sinks.local import LocalSink

            return LocalSink(self.files_root)

        return self._once("files_sink", make)

    def database(self) -> Any:
        """The pymongo database (requires ``POCKETSYNC_MONGO_URI``)."""

        def make() -> Any:
            from pocketsync.sinks.mongo import connect

            self.settings.require("mongo_uri")
            return connect(self.settings.mongo_uri or "", serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)

        return self._once("database", make)

    def store_sink(self) -> Any:
        def make() -> Any:
            from pocketsync.sinks.mongo import MongoSink

            return MongoSink(self.database())

        return self._once("store_sink", make)

    def state(self) -> Any:
        def make() -> Any:
            from pocketsync.state import MongoStateStore

            return MongoStateStore(self.database())

        return self._once("state", make)

    def mongo_ping(self) -> None:
        """Round-trip ``ping`` with the configured credentials; raises on failure."""
        self.database().command("ping")

    def reconciler_options(self) -> dict[str, Any]:
        """Extra :class:`~pocketsync.reconcile.Reconciler` kwargs (tests inject ``sleep``)."""
        return {}

    def reconciler(self, *, state: Any = None, dry_run: bool = False) -> Any:
        from pocketsync.reconcile import Reconciler

        return Reconciler(
            self.source(),
            files_sink=self.files_sink(),
            store_sink=self.store_sink(),
            state=state if state is not None else self.state(),
            dry_run=dry_run,
            initial_since=self.catch_up_since(),
            **self.reconciler_options(),
        )

    def catch_up_since(self) -> str | None:
        """The automatic catch-up start day, or None when ``backfill_since`` is ``off``."""
        value = (self.settings.backfill_since or "").strip()
        return None if value.lower() in ("", "off", "none") else value

    # -- enrichment / events -------------------------------------------------------------

    def taxonomy(self) -> Any:
        def make() -> Any:
            from pocketsync.enrich.gateway import load_taxonomy

            path = self.settings.taxonomy_path
            return load_taxonomy(Path(path).expanduser() if path else None)

        return self._once("taxonomy", make)

    def gateway(self) -> Any:
        def make() -> Any:
            from pocketsync.enrich.gateway import GatewayClient

            return GatewayClient(
                self.settings.gateway_url, self.settings.gateway_api_key, self.taxonomy()
            )

        return self._once("gateway", make)

    def chat(self) -> Any:
        """The gateway client for the chat lane (model from ``POCKETSYNC_CHAT_MODEL``)."""

        def make() -> Any:
            from pocketsync.enrich.gateway import GatewayClient

            return GatewayClient(
                self.settings.gateway_url,
                self.settings.gateway_api_key,
                self.taxonomy(),
                chat_model=self.settings.chat_model,
            )

        return self._once("chat", make)

    def search_fn(self) -> Any:
        """The ``$vectorSearch`` seam for :func:`pocketsync.recall.search.search`.

        None means the default (``collection.aggregate``); tests return a fake, because
        mongomock cannot run ``$vectorSearch``.
        """
        return None

    def gateway_probe(self) -> dict[str, Any]:
        """``GET /capabilities`` on the gateway; return the parsed JSON (raises on failure)."""
        url = capabilities_url(self.settings.gateway_url)
        scheme = urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise CliError(
                EXIT_ENV_ERROR,
                f"gateway URL must be http(s), not {scheme or 'empty'}",
                f"Set {config.ENV_VARS['gateway_url']} to the gateway's /v1 base URL.",
            )
        req = urllib.request.Request(url, method="GET")  # noqa: S310  # nosec B310
        try:
            with urllib.request.urlopen(  # nosec B310 - scheme checked above
                req, timeout=GATEWAY_PROBE_TIMEOUT
            ) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise CliError(EXIT_ENV_ERROR, f"gateway returned HTTP {exc.code}", "") from exc
        except OSError as exc:  # URLError, TimeoutError and ConnectionError are OSErrors
            reason = getattr(exc, "reason", exc)
            raise CliError(EXIT_ENV_ERROR, f"gateway unreachable: {reason}", "") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def outbox(self) -> Any:
        def make() -> Any:
            from pocketsync.events import Outbox

            return Outbox(self.files_root)

        return self._once("outbox", make)

    def publisher(self) -> Any:
        def make() -> Any:
            from pocketsync.events import OutboxPublisher

            return OutboxPublisher(self.files_root)

        return self._once("publisher", make)

    def worker(self) -> Any:
        def make() -> Any:
            from pocketsync.enrich.worker import EnrichmentWorker

            return EnrichmentWorker(
                self.store_sink(),
                self.gateway(),
                self.outbox(),
                concurrency=self.settings.enrichment_concurrency,
            )

        return self._once("worker", make)


def _default_build() -> Runtime:
    return Runtime()


#: The factory every verb calls. Tests replace it (``monkeypatch.setattr``).
build: Callable[[], Runtime] = _default_build


def get() -> Runtime:
    """Return a runtime from the current :data:`build` factory."""
    return build()
