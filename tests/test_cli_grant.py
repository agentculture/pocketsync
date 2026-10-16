"""Running under grant when the secret settings live there (operator CLI ergonomics).

Hidden grant secrets cannot be read, only injected, so a bare ``pocketsync status`` in the
operator's shell re-execs itself as ``grant run --inject VAR=VAR ... -- python -m pocketsync``.
Every collaborator is faked; no test runs the real ``grant``.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from pocketsync.cli import _grant

ALL = set(_grant.SECRET_ENV)


class Exec(Exception):
    """Raised by the fake execvpe so a test can inspect what would have run."""

    def __init__(self, file, args, env):
        super().__init__(file)
        self.file, self.argv, self.env = file, list(args), env


def fake_exec(file, args, env):
    raise Exec(file, args, env)


def fake_run(names, ok=True):
    def run(cmd, **kw):
        assert cmd[1:] == ["list", "--json"]
        out = json.dumps({"ok": ok, "names": sorted(names)})
        return SimpleNamespace(returncode=0 if ok else 1, stdout=out, stderr="")

    return run


def call(argv, env, names=ALL, which=lambda n: "/opt/bin/grant", run=None):
    return _grant.maybe_reexec(
        argv, env=env, which=which, run=run or fake_run(names), execvpe=fake_exec
    )


def test_status_without_env_reexecs_under_grant_with_only_the_missing_names():
    env = {"POCKETSYNC_API_KEY": "pk_already_set"}
    with pytest.raises(Exec) as ei:
        call(["status", "--json"], env)
    e = ei.value
    assert e.file == "/opt/bin/grant"
    sep = e.argv.index("--")
    injected = [e.argv[i + 1] for i, a in enumerate(e.argv[:sep]) if a == "--inject"]
    assert "POCKETSYNC_MONGO_URI=POCKETSYNC_MONGO_URI" in injected
    assert not any(i.startswith("POCKETSYNC_API_KEY=") for i in injected)  # env wins
    assert e.argv[sep + 1 :] == [sys.executable, "-m", "pocketsync", "status", "--json"]
    assert e.env[_grant.REEXEC_MARK] == "1"
    assert e.env["POCKETSYNC_API_KEY"] == "pk_already_set"


def test_only_names_grant_actually_holds_are_injected():
    with pytest.raises(Exec) as ei:
        call(["status"], {}, names={"POCKETSYNC_MONGO_URI", "UNRELATED"})
    injected = [a for a in ei.value.argv if a.startswith("POCKETSYNC_")]
    assert injected == ["POCKETSYNC_MONGO_URI=POCKETSYNC_MONGO_URI"]


@pytest.mark.parametrize(
    "argv, env, kw",
    [
        (["status"], {_grant.REEXEC_MARK: "1"}, {}),  # already under grant: no loop
        (["status"], {_grant.OPT_OUT: "1"}, {}),  # operator opted out
        (["status"], {v: "x" for v in _grant.SECRET_ENV}, {}),  # nothing missing
        (["status"], {}, {"which": lambda n: None}),  # grant not installed
        (["status"], {}, {"names": set()}),  # grant holds none of them
        (["status"], {}, {"run": fake_run(ALL, ok=False)}),  # grant list failed
        (["whoami"], {}, {}),  # introspection verbs never need secrets
        (["--help"], {}, {}),
        ([], {}, {}),
    ],
)
def test_no_reexec_when_it_cannot_or_should_not_help(argv, env, kw):
    assert call(argv, env, **kw) is None


def test_grant_list_crash_is_not_fatal():
    def boom(cmd, **kw):
        raise OSError("grant exploded")

    assert call(["status"], {}, run=boom) is None


def test_test_session_opts_out(monkeypatch):
    # conftest sets the opt-out for the whole session, so an in-process main() or a
    # `python -m pocketsync` child can never exec the real grant.
    import os

    assert os.environ.get(_grant.OPT_OUT) == "1"


@pytest.mark.parametrize("stdout", ['["POCKETSYNC_MONGO_URI"]', '"locked"', "null", "42"])
def test_non_object_grant_json_is_ignored(stdout):
    # Review of PR #7 (3): valid JSON that is not an object must not crash main().
    def run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    assert call(["status"], {}, run=run) is None


def test_exec_failure_falls_through_to_the_normal_path():
    # Review of PR #7 (4): grant vanishing between which() and exec must not leak a traceback.
    def broken_exec(file, args, env):
        raise FileNotFoundError(file)

    result = _grant.maybe_reexec(
        ["status"],
        env={},
        which=lambda n: "/opt/bin/grant",
        run=fake_run(ALL),
        execvpe=broken_exec,
    )
    assert result is None


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_never_spawns_grant(flag):
    # Review of PR #7 (6): help must not pay for a `grant list`.
    def run(cmd, **kw):
        raise AssertionError("grant must not be called for help")

    assert call(["status", flag], {}, run=run) is None


def test_secret_names_come_from_the_settings_layer():
    # Review of PR #7 (7): one source of truth for which settings are secrets.
    from pocketsync import config

    assert set(_grant.SECRET_ENV) == {config.ENV_VARS[n] for n in config.SECRET_SETTINGS}
