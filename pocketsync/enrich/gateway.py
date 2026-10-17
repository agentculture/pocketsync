"""Stdlib client for the lobes gateway: gemma2-embed embeddings and decider classification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.config import DEFAULT_CHAT_MODEL

DEFAULT_GATEWAY_URL = "http://localhost:8001/v1"
EMBED_MODEL = "gemma2-embed"
EMBED_DIMS = 768
DECIDER_MODEL = "decider:general"
CHAT_MODEL = DEFAULT_CHAT_MODEL  # one source: POCKETSYNC_CHAT_MODEL's default in config
DEFAULT_CHAT_MAX_TOKENS = 1024
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
DEFAULT_TIMEOUT = 60.0
_CHECK_GATEWAY = "check gateway"


class EnrichmentUnavailable(CliError):
    """The gateway/backend is not available right now (503/404/unreachable); retry later."""

    def __init__(self, message: str, remediation: str = "retry later", retry_after=None):
        super().__init__(EXIT_ENV_ERROR, message, remediation)
        self.retryable = True
        self.retry_after = retry_after


@dataclass(frozen=True)
class Label:
    name: str
    description: str


@dataclass(frozen=True)
class Filter:
    name: str
    instructions: str


@dataclass(frozen=True)
class Taxonomy:
    labels: tuple[Label, ...]
    filters: tuple[Filter, ...] = ()
    instructions: str = "What kind of recording is this?"
    multi: bool = False

    def canonical(self) -> dict[str, Any]:
        return {
            "instructions": self.instructions,
            "multi": self.multi,
            "labels": [[x.name, x.description] for x in self.labels],
            "filters": [[f.name, f.instructions] for f in self.filters],
        }

    @property
    def version(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def taxonomy_from_dict(data: Mapping[str, Any]) -> Taxonomy:
    labels = tuple(Label(str(x["name"]), str(x["description"])) for x in data.get("labels", []))
    if not labels:
        raise CliError(1, "taxonomy has no labels", "define at least one [[labels]] entry")
    filters = tuple(Filter(str(x["name"]), str(x["instructions"])) for x in data.get("filters", []))
    return Taxonomy(
        labels=labels,
        filters=filters,
        instructions=str(data.get("instructions", Taxonomy.instructions)),
        multi=bool(data.get("multi", False)),
    )


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load a taxonomy TOML file, or the shipped default when ``path`` is None."""
    try:
        if path is None:
            raw = resources.files("pocketsync.enrich").joinpath("default_taxonomy.toml")
            data = tomllib.loads(raw.read_text(encoding="utf-8"))
        else:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliError(2, f"cannot load taxonomy: {exc}", "check the taxonomy file path") from exc
    return taxonomy_from_dict(data)


def taxonomy_version(taxonomy: Taxonomy) -> str:
    return taxonomy.version


def build_questions(taxonomy: Taxonomy) -> dict[str, Any]:
    """Build the /v1/systemone ``questions`` object from a taxonomy."""
    questions: dict[str, Any] = {}
    if taxonomy.multi:
        for lab in taxonomy.labels:
            questions[f"label:{lab.name}"] = {
                "type": "noul",
                "instructions": f"{taxonomy.instructions} Is it: {lab.description}?",
            }
    else:
        questions["category"] = {
            "type": "choice",
            "instructions": taxonomy.instructions,
            "criteria": {lab.name: lab.description for lab in taxonomy.labels},
        }
    for flt in taxonomy.filters:
        questions[f"filter:{flt.name}"] = {"type": "noul", "instructions": flt.instructions}
    return questions


@dataclass
class Classification:
    labels: list[str]
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    filters: dict[str, float] = field(default_factory=dict)
    taxonomy_version: str = ""

    @property
    def label(self) -> str | None:
        return self.labels[0] if self.labels else None


#: Statuses worth retrying: lane loading or absent (503/404), throttled (429), or a proxy /
#: gateway hiccup (500/502/504). Anything else is a request bug and is not retried.
_TRANSIENT_STATUSES = frozenset({404, 429, 500, 502, 503, 504})


class GatewayClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        taxonomy: Taxonomy | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        threshold: float = 0.5,
        chat_model: str = CHAT_MODEL,
    ) -> None:
        url = base_url or os.environ.get("POCKETSYNC_GATEWAY_URL") or DEFAULT_GATEWAY_URL
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.taxonomy = taxonomy or load_taxonomy()
        self.timeout = timeout
        self.threshold = threshold
        self.chat_model = chat_model

    def _post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None):
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if self.api_key:
            hdrs["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(  # noqa: S310  # nosec B310
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers=hdrs,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_STATUSES:
                raise EnrichmentUnavailable(
                    f"gateway returned {exc.code} for {path}",
                    "retry later; the model lane may be loading or not deployed",
                    retry_after=exc.headers.get("Retry-After") if exc.headers else None,
                ) from exc
            raise CliError(2, f"gateway error {exc.code} for {path}", _CHECK_GATEWAY) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise EnrichmentUnavailable(f"gateway unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CliError(2, "gateway returned invalid JSON", _CHECK_GATEWAY) from exc

    def embed(self, texts: list[str], prompt_name: str = "Document") -> list[list[float]]:
        """Embed texts with gemma2-embed (``prompt_name`` Document or Query); asserts 768-d."""
        if not texts:
            return []
        resp = self._post(
            "/embeddings",
            {"model": EMBED_MODEL, "input": list(texts), "prompt_name": prompt_name},
        )
        data = sorted(resp.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [d["embedding"] for d in data]
        if len(vectors) != len(texts):
            raise CliError(2, "embedding count mismatch", _CHECK_GATEWAY)
        for vec in vectors:
            if len(vec) != EMBED_DIMS:
                raise CliError(2, f"expected {EMBED_DIMS}-d vectors, got {len(vec)}", "check model")
        return vectors

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = DEFAULT_CHAT_MAX_TOKENS,
    ) -> str:
        """One non-streaming chat completion; thinking off, no tools, think blocks stripped."""
        body = {
            "model": model or self.chat_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = self._post("/chat/completions", body)
        try:
            content = resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise CliError(2, "gateway returned a malformed chat response", _CHECK_GATEWAY) from exc
        content = _THINK_BLOCK.sub("", content)
        if "</think>" in content:  # an unopened block: the template pre-filled <think>
            content = content.rsplit("</think>", 1)[1]
        return content.strip()

    def classify(self, text: str) -> Classification:
        """Classify ``text`` against the configured taxonomy via /v1/systemone."""
        body = {
            "model": "clef",
            "state": text,
            "questions": build_questions(self.taxonomy),
        }
        resp = self._post("/systemone", body, {"X-Lobes-Model": DECIDER_MODEL})
        answers = resp.get("answers", {})
        probs: dict[str, float] = {}
        if self.taxonomy.multi:
            for lab in self.taxonomy.labels:
                ans = answers.get(f"label:{lab.name}", {})
                probs[lab.name] = float(ans.get("noul", 0.0))
            labels = sorted((n for n, p in probs.items() if p >= self.threshold), key=probs.get)
            labels.reverse()
            conf = probs[labels[0]] if labels else 0.0
        else:
            ans = answers.get("category", {})
            probs = {k: float(v) for k, v in ans.get("probabilities", {}).items()}
            labels = [ans["choice"]] if ans.get("choice") else []
            conf = float(ans.get("confidence", 0.0))
        # An unanswered filter is left out (unknown), never defaulted to 0.0: for a filter
        # like ``sensitive`` a fabricated 0.0 would read as "safe to share".
        filters: dict[str, float] = {}
        for f in self.taxonomy.filters:
            noul = answers.get(f"filter:{f.name}", {}).get("noul")
            if noul is not None:
                filters[f.name] = float(noul)
        return Classification(labels, conf, probs, filters, self.taxonomy.version)
