"""Local-disk sink: ``<root>/<YYYY>/<MM>/<recording-id>/`` with atomic writes, meta.json last."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import tempfile
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from pocketsync.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from pocketsync.model import Recording
from pocketsync.sinks.base import AudioDownloadError, FileRef, FileSet, PutResult, RecordState

DEFAULT_CHUNK = 64 * 1024
DEFAULT_AUDIO_EXT = ".m4a"
META_NAME = "meta.json"  # written last; its presence means the recording is complete
_LOCAL_HOSTS = ("localhost", "127.0.0.1")
_CONTENT_TYPE_EXT = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/webm": ".webm",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_YM_RE = re.compile(r"^(\d{4})-(\d{2})")

Opener = Callable[[str], Any]


def _check_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme == "https" and p.hostname:
        return
    if p.scheme == "http" and p.hostname in _LOCAL_HOSTS:
        return
    raise CliError(
        EXIT_USER_ERROR,
        "audio URL must be https (or http://localhost / 127.0.0.1)",
        "Use the pre-signed https URL from the Pocket adapter.",
    )


def _default_opener(url: str) -> Any:
    _check_url(url)
    return urllib.request.urlopen(url, timeout=60)  # nosec B310


def _audio_ext(content_type: str | None, url: str) -> str:
    if content_type:
        ext = _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    suffix = Path(urlparse(url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return DEFAULT_AUDIO_EXT


def download_audio(
    url: str, dest_dir: Path | str, *, opener: Opener | None = None, chunk: int = DEFAULT_CHUNK
) -> FileRef:
    """Stream ``url`` to ``dest_dir/audio.<ext>`` atomically, hashing sha256 while writing.

    Writes to a temp file in ``dest_dir`` and ``os.replace``s on success; the temp file is
    unlinked on any exception, so a partial download never leaves a final-named file. The
    returned ``rel_path`` is just the file name (relative to ``dest_dir``).
    """
    _check_url(url)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    resp = (opener or _default_opener)(url)
    h = hashlib.sha256()
    size = 0
    fd, tmp = tempfile.mkstemp(dir=dest, prefix=".audio.", suffix=".part")
    try:
        with resp, os.fdopen(fd, "wb") as out:
            headers = getattr(resp, "headers", None)
            ctype = headers.get("Content-Type") if headers is not None else None
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                h.update(block)
                size += len(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        name = "audio" + _audio_ext(ctype, url)
        os.replace(tmp, dest / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return FileRef(rel_path=name, size=size, sha256=h.hexdigest())


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ref(root: Path, path: Path, data: bytes) -> FileRef:
    return FileRef(
        rel_path=path.relative_to(root).as_posix(),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _fmt_ts(sec: float | None) -> str:
    if sec is None:
        return "--:--"
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def render_transcript_md(rec: Recording) -> str:
    lines = [f"# {rec.title or rec.id}", ""]
    for seg in rec.segments:
        who = seg.speaker_name or seg.speaker or "Speaker"
        lines.append(f"**{who}** [{_fmt_ts(seg.start)}]: {seg.text}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_summary_md(rec: Recording) -> str:
    out = [f"# {rec.title or rec.id}", "", rec.summary.strip(), ""]
    if rec.action_items:
        out += ["## Action items", ""]
        for a in rec.action_items:
            owner = f" ({a.owner})" if a.owner else ""
            out.append(f"- [{'x' if a.done else ' '}] {a.text}{owner}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalSink:
    """Sink writing to local disk under ``root`` (``~`` expanded)."""

    def __init__(self, root: Path | str, *, opener: Opener | None = None) -> None:
        self.root = Path(root).expanduser()
        self._opener = opener

    # -- paths -------------------------------------------------------------
    @staticmethod
    def _check_id(recording_id: str) -> None:
        if not _ID_RE.match(recording_id) or ".." in recording_id:
            raise CliError(
                EXIT_USER_ERROR,
                f"unsafe recording id: {recording_id!r}",
                "Recording ids must be filename-safe.",
            )

    def _year_month(self, rec: Recording) -> tuple[str, str]:
        for ts in (rec.recording_at, rec.created_at, rec.fetched_at):
            m = _YM_RE.match(ts or "")
            if m:
                return m.group(1), m.group(2)
        now = datetime.now(timezone.utc)
        return f"{now.year:04d}", f"{now.month:02d}"

    def _find_dir(self, recording_id: str) -> Path | None:
        self._check_id(recording_id)
        if not self.root.is_dir():
            return None
        for meta in sorted(
            self.root.glob(f"[0-9][0-9][0-9][0-9]/[0-9][0-9]/{recording_id}/meta.json")
        ):
            return meta.parent
        return None

    def _read_meta(self, recording_id: str) -> tuple[Path, dict[str, Any]] | None:
        d = self._find_dir(recording_id)
        if d is None:
            return None
        try:
            return d, json.loads((d / META_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CliError(
                EXIT_ENV_ERROR,
                f"unreadable meta.json for {recording_id}: {exc}",
                "Remove the corrupt recording directory and re-sync.",
            ) from exc

    def _locations(self, d: Path) -> dict[str, str]:
        rel = d.relative_to(self.root).as_posix()
        return {"dir": rel, "meta": f"{rel}/meta.json"}

    def _write_meta(self, d: Path, meta: dict[str, Any]) -> FileRef:
        data = (json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
        _atomic_write(d / META_NAME, data)
        return _ref(self.root, d / META_NAME, data)

    # -- protocol ----------------------------------------------------------
    def put_recording(
        self,
        rec: Recording,
        files: FileSet | None = None,
        *,
        dry_run: bool = False,
        audio_url: str | None = None,
    ) -> PutResult:
        self._check_id(rec.id)
        chash = rec.content_hash()
        existing = self._read_meta(rec.id)
        if existing is not None:
            d, meta = existing
            locs = self._locations(d)
            fs = FileSet.from_dict(meta.get("files") or {})
            same = meta.get("content_hash") == chash
            need_audio = audio_url is not None and fs.audio is None
            if same and not need_audio:
                return PutResult(False, chash, locs, fs)
        else:
            y, m = self._year_month(rec)
            d = self.root / y / m / rec.id
            meta, fs = {}, FileSet()
            locs = self._locations(d)
            same = False
        if dry_run:
            return PutResult(True, chash, locs, fs)

        d.mkdir(parents=True, exist_ok=True)
        if not same:
            tj = (
                json.dumps(
                    {"id": rec.id, "segments": [s.to_dict() for s in rec.segments]},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
            tm = render_transcript_md(rec).encode()
            sm = render_summary_md(rec).encode()
            for name, data in (("transcript.json", tj), ("transcript.md", tm), ("summary.md", sm)):
                _atomic_write(d / name, data)
            fs = replace(
                fs,
                transcript_json=_ref(self.root, d / "transcript.json", tj),
                transcript_md=_ref(self.root, d / "transcript.md", tm),
                summary_md=_ref(self.root, d / "summary.md", sm),
            )
        # Audio is immutable per recording: a content change rewrites text, never re-downloads.
        if audio_url is not None and fs.audio is None:
            try:
                a = download_audio(audio_url, d, opener=self._opener)
            except (OSError, ValueError, http.client.HTTPException, CliError) as exc:
                raise AudioDownloadError(f"audio download failed for {rec.id}: {exc}") from exc
            fs = replace(
                fs, audio=replace(a, rel_path=(d / a.rel_path).relative_to(self.root).as_posix())
            )

        new_meta = {
            "id": rec.id,
            "title": rec.title,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "recording_at": rec.recording_at,
            "duration": rec.duration,
            "language": rec.language,
            "content_hash": chash,
            "synced_at": _now(),
            "deleted_in_pocket": bool(meta.get("deleted_in_pocket", False)),
            "deleted_at": meta.get("deleted_at"),
            "merged_into": meta.get("merged_into"),
            "merged_at": meta.get("merged_at"),
            "files": {k: v for k, v in fs.to_dict().items() if k != "meta"},
        }
        fs = replace(fs, meta=self._write_meta(d, new_meta))  # meta.json last
        return PutResult(True, chash, locs, fs)

    def get_state(self, recording_id: str) -> RecordState | None:
        found = self._read_meta(recording_id)
        if found is None:
            return None
        meta = found[1]
        return RecordState(
            content_hash=meta.get("content_hash", ""),
            deleted_in_pocket=bool(meta.get("deleted_in_pocket", False)),
            merged_into=meta.get("merged_into"),
            has_audio=bool((meta.get("files") or {}).get("audio")),
        )

    def _update_meta(self, recording_id: str, **fields: Any) -> None:
        found = self._read_meta(recording_id)
        if found is None:
            raise CliError(
                EXIT_USER_ERROR,
                f"unknown recording: {recording_id}",
                "Sync the recording before tombstoning or merging it.",
            )
        d, meta = found
        meta.update(fields)
        self._write_meta(d, meta)

    def tombstone(self, recording_id: str, when: str) -> None:
        self._update_meta(recording_id, deleted_in_pocket=True, deleted_at=when)

    def mark_merged(self, src_id: str, dst_id: str, when: str) -> None:
        self._update_meta(src_id, merged_into=dst_id, merged_at=when)
