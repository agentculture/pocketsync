"""``pocketsync web``: the pocketchat page and its JSON API, loopback only, Access-gated.

A standard-library :class:`~http.server.ThreadingHTTPServer` (like
:mod:`pocketsync.webhook.server`) that binds loopback only and serves:

* ``GET /`` and ``GET /static/<file>`` from :data:`STATIC_DIR` (the page the operator uses);
* the JSON API of ``docs/web-api.md``. Every endpoint except ``/api/health`` and
  ``/api/chat`` is one registry verb called through :meth:`pocketsync.verbs.Verb.invoke`,
  the same call the CLI and the MCP server make; ``/api/chat`` (:mod:`pocketsync.web.chat`)
  retrieves through the ``search`` verb.

Every request, static or API, must carry a valid Cloudflare Access JWT
(``Cf-Access-Jwt-Assertion``) when a verifier is configured: a missing or bad token is
``403``, an unreachable JWKS is ``503`` (fail closed). A ``POST`` also needs
``Content-Type: application/json`` and an ``Origin`` equal to ``settings.site_origin``
(``403`` otherwise), and a body of at most :data:`MAX_BODY` bytes (``413``).

Every response carries the Content-Security-Policy and the other security headers of
:data:`SECURITY_HEADERS`; API responses are also ``Cache-Control: no-store``. Errors are always
``{"error": {"code", "message", "hint"}}`` and never carry a traceback.

Logging: one access line per request on :data:`ACCESS_LOGGER`, holding the method, the path
without its query string, the status and the duration in ms. The default
``BaseHTTPRequestHandler`` request log is silenced, and no question, body or answer is
logged anywhere.

:func:`serve` is the entry point of ``pocketsync web`` (:mod:`pocketsync.cli._commands.web`).
It refuses to start without a complete Access configuration unless ``access_check`` is
False, which prints a loud warning.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from pocketsync import __version__, verbs
from pocketsync.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from pocketsync.cli._output import emit_diagnostic
from pocketsync.cli._signals import install_signal_handlers, run_until_signal
from pocketsync.web import chat as web_chat
from pocketsync.web.access import (
    ACCESS_ENV,
    ENV_AUD,
    ENV_TEAM_DOMAIN,
    AccessConfig,
    AccessConfigError,
    AccessVerifier,
    VerificationError,
)

__all__ = ["ACCESS_LOGGER", "MAX_BODY", "STATIC_DIR", "WebServer", "serve"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024
#: an unaccepted body up to this size is read and discarded so the reply reaches the client
DRAIN_LIMIT = 1024 * 1024
BODY_TIMEOUT = 10.0
SOCKET_TIMEOUT = 30.0
ACCESS_LOGGER = "pocketsync.web.access"
JWT_HEADER = "Cf-Access-Jwt-Assertion"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_STOP_TIMEOUT = 5.0

CSP = (
    "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
#: static files that may be served, by extension (anything else under /static/ is a 404)
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
#: request-body spellings of docs/web-api.md -> the verb's parameter name
_ALIASES = {"search": {"labels": "label"}, "enrich": {"ids": "id"}}

access_log = logging.getLogger(ACCESS_LOGGER)


class HttpError(Exception):
    """An error response: HTTP ``status`` plus the JSON error body's code, message and hint."""

    def __init__(
        self, status: int, code: str, message: str, hint: str = "", extra: dict | None = None
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra or {}


def _forbidden(message: str, hint: str) -> HttpError:
    return HttpError(403, "forbidden", message, hint)


def _not_found(message: str = "not found", hint: str = "check the path") -> HttpError:
    return HttpError(404, "not_found", message, hint)


def _from_cli_error(exc: CliError) -> HttpError:
    if exc.code == EXIT_USER_ERROR:
        if exc.message.startswith("unknown recording"):
            return HttpError(404, "not_found", exc.message, exc.remediation)
        return HttpError(400, "bad_request", exc.message, exc.remediation)
    if exc.code == EXIT_ENV_ERROR:
        return HttpError(503, "unavailable", exc.message, exc.remediation or "retry later")
    return _internal()


def _internal() -> HttpError:
    return HttpError(500, "internal", "internal error", "check the pocketsync web service log")


def _printable(path: str) -> str:
    return "".join(c if c.isprintable() and not c.isspace() else "?" for c in path)[:512]


# -- query-string -> verb arguments ----------------------------------------------------------


def _coerce(verb: verbs.Verb, name: str, raw: str) -> Any:
    param = next((p for p in verb.params if p.name == name), None)
    if param is None:
        return raw  # unknown: Verb.bind rejects it with a proper hint
    try:
        if param.type == "integer":
            return int(raw)
        if param.type == "number":
            return float(raw)
    except ValueError:
        raise HttpError(
            400, "bad_request", f"{verb.name}: {name} must be {param.type}", "fix the query"
        ) from None
    if param.type == "boolean":
        low = raw.lower()
        if low in ("true", "1"):
            return True
        if low in ("false", "0"):
            return False
        raise HttpError(
            400, "bad_request", f"{verb.name}: {name} must be true or false", "fix the query"
        )
    return raw


def _query_args(verb: verbs.Verb, query: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    repeatable = {p.name for p in verb.params if p.repeatable}
    for name, raw in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if raw == "":
            continue  # ``since=&until=`` means "absent"
        value = _coerce(verb, name, raw)
        if name in repeatable:
            args.setdefault(name, []).append(value)
        else:
            args[name] = value
    return args


def _body_args(verb_name: str, body: dict[str, Any]) -> dict[str, Any]:
    args = dict(body)
    for alias, name in _ALIASES.get(verb_name, {}).items():
        if alias in args:
            if name in args:
                raise HttpError(
                    400, "bad_request", f"{verb_name}: pass {alias} or {name}, not both", ""
                )
            args[name] = args.pop(alias)
    return args


# -- the HTTP layer --------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    ctx: "WebServer"


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT
    server_version = "pocketsync-web"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # the default log has the full request line (query string); see _access_line

    # -- responses ---------------------------------------------------------------------------

    def _access_line(self, status: int) -> None:
        # written before the response leaves, so a client that has its reply sees the line
        ms = (time.monotonic() - self._started) * 1000.0
        access_log.info("%s %s %d %.1fms", self.command, self._log_path, status, ms)

    def _send(self, status: int, body: bytes, ctype: str, *, api: bool) -> None:
        self._status = status
        self._access_line(status)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        if api:
            self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self._send(status, body, "application/json; charset=utf-8", api=True)

    def _error(self, err: HttpError) -> None:
        payload = {"error": {"code": err.code, "message": err.message, "hint": err.hint}}
        payload.update(err.extra)
        self._json(err.status, payload)

    # -- dispatch ----------------------------------------------------------------------------

    def _handle(self) -> None:
        self._started = time.monotonic()
        self._status = 0
        self._body_read = False
        path, _, query = self.path.partition("?")
        path = path.partition("#")[0]
        self._log_path = _printable(path)
        try:
            try:
                self._authorize()
                self._route(path, query)
            except HttpError as err:
                if not self._body_read:
                    self._drain()
                self._error(err)
            except Exception as exc:  # noqa: BLE001 - never a traceback to the client
                access_log.error("internal error: %s", type(exc).__name__)
                self._error(_internal())
        finally:
            if not self._status:  # no response went out (the client went away mid-request)
                self._access_line(0)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle  # noqa: N815

    def _drain(self) -> None:
        """Discard an unread request body (bounded), or close: it must not become a request."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
            return
        try:
            length = int(raw_len)
        except ValueError:
            length = -1
        if not 0 <= length <= DRAIN_LIMIT:
            self.close_connection = True
            return
        try:
            self.connection.settimeout(BODY_TIMEOUT)
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def _authorize(self) -> None:
        verifier = self.server.ctx.verifier
        if verifier is None:
            return
        tok = self.headers.get(JWT_HEADER)
        if not tok:
            raise _forbidden("missing Access token", "sign in through Cloudflare Access")
        try:
            verifier.verify(tok.strip())
        except VerificationError as exc:
            if exc.reason == "unavailable":
                raise HttpError(
                    503, "unavailable", "Access keys unavailable", "retry in a minute"
                ) from None
            raise _forbidden(
                f"Access token refused: {exc.reason}", "sign in through Cloudflare Access again"
            ) from None

    def _route(self, path: str, query: str) -> None:
        method = self.command
        if path == "/" or path.startswith("/static/"):
            self._allow(method, "GET")
            self._static("index.html" if path == "/" else path[len("/static/") :])
            return
        if not path.startswith("/api/"):
            raise _not_found()
        api = path[len("/api/") :]
        if api == "health":
            self._allow(method, "GET")
            self._json(200, self._health())
            return
        if api in ("search", "enrich", "chat"):
            self._allow(method, "POST")
            body = self._read_json()
            if api == "chat":
                self._chat(body)
            else:
                self._json(200, self._invoke(api, _body_args(api, body)))
            return
        self._allow(method, "GET")
        self._json(200, self._get_api(api, query))

    def _get_api(self, api: str, query: str) -> dict[str, Any]:
        if api in ("recordings", "actions"):
            return self._invoke(api, _query_args(self._verb(api), query))
        parts = api.split("/")
        if parts[0] == "recordings" and len(parts) in (2, 3) and parts[1]:
            rid = urllib.parse.unquote(parts[1])
            if len(parts) == 2:
                return self._invoke("get", {"id": rid})
            if parts[2] == "transcript":
                args = _query_args(self._verb("transcript"), query)
                return self._invoke("transcript", {**args, "id": rid})
        raise _not_found()

    @staticmethod
    def _allow(method: str, allowed: str) -> None:
        if method == allowed or (allowed == "GET" and method == "HEAD"):
            return
        raise HttpError(405, "method_not_allowed", f"{method} not allowed", f"use {allowed}")

    @staticmethod
    def _verb(name: str) -> verbs.Verb:
        verb = verbs.get(name)
        if verb is None:  # pragma: no cover - the registry always has these
            raise _internal()
        return verb

    def _invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._verb(name).invoke(self.server.ctx.runtime, args)
        except CliError as exc:
            raise _from_cli_error(exc) from None

    def _health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "chat_model": self.server.ctx.runtime.settings.chat_model,
        }

    def _chat(self, body: dict[str, Any]) -> None:
        try:
            result = web_chat.answer(self.server.ctx.runtime, body)
        except web_chat.ChatUnavailable as exc:
            raise HttpError(
                503,
                "chat_unavailable",
                "the chat model is unavailable",
                "retry later; the sources found are listed",
                extra={"citations": exc.citations, "retrieval": exc.retrieval},
            ) from None
        except CliError as exc:
            raise _from_cli_error(exc) from None
        self._json(200, result)

    # -- request body ------------------------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        ctx = self.server.ctx
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _forbidden("POST needs Content-Type: application/json", "send a JSON body")
        if self.headers.get("Origin") != ctx.site_origin:
            raise _forbidden("cross-origin POST refused", "use the pocketchat page")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise HttpError(411, "bad_request", "Content-Length required", "send a JSON body")
        try:
            length = int(raw_len)
            if length < 0:
                raise ValueError
        except ValueError:
            raise HttpError(400, "bad_request", "bad Content-Length", "") from None
        if length > ctx.max_body:
            raise HttpError(
                413, "too_large", "request body too large", f"keep it under {ctx.max_body} bytes"
            )
        self.connection.settimeout(BODY_TIMEOUT)
        try:
            raw = self.rfile.read(length)
        except OSError:
            raise HttpError(408, "bad_request", "request body timed out", "") from None
        self._body_read = True
        if len(raw) != length:
            self.close_connection = True
            raise HttpError(400, "bad_request", "truncated request body", "")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HttpError(
                400, "bad_request", "body is not valid JSON", "send a JSON object"
            ) from None
        if not isinstance(body, dict):
            raise HttpError(400, "bad_request", "body must be a JSON object", "send {...}")
        return body

    # -- static ------------------------------------------------------------------------------

    def _static(self, name: str) -> None:
        root = self.server.ctx.static_dir
        name = urllib.parse.unquote(name)
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise _not_found()
        ctype = _STATIC_TYPES.get(Path(name).suffix.lower())
        target = root / name
        if ctype is None or not target.is_file():
            raise _not_found()
        self._send(200, target.read_bytes(), ctype, api=False)


class WebServer:
    """The pocketchat HTTP service; ``start()`` / ``stop()`` like the webhook server."""

    def __init__(
        self,
        runtime: Any,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        verifier: AccessVerifier | None,
        site_origin: str | None = None,
        static_dir: Path | None = None,
        max_body: int = MAX_BODY,
    ) -> None:
        if host not in _LOOPBACK:
            raise ValueError(f"web server may only bind loopback, not {host!r}")
        self.runtime = runtime
        self.host = "127.0.0.1" if host == "localhost" else host
        self._port = port
        self.verifier = verifier
        self.site_origin = site_origin or runtime.settings.site_origin
        self.static_dir = Path(static_dir) if static_dir is not None else STATIC_DIR
        self.max_body = max_body
        self.stop_event = threading.Event()
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._port
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        httpd = _Server((self.host, self._port), _Handler)
        httpd.ctx = self
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="pocketsync-web", daemon=True
        )
        self._thread.start()

    def stop(self) -> list[str]:
        """Stop serving; return the names of threads that did not finish in time."""
        leftover: list[str] = []
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT)
            if self._thread.is_alive():
                leftover.append(self._thread.name)
            self._thread = None
        return leftover


# -- the entry point -------------------------------------------------------------------------

_ACCESS_HINT = (
    f"set {ENV_TEAM_DOMAIN} and {ENV_AUD} (the Access team domain and application AUD tag), "
    "or pass --no-access-check for loopback development only"
)


def build_verifier(settings: Any) -> AccessVerifier:
    """The Access verifier from settings; CliError (exit 2) when Access is unset or partial."""
    env = {ENV_TEAM_DOMAIN: settings.access_team_domain or "", ENV_AUD: settings.access_aud or ""}
    try:
        cfg = AccessConfig.from_env(env)
    except AccessConfigError as exc:
        raise CliError(EXIT_ENV_ERROR, str(exc), _ACCESS_HINT) from None
    if cfg is None:
        raise CliError(
            EXIT_ENV_ERROR,
            f"Cloudflare Access is not configured ({' and '.join(ACCESS_ENV)} unset); "
            "refusing to serve without the JWT check",
            _ACCESS_HINT,
        )
    try:
        return AccessVerifier(cfg.team_domain, cfg.audience)
    except AccessConfigError as exc:
        raise CliError(EXIT_ENV_ERROR, str(exc), _ACCESS_HINT) from None


def _ensure_access_log_handler() -> None:
    if not access_log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s web %(message)s"))
        access_log.addHandler(handler)
    access_log.setLevel(logging.INFO)


def serve(
    runtime: Any,
    *,
    port: int,
    access_check: bool,
    install: Callable[[threading.Event], Callable[[], None]] = install_signal_handlers,
) -> int:
    """Run the site on ``127.0.0.1:port`` until SIGTERM/SIGINT; the ``pocketsync web`` body."""
    if access_check:
        verifier: AccessVerifier | None = build_verifier(runtime.settings)
    else:
        verifier = None
        emit_diagnostic(
            "WARNING: --no-access-check: Cloudflare Access JWT verification is OFF. "
            "Every request is served unauthenticated. Use this for loopback development only; "
            "never route a tunnel to this port."
        )
    service = WebServer(runtime, port=port, verifier=verifier)
    _ensure_access_log_handler()

    def started() -> None:
        emit_diagnostic(
            f"serving: pocketchat on http://127.0.0.1:{service.port}/ "
            f"(Access check {'on' if verifier else 'OFF'}; SIGTERM or Ctrl-C to stop)"
        )

    leftover = run_until_signal(service, service.stop_event, on_started=started, install=install)
    if leftover:
        emit_diagnostic(f"warning: threads still running at exit: {', '.join(leftover)}")
    return 0
