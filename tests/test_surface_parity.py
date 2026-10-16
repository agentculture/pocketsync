"""One verb registry drives every surface: data-driven parity over ``verbs.REGISTRY``.

Mirrors culture-rules ``tests/test_surface_parity.py``. Each surface contributes a function
that lists the problems it has against the registry (a missing command, a missing flag, an
``apply`` that is not off by default...). The real surfaces must have none, and the mutation
cases prove each check actually bites: dropping a verb from the registry, or a command from
the CLI parser, makes the parity check fail.

The CLI half and the MCP half (tool specs, plus in-process tool-call output parity against
the CLI and :meth:`Verb.invoke`) both live here.
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


#: surface name -> problems function (``mcp_problems`` is defined in the MCP half below).
SURFACES = {
    "cli": lambda registry: cli_problems(registry, _build_parser()),
    "mcp": lambda registry: mcp_problems(registry, _mcp_specs()),
}


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


def _mcp_specs() -> list[dict]:
    from pocketsync.mcp import tools  # stdlib-only: no mcp SDK needed to list the specs

    return tools.tool_specs()


def mcp_problems(registry, specs: list[dict]) -> list[str]:
    """Every way the MCP tool list disagrees with ``registry`` (empty when in parity)."""
    problems: list[str] = []
    by_name: dict[str, dict] = {}
    for spec in specs:
        if spec["name"] in by_name:
            problems.append(f"mcp: duplicate tool {spec['name']!r}")
        by_name[spec["name"]] = spec
    wanted = {v.tool_name for v in registry}
    for extra in sorted(set(by_name) - wanted):
        problems.append(f"mcp: tool {extra!r} has no registry verb")
    for verb in registry:
        spec = by_name.get(verb.tool_name)
        if spec is None:
            problems.append(f"mcp: no tool for verb {verb.name!r}")
            continue
        if spec["inputSchema"] != verb.params_schema():
            problems.append(f"mcp: {verb.name} inputSchema != params_schema()")
        props = spec["inputSchema"].get("properties", {})
        if verb.mutating:
            if props.get("apply", {}).get("default") is not False:
                problems.append(f"mcp: mutating {verb.name} lacks apply (default false)")
            if "apply=true" not in spec["description"]:
                problems.append(f"mcp: mutating {verb.name} does not say it is a dry run")
        elif "apply" in props:
            problems.append(f"mcp: read-only {verb.name} exposes apply")
        elif "calling model's context" not in spec["description"]:
            problems.append(f"mcp: read tool {verb.name} lacks the privacy note")
    return problems


def test_a_registry_verb_missing_from_mcp_fails():
    assert mcp_problems((*REGISTRY, _extra_verb()), _mcp_specs())


def test_an_mcp_tool_dropped_fails():
    specs = [s for s in _mcp_specs() if s["name"] != "search"]
    assert any("search" in p for p in mcp_problems(REGISTRY, specs))


def test_an_extra_mcp_tool_fails():
    specs = [*_mcp_specs(), {"name": "phantom", "description": "", "inputSchema": {}}]
    assert any("phantom" in p for p in mcp_problems(REGISTRY, specs))


def test_an_mcp_schema_drift_fails():
    specs = _mcp_specs()
    specs[1] = {**specs[1], "inputSchema": {**specs[1]["inputSchema"], "required": []}}
    assert mcp_problems(REGISTRY, specs)


def test_a_read_verb_turned_mutating_fails_on_mcp():
    verb = next(v for v in REGISTRY if v.name == "search")
    assert mcp_problems((replace(verb, mutating=True),), _mcp_specs())


def _mcp_call(runtime, name: str, arguments: dict):
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    from pocketsync.mcp import server as mcp_server

    async def go():
        server = mcp_server.build_server(lambda: runtime)
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(name, arguments)

    return asyncio.run(go())


@pytest.mark.parametrize("name, argv, arguments", CASES, ids=lambda c: str(c))
def test_mcp_result_equals_cli_json_and_registry_invoke(rt, capsys, name, argv, arguments):
    pytest.importorskip("mcp")
    res = _mcp_call(rt, name, arguments)
    assert not res.isError, res.content
    mcp_out = json.loads(res.content[0].text)
    assert main([name, *argv, "--json"]) == 0
    assert mcp_out == json.loads(capsys.readouterr().out)
    assert mcp_out == verbs.get(name).invoke(rt, arguments)


def test_mcp_enrich_apply_matches_cli_apply(tmp_path, monkeypatch, capsys):
    pytest.importorskip("mcp")
    results = []
    for sub, via_cli in (("cli", True), ("mcp", False)):
        runtime = install(monkeypatch, FakeRuntime(tmp_path / sub))
        runtime.db["recordings"].insert_many(
            [_doc("r0002", "2026-10-05T09:00:00Z", sensitive=0.9, status="error")]
        )
        if via_cli:
            assert main(["enrich", "--id", "r0002", "--apply", "--json"]) == 0
            results.append(json.loads(capsys.readouterr().out))
        else:
            res = _mcp_call(runtime, "enrich", {"id": ["r0002"], "apply": True})
            assert not res.isError, res.content
            results.append(json.loads(res.content[0].text))
    assert results[0] == results[1]
    assert results[1]["dry_run"] is False and results[1]["updated"] == 1


def test_mcp_cli_error_matches_cli_error(rt, capsys):
    pytest.importorskip("mcp")
    res = _mcp_call(rt, "get", {"id": "nope"})
    assert res.isError
    assert main(["get", "nope", "--json"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["message"] in res.content[0].text
    assert f"hint: {err['remediation']}" in res.content[0].text
