"""Every `self._foo(...)` must name a method that exists.

The GUI modules are barely reachable from unit tests — nothing here
constructs a BrowserWindow or an AuthorPage — so a misspelled or
renamed private method sits there compiling perfectly and raises only
when a user reaches that code path. Two landed in one day
(2026-09-02): `self._hide_progress()` in the BibTeX-import completion
handler, which had been renamed `_end_progress`, and it took a real
import to surface it.

This walks the AST instead: collect every method each class defines,
collect every `self._name(...)` it calls, and require the second to
be a subset of the first (plus its in-repo base classes). Only
underscore-prefixed names are checked — anything else may legitimately
come from Gtk, Adw or GObject, which we cannot see from here.
"""

import ast
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "alexandria")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# GObject calls these on our behalf; they are defined by the base
# class or wired through the type system, not by us.
_ALLOWED_MISSING = frozenset((
    "_class_init", "_init",
))


def _modules():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py") and not name.startswith("__"):
            yield os.path.join(PKG, name)


def _classes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node


def _defined(cls):
    """Method names defined directly in `cls`, plus any assigned as
    class attributes (`apply_links = SomeClass._method`)."""
    out = set()
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def _self_calls(cls):
    """(name, lineno) for every `self._x(...)` inside `cls`, and for
    `self._x` referenced as a bare attribute in a callback position."""
    out = []
    for node in ast.walk(cls):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name) or node.value.id != "self":
            continue
        if node.attr.startswith("_") and not node.attr.startswith("__"):
            out.append((node.attr, node.lineno))
    return out


def _bases_in_module(cls, by_name):
    """Names of base classes that are defined in the same file, so an
    inherited private method is not reported as missing."""
    seen = []
    for b in cls.bases:
        if isinstance(b, ast.Name) and b.id in by_name:
            seen.append(b.id)
    return seen


@pytest.mark.parametrize("path", list(_modules()),
                         ids=lambda p: os.path.basename(p))
def test_private_self_references_resolve(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    by_name = {c.name: c for c in _classes(tree)}
    problems = []
    for cls in by_name.values():
        known = set(_defined(cls))
        for base in _bases_in_module(cls, by_name):
            known |= _defined(by_name[base])
        # Attributes assigned anywhere in the class count as defined:
        # `self._foo = ...` in __init__ is the usual case.
        for node in ast.walk(cls):
            if isinstance(node, ast.Attribute) and \
                    isinstance(node.ctx, ast.Store) and \
                    isinstance(node.value, ast.Name) and \
                    node.value.id == "self":
                known.add(node.attr)
        for name, lineno in _self_calls(cls):
            if name in known or name in _ALLOWED_MISSING:
                continue
            problems.append("{}:{}: {}.self.{} is never defined".format(
                os.path.basename(path), lineno, cls.name, name))
    assert not problems, "\n" + "\n".join(sorted(set(problems)))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
