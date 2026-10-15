"""The operational verbs are wired into every introspection surface the rubric checks."""

from __future__ import annotations

import argparse
import json

import pytest

from pocketsync.cli import _build_parser, main
from pocketsync.cli._commands.overview import _VERBS
from pocketsync.explain import known_paths

NEW_VERBS = ("auth", "list", "sync", "watch", "serve", "status", "reembed")


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


def test_new_verbs_registered():
    assert set(NEW_VERBS) <= set(_subparsers(_build_parser()))


@pytest.mark.parametrize("verb", ["sync", "reembed"])
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
