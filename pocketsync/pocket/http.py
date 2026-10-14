"""Official Pocket public-API client (stdlib ``urllib`` only).

Wire shapes are partly undocumented, so mapping is defensive and the full wire dict is kept in
``Recording.raw``. See ``_map_recording``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pocketsync.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from pocketsync.model import ActionItem, Recording, TranscriptSegment
from pocketsync.pocket.base import MAX_PAGE_LIMIT, RateLimited, RecordingPage

BASE_URL = "https://public.heypocketai.com/api/v1/public"
DEFAULT_TIMEOUT = 30.0


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
        if isinstance(val, dict):
            val = val.get("items")
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
    text = item.get("text") or item.get("title") or item.get("description") or ""
    done = item.get("done", item.get("completed", False))
    return ActionItem(text=str(text), done=bool(done), owner=item.get("owner"))


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


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class PocketHttpSource:
    """:class:`~pocketsync.pocket.base.PocketSource` backed by Pocket's public HTTP API."""

    def __init__(
        self, api_key: str, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise CliError(EXIT_USER_ERROR, "Pocket base URL must be https", "Use an https URL.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # never expose the key
        return f"PocketHttpSource(base_url={self._base_url!r})"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310
                body = resp.read()
        except urllib.error.HTTPError as exc:
            self._raise_http(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
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
    def _raise_http(exc: urllib.error.HTTPError) -> None:
        detail = ""
        try:
            payload = json.loads(exc.read() or b"{}")
            if isinstance(payload, dict):
                detail = str(payload.get("error") or "")
        except (ValueError, OSError):
            pass
        suffix = f": {detail}" if detail else ""
        if exc.code == 429:
            raise RateLimited(_retry_after(exc.headers)) from exc
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
        raise CliError(EXIT_ENV_ERROR, f"Pocket HTTP {exc.code}{suffix}", "Retry later.") from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise CliError(EXIT_ENV_ERROR, "Unexpected Pocket response", "Report this.")
        if payload.get("success") is False:
            raise CliError(
                EXIT_ENV_ERROR,
                f"Pocket error: {payload.get('error') or payload.get('error_code')}",
                "Retry later.",
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
            raise CliError(EXIT_ENV_ERROR, "Pocket returned no recording", "Retry later.")
        return _map_recording(data)

    def get_audio_url(self, recording_id: str, expires_in: int = 3600) -> str:
        if not 60 <= expires_in <= 86400:
            raise CliError(EXIT_USER_ERROR, "expires_in must be 60..86400", "Adjust expires_in.")
        rid = urllib.parse.quote(recording_id, safe="")
        payload = self._unwrap(
            self._get(f"/recordings/{rid}/audio-url", {"expires_in": expires_in})
        )
        data = payload.get("data")
        if isinstance(data, dict):  # shape undocumented: accept {url|audio_url|download_url}
            data = data.get("url") or data.get("audio_url") or data.get("download_url")
        if not isinstance(data, str) or not data:
            raise CliError(EXIT_ENV_ERROR, "Pocket returned no audio URL", "Retry later.")
        return data
