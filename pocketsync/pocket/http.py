"""Official Pocket public-API client (stdlib ``urllib`` only).

Wire shapes are partly undocumented, so mapping is defensive and the full wire dict is kept in
``Recording.raw``. See ``_map_recording``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from pocketsync.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from pocketsync.model import ActionItem, Recording, TranscriptSegment
from pocketsync.pocket.base import MAX_PAGE_LIMIT, RateLimited, RecordingPage

BASE_URL = "https://public.heypocketai.com/api/v1/public"
DEFAULT_TIMEOUT = 30.0
MAX_THROTTLE_SECONDS = 120.0
_RETRY_LATER = "Retry later."


def _as_str_list(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("speakerName") or item.get("id")
            if name:
                out.append(str(name))
    return out


def _find_summary(summ: Any) -> str:
    """Pull summary markdown out of ``summarizations`` (shape not fully documented)."""
    if isinstance(summ, str):
        return summ
    if isinstance(summ, list):
        for item in summ:
            found = _find_summary(item)
            if found:
                return found
        return ""
    if not isinstance(summ, dict):
        return ""
    for path in (("v2", "summary", "markdown"), ("summary", "markdown"), ("markdown",)):
        cur: Any = summ
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur:
            return cur
    if isinstance(summ.get("summary"), str):
        return summ["summary"]
    for value in summ.values():
        if isinstance(value, (dict, list)):
            found = _find_summary(value)
            if found:
                return found
    return ""


def _find_action_items(summ: Any) -> list[Any]:
    if isinstance(summ, list):
        for item in summ:
            found = _find_action_items(item)
            if found:
                return found
        return []
    if not isinstance(summ, dict):
        return []
    for key in ("actionItems", "action_items"):
        val = summ.get(key)
        if isinstance(val, dict):  # live API: v2.actionItems.actions (verified 2026-10-14)
            val = val.get("actions") or val.get("items")
        if isinstance(val, list) and val:
            return val
    for value in summ.values():
        if isinstance(value, (dict, list)):
            found = _find_action_items(value)
            if found:
                return found
    return []


def _map_action_item(item: Any) -> ActionItem:
    if isinstance(item, str):
        return ActionItem(text=item)
    if not isinstance(item, dict):
        return ActionItem(text=str(item))
    text = (
        item.get("label") or item.get("text") or item.get("title") or item.get("description") or ""
    )
    done = item.get(
        "isCompleted", item.get("is_completed", item.get("done", item.get("completed")))
    )
    owner = item.get("assignee") or item.get("owner")
    return ActionItem(text=str(text), done=bool(done), owner=owner)


def _map_segments(transcript: Any) -> list[TranscriptSegment]:
    if isinstance(transcript, dict):
        transcript = transcript.get("segments") or []
    if not isinstance(transcript, list):
        return []
    return [TranscriptSegment.from_dict(s) for s in transcript if isinstance(s, dict)]


def _map_recording(wire: dict[str, Any]) -> Recording:
    """Map one wire recording object into the model; the wire dict is kept in ``raw``."""
    summ = wire.get("summarizations")
    return Recording(
        id=str(wire["id"]),
        title=wire.get("title") or "",
        description=wire.get("description") or "",
        created_at=wire.get("created_at"),
        updated_at=wire.get("updated_at"),
        recording_at=wire.get("recording_at"),
        duration=wire.get("duration"),
        language=wire.get("language"),
        state=None if wire.get("state") is None else str(wire["state"]),
        folder_id=wire.get("folder_id"),
        tags=_as_str_list(wire.get("tags")),
        speakers=_as_str_list(wire.get("speakers")),
        segments=_map_segments(wire.get("transcript")),
        summary=_find_summary(summ),
        action_items=[_map_action_item(a) for a in _find_action_items(summ)],
        raw=dict(wire),
    )


def _header_float(headers: Any, name: str) -> float | None:
    raw = headers.get(name) if headers is not None else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _retry_after(headers: Any, now: float) -> float | None:
    """Retry-After if present, else seconds until ``X-Ratelimit-Reset`` (unix epoch)."""
    explicit = _header_float(headers, "Retry-After")
    if explicit is not None:
        return explicit
    reset = _header_float(headers, "X-Ratelimit-Reset")
    return None if reset is None else max(0.0, reset - now)


class PocketHttpSource:
    """:class:`~pocketsync.pocket.base.PocketSource` backed by Pocket's public HTTP API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_throttle: float = MAX_THROTTLE_SECONDS,
    ) -> None:
        if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise CliError(EXIT_USER_ERROR, "Pocket base URL must be https", "Use an https URL.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._max_throttle = max_throttle
        # serve shares one source between the landing consumer and the reconciler.
        self._throttle_lock = threading.Lock()
        self._throttle_until: float | None = None

    def __repr__(self) -> str:  # never expose the key
        return f"PocketHttpSource(base_url={self._base_url!r})"

    def _note_rate_limit(self, headers: Any) -> None:
        """Pocket sends X-Ratelimit-{Limit,Remaining,Reset} (~50/min, verified 2026-10-14).

        When the window is exhausted, the NEXT request waits until the reset instead of
        drawing a 429.
        """
        remaining = _header_float(headers, "X-Ratelimit-Remaining")
        if remaining is not None and remaining <= 0:
            self._arm(_header_float(headers, "X-Ratelimit-Reset"))

    def _note_throttled(self, headers: Any) -> None:
        """A 429 means the window is exhausted: wait out its reset before the next request."""
        self._arm(_header_float(headers, "X-Ratelimit-Reset"))

    def _arm(self, reset: float | None) -> None:
        # Only ever extend the deadline: a concurrent response that still had quota left
        # must not disarm a window another thread just saw exhausted.
        if reset is None:
            return
        with self._throttle_lock:
            if self._throttle_until is None or reset > self._throttle_until:
                self._throttle_until = reset

    def _await_window(self) -> None:
        # Every caller waits until the deadline passes (serve shares this source between
        # threads); the deadline is dropped only once it has expired.
        with self._throttle_lock:
            until = self._throttle_until
            if until is not None and until <= self._clock():
                self._throttle_until = until = None
        if until is None:
            return
        wait = min(self._max_throttle, until - self._clock())
        if wait > 0:
            self._sleep(wait)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._await_window()
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310
                body = resp.read()
                self._note_rate_limit(getattr(resp, "headers", None))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self._note_throttled(exc.headers)
            self._raise_http(exc, self._clock())
        except OSError as exc:  # URLError and TimeoutError are OSErrors
            raise CliError(
                EXIT_ENV_ERROR, f"Pocket request failed: {exc}", "Check connectivity; retry."
            ) from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise CliError(
                EXIT_ENV_ERROR, "Pocket returned invalid JSON", "Retry; report if it persists."
            ) from exc

    @staticmethod
    def _raise_http(exc: urllib.error.HTTPError, now: float = 0.0) -> None:
        detail = ""
        try:
            payload = json.loads(exc.read() or b"{}")
            if isinstance(payload, dict):
                detail = str(payload.get("error") or "")
        except (ValueError, OSError):
            pass
        suffix = f": {detail}" if detail else ""
        if exc.code == 429:
            raise RateLimited(_retry_after(exc.headers, now)) from exc
        if exc.code in (401, 403):
            raise CliError(
                EXIT_USER_ERROR,
                f"Pocket rejected the API key (HTTP {exc.code}){suffix}",
                "Check the Pocket API key (pk_...) and regenerate it if needed.",
            ) from exc
        if exc.code == 404:
            raise CliError(
                EXIT_USER_ERROR, f"Pocket resource not found{suffix}", "Check the recording id."
            ) from exc
        raise CliError(EXIT_ENV_ERROR, f"Pocket HTTP {exc.code}{suffix}", _RETRY_LATER) from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise CliError(EXIT_ENV_ERROR, "Unexpected Pocket response", "Report this.")
        if payload.get("success") is False:
            raise CliError(
                EXIT_ENV_ERROR,
                f"Pocket error: {payload.get('error') or payload.get('error_code')}",
                _RETRY_LATER,
            )
        return payload

    def list_recordings(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> RecordingPage:
        if not 1 <= limit <= MAX_PAGE_LIMIT:
            raise CliError(
                EXIT_USER_ERROR, f"limit must be 1..{MAX_PAGE_LIMIT}", "Use a smaller page size."
            )
        payload = self._unwrap(
            self._get(
                "/recordings",
                {"start_date": start_date, "end_date": end_date, "page": page, "limit": limit},
            )
        )
        pag = payload.get("pagination") or {}
        return RecordingPage(
            recordings=[_map_recording(w) for w in payload.get("data") or []],
            page=pag.get("page", page),
            has_more=bool(pag.get("has_more", False)),
            total=pag.get("total"),
        )

    def get_recording(
        self,
        recording_id: str,
        include_transcript: bool = True,
        include_summarizations: bool = True,
    ) -> Recording:
        rid = urllib.parse.quote(recording_id, safe="")
        payload = self._unwrap(
            self._get(
                f"/recordings/{rid}",
                {
                    "include_transcript": str(include_transcript).lower(),
                    "include_summarizations": str(include_summarizations).lower(),
                },
            )
        )
        data = payload.get("data")
        if not isinstance(data, dict) or "id" not in data:
            raise CliError(EXIT_ENV_ERROR, "Pocket returned no recording", _RETRY_LATER)
        return _map_recording(data)

    def get_audio_url(self, recording_id: str, expires_in: int = 3600) -> str:
        if not 60 <= expires_in <= 86400:
            raise CliError(EXIT_USER_ERROR, "expires_in must be 60..86400", "Adjust expires_in.")
        rid = urllib.parse.quote(recording_id, safe="")
        payload = self._unwrap(
            self._get(f"/recordings/{rid}/audio-url", {"expires_in": expires_in})
        )
        data = payload.get("data")
        if isinstance(data, dict):  # live API: {signed_url, expires_in, expires_at}
            data = (
                data.get("signed_url")
                or data.get("url")
                or data.get("audio_url")
                or data.get("download_url")
            )
        if not isinstance(data, str) or not data:
            raise CliError(EXIT_ENV_ERROR, "Pocket returned no audio URL", _RETRY_LATER)
        return data
