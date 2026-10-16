"""The operational verbs are wired into every introspection surface the rubric checks."""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - runs this interpreter on a fixed snippet
import sys
import types

import pytest

from pocketsync.cli import _build_parser, main
from pocketsync.cli._commands.overview import _VERBS
from pocketsync.cli._grant import RUNTIME_VERBS
from pocketsync.explain import known_paths
from tests.cli_fakes import FakeRuntime, install

NEW_VERBS = (
    "auth",
    "list",
    "sync",
    "watch",
    "serve",
    "status",
    "reembed",
    "enrich",
    "search",
    "get",
    "transcript",
    "recordings",
    "actions",
    "mcp",
    "web",
)


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _walk(parser, path=()):
    yield path, parser
    for name, sub in _subparsers(parser).items():
        yield from _walk(sub, path + (name,))


def test_every_command_has_json_flag():
    for path, parser in _walk(_build_parser()):
        if not path:
            continue
        flags = {s for a in parser._actions for s in a.option_strings}
        assert "--json" in flags, f"{' '.join(path)} lacks --json"


@pytest.mark.parametrize("verb", NEW_VERBS)
def test_runtime_verbs_reexec_under_grant(verb):
    assert verb in RUNTIME_VERBS


def test_new_verbs_registered():
    assert set(NEW_VERBS) <= set(_subparsers(_build_parser()))


@pytest.mark.parametrize("verb", ["sync", "reembed", "enrich"])
def test_write_verbs_have_apply_defaulting_off(verb):
    sub = _subparsers(_build_parser())[verb]
    assert sub.parse_args([]).apply is False


@pytest.mark.parametrize("verb", NEW_VERBS)
def test_explain_entry_for_every_new_verb(verb, capsys):
    assert (verb,) in known_paths()
    assert main(["explain", verb]) == 0
    assert f"pocketsync {verb}" in capsys.readouterr().out


def test_learn_text_and_json_list_every_verb(capsys):
    main(["learn"])
    text = capsys.readouterr().out
    main(["learn", "--json"])
    paths = {tuple(c["path"]) for c in json.loads(capsys.readouterr().out)["commands"]}
    for verb in NEW_VERBS:
        assert f"pocketsync {verb}" in text
        assert (verb,) in paths


def test_overview_verbs_list_every_verb():
    names = {v.split(" ", 1)[0] for v in _VERBS}
    assert set(NEW_VERBS) <= names


# -- mcp / web entry stubs: lazy import, exit 2 with a hint when the server is absent --------


@pytest.mark.parametrize(
    "argv, module, hint",
    [
        (["mcp"], "pocketsync.mcp.server", "pocketsync[mcp]"),
        (["web"], "pocketsync.web.server", "pocketsync.web.server"),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_server_verbs_exit_2_when_server_absent(monkeypatch, capsys, argv, module, hint, as_json):
    monkeypatch.setitem(sys.modules, module, None)  # makes ``import module`` raise ImportError
    rc = main(argv + (["--json"] if as_json else []))
    out = capsys.readouterr()
    assert rc == 2
    assert out.out == ""  # stdout stays clean (reserved for the MCP protocol)
    if as_json:
        err = json.loads(out.err)
        assert err["code"] == 2
        assert hint in err["remediation"]
    else:
        assert out.err.startswith("error:")
        assert "hint:" in out.err
        assert hint in out.err


def test_importing_the_cli_does_not_import_the_servers():
    snippet = (
        "import sys, pocketsync.cli; from pocketsync.cli import _build_parser; _build_parser(); "
        "print([m for m in sys.modules if m.startswith(('pocketsync.mcp', 'pocketsync.web.server',"
        " 'mcp'))])"
    )
    out = subprocess.run(  # nosec B603 - fixed argv
        [sys.executable, "-c", snippet], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_mcp_runs_the_stdio_server_when_present(tmp_path, monkeypatch, capsys):
    calls = []
    fake = types.ModuleType("pocketsync.mcp.server")
    fake.run_stdio = lambda: calls.append("stdio")
    monkeypatch.setitem(sys.modules, "pocketsync.mcp", types.ModuleType("pocketsync.mcp"))
    monkeypatch.setitem(sys.modules, "pocketsync.mcp.server", fake)
    assert main(["mcp"]) == 0
    assert calls == ["stdio"]
    assert capsys.readouterr().out == ""


def test_web_serves_with_port_and_access_flag(tmp_path, monkeypatch):
    rt = install(monkeypatch, FakeRuntime(tmp_path))
    calls = []
    fake = types.ModuleType("pocketsync.web.server")
    fake.serve = lambda runtime, *, port, access_check: calls.append((runtime, port, access_check))
    monkeypatch.setitem(sys.modules, "pocketsync.web.server", fake)
    assert main(["web"]) == 0
    assert main(["web", "--port", "9001", "--no-access-check"]) == 0
    assert calls == [(rt, rt.settings.web_port, True), (rt, 9001, False)]


def test_web_rejects_bad_port(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path))
    fake = types.ModuleType("pocketsync.web.server")
    fake.serve = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "pocketsync.web.server", fake)
    assert main(["web", "--port", "70000"]) == 1
    assert "hint:" in capsys.readouterr().err
