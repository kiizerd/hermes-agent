"""Contracts for scripts/fork/ref_drift.py.

Why this tool exists (2026-08-26): the 485-commit rebase moved every symbol
``docs/fork/*.md`` points at. `_acp_config` slid from line 506 to 726,
`_config_mcp_servers` from 1799 to 2155 -- and every one of those refs still
*resolved*, because the files are thousands of lines long. Nothing in a diff, a
test run, or a lint pass could see it. The same rebase orphaned 22 ledger SHAs.

The checks that gate are exact: a ref names a file that is missing, or a line
past the end of it, or a ``### <sha>`` names a commit unreachable from HEAD.
The anchor heuristic is opt-in and deliberately not a gate -- it produced 27
false positives against 3 real finds on the rebase that motivated the tool.

Fixtures are SYNTHETIC, for the reason the signature-drift tests give: pinning
real historical commits rots on the next rebase, which is the exact failure
this tool exists to catch.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.fork.ref_drift import check_refs


def write(root: Path, relpath: str, source: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    """A miniature repo: one 40-line source file plus a docs/fork dir."""
    root = tmp_path
    docs = root / "docs" / "fork"
    docs.mkdir(parents=True)
    body = "\n".join(f"line {i}" for i in range(1, 20))
    write(root, "agent/thing.py", f"{body}\ndef target():\n    return 1\n")
    return root, docs


def run(tree, **kw):
    root, docs = tree
    return check_refs(docs=docs, root=root, **kw)


# ---------------------------------------------------------------------------
# the exact checks -- these gate
# ---------------------------------------------------------------------------


def test_a_ref_inside_the_file_is_accepted(tree):
    _, docs = tree
    write(docs, "a.md", "`target()` lives at `agent/thing.py:20`.\n")
    assert run(tree) == []


def test_a_ref_past_the_end_of_the_file_is_reported(tree):
    _, docs = tree
    write(docs, "a.md", "`target()` lives at `agent/thing.py:900`.\n")
    problems = run(tree)
    assert len(problems) == 1
    assert "OUT OF RANGE" in problems[0]


def test_a_ref_to_a_file_that_does_not_exist_is_reported(tree):
    _, docs = tree
    write(docs, "a.md", "See `agent/gone.py:3`.\n")
    problems = run(tree)
    assert len(problems) == 1
    assert "MISSING FILE" in problems[0]


def test_a_bare_continuation_ref_inherits_the_last_named_file(tree):
    """`:12` after a named ref is how the docs write a second site in one file."""
    _, docs = tree
    write(docs, "a.md", "`target()` at `agent/thing.py:20`, and also `:12`.\n")
    assert run(tree) == []


def test_a_bare_ref_inheriting_the_wrong_file_is_caught_by_range(tree):
    """The real 2026-08-26 bug: `:1027` inherited a 528-line file.

    The tool cannot know which file the author meant, but a line past the end
    of the one it inherited is proof the ref is unreadable as written.
    """
    _, docs = tree
    write(docs, "a.md", "`agent/thing.py:20` does one thing; `helper()` does `:1027`.\n")
    problems = run(tree)
    assert len(problems) == 1
    assert "OUT OF RANGE" in problems[0]


def test_line_lists_and_ranges_are_each_checked(tree):
    _, docs = tree
    write(docs, "a.md", "Readers at `agent/thing.py:5,20,900`.\n")
    problems = run(tree)
    assert len(problems) == 1
    assert ":900" in problems[0]


def test_the_external_agent_bundle_is_skipped(tree):
    """acp-agent.js is not in this repo; the docs flag it separately."""
    _, docs = tree
    write(docs, "a.md", "Wire shape at `dist/acp-agent.js:4155`.\n")
    assert run(tree) == []


# ---------------------------------------------------------------------------
# the anchor heuristic -- opt-in, must stay silent by default
# ---------------------------------------------------------------------------


def test_a_ref_landing_far_from_its_named_symbol_is_silent_by_default(tree):
    _, docs = tree
    write(docs, "a.md", "`target()` is defined at `agent/thing.py:2`.\n")
    assert run(tree) == []


def test_the_same_ref_is_reported_once_anchors_are_requested(tree):
    _, docs = tree
    write(docs, "a.md", "`target()` is defined at `agent/thing.py:2`.\n")
    problems = run(tree, anchors_on=True)
    assert len(problems) == 1
    assert "NO ANCHOR" in problems[0]


def test_anchors_accept_a_ref_that_lands_on_its_symbol(tree):
    _, docs = tree
    write(docs, "a.md", "`target()` is defined at `agent/thing.py:20`.\n")
    assert run(tree, anchors_on=True) == []


# ---------------------------------------------------------------------------
# the live guard
# ---------------------------------------------------------------------------


def test_the_real_fork_docs_have_no_broken_refs_today():
    """Fails the moment a rebase moves a file out from under docs/fork."""
    assert check_refs() == []
