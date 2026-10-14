"""Split transcript segments into chunks under a token budget."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

DEFAULT_MAX_TOKENS = 1024  # well under the 8192-token embedding window


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: one token per 4 characters, rounded up."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    start: float | None
    end: float | None
    first_segment: int
    last_segment: int  # inclusive

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "segments": [self.first_segment, self.last_segment],
        }


def _render(segment: Mapping[str, Any]) -> str:
    text = str(segment.get("text", "")).strip()
    speaker = segment.get("speaker")
    return f"{speaker}: {text}" if speaker and text else text


def _split_long(line: str, budget_chars: int) -> list[str]:
    return [line[i : i + budget_chars] for i in range(0, len(line), budget_chars)]


def chunk_segments(
    segments: Iterable[Mapping[str, Any]], max_tokens: int = DEFAULT_MAX_TOKENS
) -> list[Chunk]:
    """Group segments (``text``, ``start``, ``end``, optional ``speaker``) into chunks.

    Each chunk stays within ``max_tokens`` (estimated); a single oversize segment
    is split into pieces that share its start/end times and segment index.
    """
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")
    budget_chars = max_tokens * 4
    chunks: list[Chunk] = []
    lines: list[str] = []
    state: dict[str, Any] = {}

    def flush() -> None:
        if lines:
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text="\n".join(lines),
                    start=state["start"],
                    end=state["end"],
                    first_segment=state["first"],
                    last_segment=state["last"],
                )
            )
        lines.clear()
        state.clear()

    def add(line: str, i: int, seg: Mapping[str, Any]) -> None:
        if lines and estimate_tokens("\n".join(lines + [line])) > max_tokens:
            flush()
        if not lines:
            state.update(start=seg.get("start"), first=i)
        lines.append(line)
        state.update(end=seg.get("end"), last=i)

    for i, seg in enumerate(segments):
        line = _render(seg)
        if not line:
            continue
        for piece in _split_long(line, budget_chars):
            add(piece, i, seg)
    flush()
    return chunks
