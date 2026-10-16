"""One verb registry drives every surface: data-driven parity over ``verbs.REGISTRY``.

Mirrors culture-rules ``tests/test_surface_parity.py``. Each surface contributes a function
that lists the problems it has against the registry (a missing command, a missing flag, an
``apply`` that is not off by default...). The real surfaces must have none, and the mutation
cases prove each check actually bites: dropping a verb from the registry, or a command from
the CLI parser, makes the parity check fail.

The CLI half lives here now. The MCP half is added by the MCP server task (t10): see the
``MCP half`` section at the bottom, and add its problems function to :data:`SURFACES`.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - runs this interpreter on a fixed snippet
import sys
from dataclasses import replace

import pytest

from pocketsync import verbs
from pocketsync.cli import _build_parser, main
from pocketsync.verbs import REGISTRY, Context, Param, Verb
from tests.cli_fakes import FakeRuntime, install
from tests.test_cli_recall import _doc

# -- helpers --------------------------------------------------------------------------------


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    return {s for a in parser._actions for s in a.option_strings}


def _dests(parser: argparse.ArgumentParser) -> set[str]:
    return {a.dest for a in parser._actions}


# -- the CLI half ---------------------------------------------------------------------------


def cli_problems(registry, parser: argparse.ArgumentParser) -> list[str]:
    """Every way the CLI parser disagrees with ``registry`` (empty when in parity)."""
    problems: list[str] = []
    commands = _subparsers(parser)
    for verb in registry:
        sub = commands.get(verb.name)
        if sub is None:
            problems.append(f"cli: no command for verb {verb.name!r}")
            continue
        if "--json" not in _flags(sub):
            problems.append(f"cli: {verb.name} lacks --json")
        dests = _dests(sub)
        for p in verb.params:
            if p.name not in dests:
                problems.append(f"cli: {verb.name} lacks parameter {p.name!r}")
        if verb.mutating:
            if "--apply" not in _flags(sub):
                problems.append(f"cli: mutating {verb.name} lacks --apply")
        elif "--apply" in _flags(sub):
            problems.append(f"cli: read-only {verb.name} exposes --apply")
    return problems


#: surface name -> problems function. t10 adds ``"mcp": mcp_problems``.
SURFACES = {"cli": lambda registry: cli_problems(registry, _build_parser())}


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_surface_in_parity_with_registry(surface):
    assert SURFACES[surface](REGISTRY) == []


def test_registry_lists_the_talk_verbs():
    assert [v.name for v in REGISTRY] == [
        "enrich",
        "search",
        "get",
        "transcript",
        "recordings",
        "actions",
    ]
    assert [v.name for v in REGISTRY if v.mutating] == ["enrich"]


# -- mutation cases: each check bites -------------------------------------------------------


def _extra_verb() -> Verb:
    return Verb("phantom", "not wired anywhere", lambda ctx: {}, (Param("x"),), False)


def test_a_registry_verb_missing_from_the_cli_fails():
    assert cli_problems((*REGISTRY, _extra_verb()), _build_parser())


def test_a_cli_command_dropped_from_the_parser_fails():
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            del action.choices["search"]
    assert any("search" in p for p in cli_problems(REGISTRY, parser))


def test_a_new_param_missing_from_the_cli_fails():
    verb = REGISTRY[1]
    widened = replace(verb, params=(*verb.params, Param("phantom_flag")))
    assert cli_problems((widened,), _build_parser())


def test_a_read_verb_turned_mutating_fails():
    verb = next(v for v in REGISTRY if v.name == "search")
    assert cli_problems((replace(verb, mutating=True),), _build_parser())


# -- params_schema --------------------------------------------------------------------------


@pytest.mark.parametrize("verb", REGISTRY, ids=lambda v: v.name)
def test_params_schema_shape(verb):
    schema = verb.params_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])
    if verb.mutating:
        assert schema["properties"]["apply"] == {
            "type": "boolean",
            "default": False,
            "description": schema["properties"]["apply"]["description"],
        }
    else:
        assert "apply" not in schema["properties"]
    json.dumps(schema)  # JSON-able


def test_params_schema_details():
    search = verbs.get("search")
    props = search.params_schema()["properties"]
    assert search.params_schema()["required"] == ["query"]
    assert props["label"]["type"] == "array"
    assert props["label"]["items"] == {"type": "string"}
    assert props["limit"]["type"] == "integer"
    enrich = verbs.get("enrich").params_schema()["properties"]
    assert enrich["status"]["enum"] == ["pending", "done", "error"]
    assert enrich["id"]["type"] == "array"
    actions = verbs.get("actions").params_schema()["properties"]
    assert actions["open_only"]["default"] is True


def test_tool_names_are_unique_and_stable():
    names = [v.tool_name for v in REGISTRY]
    assert len(set(names)) == len(names)
    assert verbs.get("search").tool_name == "search"


def test_get_unknown_verb_returns_none():
    assert verbs.get("nope") is None


# -- invoke: the one entry point every surface calls ----------------------------------------


@pytest.fixture
def rt(tmp_path, monkeypatch):
    runtime = install(monkeypatch, FakeRuntime(tmp_path))
    runtime.db["recordings"].insert_many(
        [
            _doc("r0001", "2026-10-01T09:00:00Z", actions=[{"text": "send notes"}]),
            _doc("r0002", "2026-10-05T09:00:00Z", sensitive=0.9, status="error"),
        ]
    )
    return runtime


def test_invoke_rejects_unknown_and_missing_arguments(rt):
    from pocketsync.cli._errors import CliError

    with pytest.raises(CliError) as exc:
        verbs.get("search").invoke(rt, {"query": "x", "bogus": 1})
    assert exc.value.code == 1
    with pytest.raises(CliError):
        verbs.get("search").invoke(rt, {})
    with pytest.raises(CliError):
        verbs.get("search").invoke(rt, {"query": "x", "apply": True})  # read-only
    with pytest.raises(CliError):
        verbs.get("search").invoke(rt, {"query": "x", "limit": "ten"})
    with pytest.raises(CliError):
        verbs.get("search").invoke(rt, {"query": "x", "label": "meeting"})  # not a list
    with pytest.raises(CliError):
        verbs.get("enrich").invoke(rt, {"all": True, "status": "nope"})  # not in enum
    with pytest.raises(CliError):
        verbs.get("enrich").invoke(rt, {"all": True, "apply": "yes"})


def test_invoke_enrich_apply_defaults_off(rt):
    out = verbs.get("enrich").invoke(rt, {"all": True})
    assert out["dry_run"] is True
    out = verbs.get("enrich").invoke(rt, {"id": ["r0002"], "apply": True})
    assert out["dry_run"] is False and out["updated"] == 1


CASES = [
    ("enrich", ["--all"], {"all": True}),
    (
        "enrich",
        ["--status", "error", "--only", "classify"],
        {"status": "error", "only": "classify"},
    ),
    ("search", ["budgets"], {"query": "budgets"}),
    (
        "search",
        ["budgets", "--exclude-sensitive", "--label", "meeting"],
        {"query": "budgets", "exclude_sensitive": True, "label": ["meeting"]},
    ),
    ("get", ["r0001"], {"id": "r0001"}),
    ("transcript", ["r0001", "--start", "6"], {"id": "r0001", "start": 6}),
    ("recordings", ["--limit", "1"], {"limit": 1}),
    ("actions", ["--no-open-only"], {"open_only": False}),
]


@pytest.mark.parametrize("name, argv, arguments", CASES, ids=lambda c: str(c))
def test_cli_json_equals_registry_invoke(rt, capsys, name, argv, arguments):
    assert main([name, *argv, "--json"]) == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out == verbs.get(name).invoke(rt, arguments)


@pytest.mark.parametrize(
    "name, argv, target",
    [
        ("search", ["budgets"], ("pocketsync.recall.search", "search")),
        ("enrich", ["--all"], ("pocketsync.recall.enrich", "plan_enrich")),
        ("get", ["r0001"], ("pocketsync.recall.read", "get_recording")),
        ("transcript", ["r0001"], ("pocketsync.recall.read", "get_transcript")),
        ("recordings", [], ("pocketsync.recall.read", "list_recordings")),
        ("actions", [], ("pocketsync.recall.read", "list_action_items")),
    ],
)
def test_cli_calls_the_shared_core_function(rt, capsys, monkeypatch, name, argv, target):
    import importlib

    module = importlib.import_module(target[0])
    calls = []

    def spy(*a, **kw):
        calls.append((a, kw))
        return {"spied": True}

    monkeypatch.setattr(module, target[1], spy)
    assert main([name, *argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"spied": True}
    assert len(calls) == 1


def test_handler_receives_context_with_apply(rt, monkeypatch, capsys):
    seen = []
    enrich = verbs.get("enrich")
    monkeypatch.setattr(
        enrich, "handler", lambda ctx, **kw: seen.append(ctx) or {"ok": True}, raising=False
    )
    assert main(["enrich", "--all", "--apply", "--json"]) == 0
    capsys.readouterr()
    assert isinstance(seen[0], Context)
    assert seen[0].apply is True
    assert seen[0].runtime is rt


def test_verbs_module_imports_no_third_party_code():
    # Modules already loaded at interpreter start (site hooks such as _virtualenv) are not
    # pocketsync's doing; only what ``import pocketsync.verbs`` adds is checked.
    snippet = (
        "import sys; before = set(sys.modules); import pocketsync.verbs; "
        "std = set(sys.stdlib_module_names); "
        "bad = sorted(m for m in set(sys.modules) - before if m.split('.')[0] not in std "
        "and m.split('.')[0] != 'pocketsync'); "
        "print(','.join(bad))"
    )
    out = subprocess.run(  # nosec B603 - fixed argv
        [sys.executable, "-c", snippet], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


# -- MCP half (t10) -------------------------------------------------------------------------
# t10: define ``mcp_problems(registry) -> list[str]`` (every registry verb is exactly one MCP
# tool named ``verb.tool_name`` whose inputSchema == ``verb.params_schema()``, and no extra
# tools), register it as ``SURFACES["mcp"] = mcp_problems``, add the mutation cases
# (a tool dropped / an extra tool fails), and extend CASES-based output parity to the MCP
# tool call result.
