"""Contracts for scripts/fork/signature_drift.py.

Why this tool exists (2026-08-16): upstream ``1596148ff22`` added a
required keyword-only ``single_query_deny_message`` to
``_run_approval_gate`` in ``tools/approval.py``. Our fork's caller lives
in ``agent/copilot_acp_client.py`` -- a *different file* -- so the rebase
produced zero conflict markers, the merge-base overlap scout saw
nothing, the ``merge-tree`` rehearsal was clean, the CRLF pass was
clean, and the non-shell ACP approval branch shipped raising
``TypeError``.

Textual tooling structurally cannot see that class of break. These
tests pin the tool that can.

Fixtures here are SYNTHETIC on purpose. The obvious alternative --
pinning the real historical commits -- rots on the next rebase, which
rewrites every fork SHA (the same trap that orphaned 13 SHAs in
``docs/fork/changes.md``). The shapes below are modelled on the real
break instead of anchored to it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.fork.signature_drift import (
    BREAK,
    INFO,
    SourceTree,
    analyse,
    collect_call_sites,
    fork_files,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def write(root: Path, relpath: str, source: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def run(
    root: Path,
    caller_rel: str = "agent/caller.py",
    base: Path | None = None,
    merge_base: Path | None = None,
    caller_root: Path | None = None,
):
    """Collect call sites from ``caller_rel`` and bind them against ``root``.

    ``caller_root`` defaults to ``root``, but drift scenarios need the
    caller read from OUR tree while targets resolve at the upstream one.
    """
    source = (caller_root or root).joinpath(caller_rel).read_text(encoding="utf-8")
    sites = collect_call_sites(caller_rel, source)
    return analyse(
        sites,
        SourceTree(root=root),
        base_tree=SourceTree(root=base) if base is not None else None,
        merge_base_tree=(
            SourceTree(root=merge_base) if merge_base is not None else None
        ),
    )


def severities(report, severity):
    return [f for f in report.findings if f.severity == severity]


# ---------------------------------------------------------------------------
# the break that shipped
# ---------------------------------------------------------------------------


def test_upstream_adding_a_required_kwarg_is_a_break(tmp_path):
    """The exact 2026-08-16 shape: new required keyword-only param.

    Note the caller imports INSIDE the function body -- that is how the
    real ``copilot_acp_client.py`` call site is written, and an import
    index built only from ``tree.body`` would find no call at all here.
    """
    write(
        tmp_path,
        "tools/approval.py",
        """
        def _run_approval_gate(*, pattern_key, cron_deny_message,
                               single_query_deny_message):
            return {}
        """,
    )
    write(
        tmp_path,
        "agent/caller.py",
        """
        def handle():
            from tools.approval import _run_approval_gate
            return _run_approval_gate(
                pattern_key="k",
                cron_deny_message="c",
            )
        """,
    )

    breaks = severities(run(tmp_path), BREAK)
    assert len(breaks) == 1
    assert "single_query_deny_message" in breaks[0].message
    assert "missing" in breaks[0].message
    assert breaks[0].site.target == "tools.approval._run_approval_gate"


def test_call_site_is_clean_once_the_kwarg_is_passed(tmp_path):
    """Negative control -- without it the test above proves nothing."""
    write(
        tmp_path,
        "tools/approval.py",
        """
        def _run_approval_gate(*, pattern_key, cron_deny_message,
                               single_query_deny_message):
            return {}
        """,
    )
    write(
        tmp_path,
        "agent/caller.py",
        """
        def handle():
            from tools.approval import _run_approval_gate
            return _run_approval_gate(
                pattern_key="k",
                cron_deny_message="c",
                single_query_deny_message="s",
            )
        """,
    )
    assert severities(run(tmp_path), BREAK) == []


# ---------------------------------------------------------------------------
# other drift shapes
# ---------------------------------------------------------------------------


def test_upstream_removing_a_param_we_pass_is_a_break(tmp_path):
    write(tmp_path, "tools/approval.py", "def gate(*, pattern_key):\n    return {}\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.approval import gate

        def handle():
            return gate(pattern_key="k", display_target="t")
        """,
    )
    breaks = severities(run(tmp_path), BREAK)
    assert len(breaks) == 1
    assert "display_target" in breaks[0].message


def test_too_many_positionals_is_a_break(tmp_path):
    write(tmp_path, "tools/helper.py", "def fn(a, b):\n    return a\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1, 2, 3)
        """,
    )
    assert len(severities(run(tmp_path), BREAK)) == 1


def test_keyword_only_marker_is_respected(tmp_path):
    """`*` means the param cannot be passed positionally."""
    write(tmp_path, "tools/helper.py", "def fn(a, *, b):\n    return a\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1, 2)
        """,
    )
    assert len(severities(run(tmp_path), BREAK)) == 1


def test_positional_only_marker_is_respected(tmp_path):
    """`/` means the param cannot be passed by name."""
    write(tmp_path, "tools/helper.py", "def fn(a, /, b):\n    return a\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(a=1, b=2)
        """,
    )
    assert len(severities(run(tmp_path), BREAK)) == 1


def test_defaults_make_a_param_optional(tmp_path):
    write(tmp_path, "tools/helper.py", "def fn(a, b=1, *, c=2):\n    return a\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1)
        """,
    )
    assert severities(run(tmp_path), BREAK) == []


def test_target_var_keyword_absorbs_unknown_names(tmp_path):
    write(tmp_path, "tools/helper.py", "def fn(a, **kw):\n    return a\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1, anything=2, else_=3)
        """,
    )
    assert severities(run(tmp_path), BREAK) == []


def test_class_target_binds_against_init_without_self(tmp_path):
    write(
        tmp_path,
        "agent/thing.py",
        """
        class Thing:
            def __init__(self, name, *, size=1):
                self.name = name
        """,
    )
    write(
        tmp_path,
        "agent/caller.py",
        """
        from agent.thing import Thing

        def handle():
            return Thing("x", size=2)
        """,
    )
    assert severities(run(tmp_path), BREAK) == []


def test_module_alias_attribute_calls_are_resolved(tmp_path):
    """`import tools.approval as approval` then `approval.gate(...)`."""
    write(tmp_path, "tools/approval.py", "def gate(*, pattern_key):\n    return {}\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        import tools.approval as approval

        def handle():
            return approval.gate(pattern_key="k", gone="g")
        """,
    )
    breaks = severities(run(tmp_path), BREAK)
    assert len(breaks) == 1
    assert "gone" in breaks[0].message


def test_third_party_and_stdlib_calls_are_ignored(tmp_path):
    """Only first-party targets are our problem."""
    write(
        tmp_path,
        "agent/caller.py",
        """
        import json
        from pathlib import Path

        def handle():
            return json.dumps({}), Path("x", "y", "z", "extra", "more")
        """,
    )
    report = run(tmp_path)
    assert report.checked == 0
    assert report.findings == []


# ---------------------------------------------------------------------------
# **kwargs leniency -- provable vs unprovable
# ---------------------------------------------------------------------------


def test_kwargs_splat_suppresses_the_missing_argument_verdict(tmp_path):
    """A splat could be supplying the missing name, so no BREAK is claimed."""
    write(
        tmp_path,
        "tools/helper.py",
        "def fn(*, a, b):\n    return a\n",
    )
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle(extra):
            return fn(a=1, **extra)
        """,
    )
    assert severities(run(tmp_path), BREAK) == []


def test_kwargs_splat_still_catches_an_explicitly_unknown_name(tmp_path):
    """A splat cannot excuse a name we typed out that no longer exists."""
    write(tmp_path, "tools/helper.py", "def fn(*, a):\n    return a\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle(extra):
            return fn(a=1, removed=2, **extra)
        """,
    )
    breaks = severities(run(tmp_path), BREAK)
    assert len(breaks) == 1
    assert "removed" in breaks[0].message


# ---------------------------------------------------------------------------
# drift mode -- needs both revisions
# ---------------------------------------------------------------------------


def test_positional_reorder_binds_but_is_reported(tmp_path):
    """The failure `Signature.bind` is blind to.

    Two same-arity params swapped: the call still binds, and quietly
    means something else. Only a name-order comparison across the two
    revisions can see it.
    """
    before = tmp_path / "before"
    after = tmp_path / "after"
    write(before, "tools/helper.py", "def fn(src, dst):\n    return src\n")
    write(after, "tools/helper.py", "def fn(dst, src):\n    return src\n")
    caller = """
        from tools.helper import fn

        def handle():
            return fn("a", "b")
    """
    write(before, "agent/caller.py", caller)
    write(after, "agent/caller.py", caller)

    report = run(after, base=before)
    breaks = severities(report, BREAK)
    assert len(breaks) == 1
    assert "reorder" in breaks[0].message
    assert "src, dst" in breaks[0].detail


def test_reorder_is_not_flagged_when_the_call_uses_keywords(tmp_path):
    """Keyword calls are immune to reordering -- must not false-positive."""
    before = tmp_path / "before"
    after = tmp_path / "after"
    write(before, "tools/helper.py", "def fn(src, dst):\n    return src\n")
    write(after, "tools/helper.py", "def fn(dst, src):\n    return src\n")
    caller = """
        from tools.helper import fn

        def handle():
            return fn(src="a", dst="b")
    """
    write(before, "agent/caller.py", caller)
    write(after, "agent/caller.py", caller)

    assert severities(run(after, base=before), BREAK) == []


def test_target_vanishing_is_a_break_only_when_it_used_to_exist(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    write(before, "tools/helper.py", "def fn(a):\n    return a\n")
    write(after, "tools/helper.py", "def other(a):\n    return a\n")
    caller = """
        from tools.helper import fn

        def handle():
            return fn(1)
    """
    write(before, "agent/caller.py", caller)
    write(after, "agent/caller.py", caller)

    breaks = severities(run(after, base=before), BREAK)
    assert len(breaks) == 1
    assert "no longer exists" in breaks[0].message


def test_fork_only_symbol_absent_upstream_is_not_a_break(tmp_path):
    """A helper WE added does not exist upstream -- that is not drift.

    Caught late: the first predictive run against real ``upstream/main``
    produced 10 BREAKs and every one was a fork-only symbol
    (``_acp_config_str``, ``tui_gateway/acp_session_modes.py``, ...).
    Presence at the merge base is what distinguishes the two cases.
    """
    upstream = tmp_path / "upstream"
    ours = tmp_path / "ours"
    merge_base = tmp_path / "mb"

    # Upstream and the merge base never had this helper; we added it.
    write(upstream, "tools/helper.py", "def unrelated():\n    return 1\n")
    write(merge_base, "tools/helper.py", "def unrelated():\n    return 1\n")
    write(ours, "tools/helper.py", "def fork_added(a):\n    return a\n")
    write(
        ours,
        "agent/caller.py",
        """
        from tools.helper import fork_added

        def handle():
            return fork_added(1)
        """,
    )

    report = run(upstream, base=ours, merge_base=merge_base, caller_root=ours)
    assert severities(report, BREAK) == []

    # ...and it is still BOUND, not merely skipped: a bad call must fail.
    # Without this arm the assertion above passes for the wrong reason.
    write(
        ours,
        "agent/caller.py",
        """
        from tools.helper import fork_added

        def handle():
            return fork_added(1, 2, 3)
        """,
    )
    bad = run(upstream, base=ours, merge_base=merge_base, caller_root=ours)
    assert len(severities(bad, BREAK)) == 1


def test_upstream_deleting_a_shared_helper_is_still_a_break(tmp_path):
    """The other side of the merge-base test -- it must stay loud."""
    upstream = tmp_path / "upstream"
    ours = tmp_path / "ours"
    merge_base = tmp_path / "mb"

    # Present at the merge base => upstream owned it and removed it.
    write(merge_base, "tools/helper.py", "def shared(a):\n    return a\n")
    write(ours, "tools/helper.py", "def shared(a):\n    return a\n")
    write(upstream, "tools/helper.py", "def something_else():\n    return 1\n")
    write(
        ours,
        "agent/caller.py",
        """
        from tools.helper import shared

        def handle():
            return shared(1)
        """,
    )

    breaks = severities(
        run(upstream, base=ours, merge_base=merge_base, caller_root=ours), BREAK
    )
    assert len(breaks) == 1
    assert "no longer exists" in breaks[0].message


def test_our_own_signature_change_is_not_drift(tmp_path):
    """We widened a shared helper; upstream did not touch it.

    Real instance: the fork added ``advisory=`` to
    ``CopilotACPClient.__init__``. Upstream has no such parameter, so a
    naive two-way diff reports the param as "lost" -- but our hunk wins
    the rebase and nothing is lost at all.
    """
    upstream = tmp_path / "upstream"
    ours = tmp_path / "ours"
    merge_base = tmp_path / "mb"

    write(merge_base, "tools/helper.py", "def fn(a):\n    return a\n")
    write(upstream, "tools/helper.py", "def fn(a):\n    return a\n")
    write(ours, "tools/helper.py", "def fn(a, *, advisory=False):\n    return a\n")
    write(
        ours,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1, advisory=True)
        """,
    )

    report = run(upstream, base=ours, merge_base=merge_base, caller_root=ours)
    assert severities(report, BREAK) == []
    assert severities(report, INFO) == []


def test_both_sides_changing_one_signature_is_a_conflict(tmp_path):
    """Neither side's version simply wins -- say so instead of guessing."""
    upstream = tmp_path / "upstream"
    ours = tmp_path / "ours"
    merge_base = tmp_path / "mb"

    write(merge_base, "tools/helper.py", "def fn(a):\n    return a\n")
    write(upstream, "tools/helper.py", "def fn(a, *, mode=None):\n    return a\n")
    write(ours, "tools/helper.py", "def fn(a, *, advisory=False):\n    return a\n")
    write(
        ours,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1)
        """,
    )

    breaks = severities(
        run(upstream, base=ours, merge_base=merge_base, caller_root=ours), BREAK
    )
    assert len(breaks) == 1
    assert "both the fork and upstream" in breaks[0].message


def test_unresolvable_target_is_silent_without_a_base(tmp_path):
    """A name we cannot resolve statically is not evidence of a break.

    Re-exports, conditional defs and C extensions all land here. Staying
    quiet is what keeps validate mode usable as a test.
    """
    write(tmp_path, "tools/helper.py", "from somewhere import fn\n")
    write(
        tmp_path,
        "agent/caller.py",
        """
        from tools.helper import fn

        def handle():
            return fn(1, 2, 3)
        """,
    )
    report = run(tmp_path)
    assert report.findings == []
    assert report.resolved == 0


def test_compatible_signature_change_is_info_not_break(tmp_path):
    """A new optional param is worth knowing about, not worth failing on."""
    before = tmp_path / "before"
    after = tmp_path / "after"
    write(before, "tools/helper.py", "def fn(a):\n    return a\n")
    write(after, "tools/helper.py", "def fn(a, b=1):\n    return a\n")
    caller = """
        from tools.helper import fn

        def handle():
            return fn(1)
    """
    write(before, "agent/caller.py", caller)
    write(after, "agent/caller.py", caller)

    report = run(after, base=before)
    assert severities(report, BREAK) == []
    assert len(severities(report, INFO)) == 1


# ---------------------------------------------------------------------------
# the live guard
# ---------------------------------------------------------------------------


def test_the_real_fork_has_no_signature_drift_today():
    """Bind every real fork call site against the real working tree.

    This is the assertion that earns the tool its place in the suite:
    the next time upstream changes a helper signature out from under a
    fork call site, this fails instead of production.
    """
    try:
        paths = fork_files("upstream/main")
    except RuntimeError as exc:  # no upstream remote (fresh clone / CI)
        pytest.skip(f"upstream/main unavailable: {exc}")

    assert paths, "expected at least one fork-touched Python file"

    tree = SourceTree()
    sites = []
    for relpath in paths:
        source = tree.read(relpath)
        if source is not None:
            sites.extend(collect_call_sites(relpath, source))

    assert sites, "expected the fork to call at least one first-party helper"

    report = analyse(sites, tree)
    breaks = severities(report, BREAK)
    assert not breaks, "signature drift in fork call sites:\n" + "\n".join(
        f.render() for f in breaks
    )
