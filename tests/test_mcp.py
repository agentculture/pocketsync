"""t10: ``pocketsync mcp`` serves the one verb registry as MCP tools over stdio.

Mirrors culture-rules ``tests/mcp/test_mcp.py``: the server runs in-process against a fake
runtime through ``create_connected_server_and_client_session``, so nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess  # nosec B404 - runs this interpreter on a fixed argv
import sys
import threading

import pytest

pytest.importorskip("mcp")

from pocketsync import verbs  # noqa: E402
from pocketsync.cli._errors import CliError  # noqa: E402
from pocketsync.mcp import server as mcp_server  # noqa: E402
from pocketsync.mcp import tools  # noqa: E402
from pocketsync.verbs import REGISTRY  # noqa: E402
from tests.cli_fakes import FakeRuntime, install  # noqa: E402
from tests.test_cli_recall import _doc  # noqa: E402

PRIVACY = "Returns the operator's own recording content into the calling model's context."
DRY_RUN = "Writes are a dry-run unless apply=true."


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


def with_session(runtime, fn, factory=None):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        server = mcp_server.build_server(factory or (lambda: runtime))
        async with create_connected_server_and_client_session(server) as session:
            return await fn(session)

    return asyncio.run(go())


def call(runtime, name, args):
    async def fn(session):
        return await session.call_tool(name, args)

    return with_session(runtime, fn)


def list_tools(runtime):
    async def fn(session):
        return (await session.list_tools()).tools

    return with_session(runtime, fn)


def payload(res):
    assert not res.isError, res.content
    return json.loads(res.content[0].text)


# -- import hygiene -------------------------------------------------------------------------


def test_importing_the_package_does_not_import_mcp():
    code = (
        "import sys, pocketsync.mcp, pocketsync.mcp.tools, pocketsync.mcp.server;"
        "assert 'mcp' not in sys.modules, sorted(m for m in sys.modules if m.startswith('mcp'))"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # nosec B603


def test_tools_module_has_no_third_party_imports():
    snippet = (
        "import sys; before = set(sys.modules); import pocketsync.mcp.tools; "
        "std = set(sys.stdlib_module_names); "
        "bad = sorted(m for m in set(sys.modules) - before if m.split('.')[0] not in std "
        "and m.split('.')[0] != 'pocketsync'); "
        "print(','.join(bad))"
    )
    out = subprocess.run(  # nosec B603 - fixed argv
        [sys.executable, "-c", snippet], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_sdk_missing_raises_server_extra_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.types", None)
    with pytest.raises(mcp_server.ServerExtraMissing) as exc:
        mcp_server.build_server(lambda: None)
    assert "pocketsync[mcp]" in str(exc.value)
    assert issubclass(mcp_server.ServerExtraMissing, ImportError)
    with pytest.raises(mcp_server.ServerExtraMissing):
        mcp_server.run_stdio()


def test_module_main_exits_2_when_sdk_missing(monkeypatch, capsys):
    from pocketsync.mcp import __main__ as entry

    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.server", None)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", None)
    assert entry.main() == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("error:")
    assert "pocketsync[mcp]" in out.err


# -- the tool list is the registry ----------------------------------------------------------


def test_list_tools_equals_registry_verbs(rt):
    listed = list_tools(rt)
    assert [t.name for t in listed] == [v.tool_name for v in REGISTRY]
    by_name = {t.name: t for t in listed}
    for v in REGISTRY:
        assert by_name[v.tool_name].inputSchema == v.params_schema()


def test_tool_descriptions_carry_the_write_and_privacy_notes(rt):
    by_name = {t.name: t.description for t in list_tools(rt)}
    for v in REGISTRY:
        desc = by_name[v.tool_name]
        assert desc.startswith(v.summary)
        if v.mutating:
            assert DRY_RUN in desc
            assert PRIVACY not in desc
        else:
            assert PRIVACY in desc
            assert DRY_RUN not in desc


def test_list_tools_does_not_build_the_runtime():
    def factory():
        raise AssertionError("listing tools must not need a runtime")

    async def fn(session):
        return (await session.list_tools()).tools

    assert with_session(None, fn, factory=factory)


# -- calls ----------------------------------------------------------------------------------


def test_results_are_json_text(rt):
    res = call(rt, "get", {"id": "r0001"})
    assert res.content[0].type == "text"
    assert payload(res) == verbs.get("get").invoke(rt, {"id": "r0001"})


@pytest.mark.parametrize("apply", [None, False, "true", 1, "yes"])
def test_enrich_is_a_dry_run_unless_apply_is_literally_true(rt, apply):
    args = {"id": ["r0002"]}
    if apply is not None:
        args["apply"] = apply
    before = list(rt.db["recordings"].find({}))
    out = payload(call(rt, "enrich", args))
    assert out["dry_run"] is True
    assert list(rt.db["recordings"].find({})) == before


def test_enrich_apply_true_commits(rt):
    out = payload(call(rt, "enrich", {"id": ["r0002"], "apply": True}))
    assert out["dry_run"] is False
    assert out["updated"] == 1


def test_apply_on_a_read_tool_is_ignored(rt, monkeypatch):
    seen = []
    real = verbs.Verb.invoke

    # Patch the class, not the registry instance: undoing an instance patch leaves the
    # bound method behind on the instance and shadows later class-level spies.
    def spy(self, runtime, arguments=None):
        if self.name != "search":
            return real(self, runtime, arguments)
        seen.append(dict(arguments or {}))
        return {"ok": True}

    monkeypatch.setattr(verbs.Verb, "invoke", spy)
    assert payload(call(rt, "search", {"query": "x", "apply": True})) == {"ok": True}
    assert seen == [{"query": "x"}]


def test_cli_errors_become_is_error_text_with_hint(rt):
    res = call(rt, "get", {"id": "nope"})
    assert res.isError
    text = res.content[0].text
    assert "hint:" in text


def test_bad_arguments_and_unknown_tools_are_tool_errors(rt):
    for name, args in [
        ("search", {}),
        ("search", {"query": "x", "bogus": 1}),
        ("search", {"query": "x", "limit": "ten"}),
        ("no_such_tool", {}),
    ]:
        res = call(rt, name, args)
        assert res.isError, (name, args)
        assert "Traceback" not in res.content[0].text


def test_runtime_errors_are_tool_errors_not_crashes(tmp_path):
    def factory():
        raise CliError(2, "no runtime", "set POCKETSYNC_MONGO_URI")

    async def fn(session):
        return await session.call_tool("recordings", {})

    res = with_session(None, fn, factory=factory)
    assert res.isError
    assert "no runtime" in res.content[0].text
    assert "hint: set POCKETSYNC_MONGO_URI" in res.content[0].text


def test_unexpected_exceptions_are_tool_errors(rt, monkeypatch):
    def boom(ctx, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr(verbs.get("get"), "handler", boom)
    res = call(rt, "get", {"id": "r0001"})
    assert res.isError
    assert "kaput" in res.content[0].text


def test_runtime_is_built_lazily_once(rt):
    calls = []

    def factory():
        calls.append(1)
        return rt

    async def fn(session):
        await session.list_tools()
        assert calls == []
        await session.call_tool("recordings", {})
        await session.call_tool("actions", {})

    with_session(None, fn, factory=factory)
    assert calls == [1]


def test_default_factory_uses_cli_runtime(rt):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        server = mcp_server.build_server()  # no factory: pocketsync.cli._runtime.get()
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool("get", {"id": "r0001"})

    assert payload(asyncio.run(go())) == verbs.get("get").invoke(rt, {"id": "r0001"})


def test_call_tool_pure_function(rt):
    assert tools.call_tool("get", {"id": "r0001"}, rt) == verbs.get("get").invoke(
        rt, {"id": "r0001"}
    )
    with pytest.raises(tools.ToolError):
        tools.call_tool("nope", {}, rt)


# -- stdio: stdout carries only protocol ----------------------------------------------------


def _jsonrpc(*messages):
    return "".join(json.dumps(m) + "\n" for m in messages)


@pytest.mark.parametrize(
    "argv", [["-m", "pocketsync.mcp"], ["-m", "pocketsync", "mcp"]], ids=["module", "cli"]
)
def test_stdio_stdout_carries_only_protocol(tmp_path, argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("POCKETSYNC_")}
    env["POCKETSYNC_NO_GRANT"] = "1"
    env["HOME"] = str(tmp_path)
    stdin = _jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "recordings", "arguments": {}},
        },
    )
    # Keep stdin open until the last reply arrives: at EOF the server cancels in-flight calls.
    proc = subprocess.Popen(  # nosec B603 - fixed argv
        [sys.executable, *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    try:
        proc.stdin.write(stdin)
        proc.stdin.flush()
        raw = []
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            raw.append(line)
            if line.strip() and json.loads(line).get("id") == 3:
                break
        proc.stdin.close()
        # Python 3.12's communicate() flushes stdin even when it is already closed; detach it.
        proc.stdin = None
        rest, err = proc.communicate(timeout=30)
    finally:
        watchdog.cancel()
    assert proc.returncode == 0, err
    stdout = "".join(raw) + rest
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    msgs = [json.loads(ln) for ln in lines]  # every stdout line is a JSON-RPC message
    assert all(m.get("jsonrpc") == "2.0" for m in msgs)
    by_id = {m["id"]: m for m in msgs if "id" in m}
    assert [t["name"] for t in by_id[2]["result"]["tools"]] == [v.tool_name for v in REGISTRY]
    # no mongo configured: the call is a tool error (with a hint), not a crash
    assert by_id[3]["result"]["isError"] is True
    assert "hint:" in by_id[3]["result"]["content"][0]["text"]


def test_cli_mcp_exits_2_with_hint_when_sdk_missing(monkeypatch, capsys):
    from pocketsync.cli import main

    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.server", None)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", None)
    assert main(["mcp"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("error:")
    assert "pocketsync[mcp]" in out.err
