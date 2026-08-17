#!/usr/bin/env python
"""Detect signature drift between fork call sites and upstream helpers.

Rebase reports conflicts *textually*. When upstream changes a function's
signature in one file and our fork calls that function from a different
file, the rebase is clean, every conflict marker is absent, every CRLF
check passes -- and the call site is broken at runtime.

That happened on 2026-08-16: upstream ``1596148ff22`` added a required
keyword-only ``single_query_deny_message`` to ``_run_approval_gate``
(``tools/approval.py``); our caller in ``agent/copilot_acp_client.py``
did not pass it, and the non-shell ACP approval branch started raising
``TypeError``. Nothing in the merge-base overlap scout, the
``merge-tree`` rehearsal, or the CRLF pass can see that class of break.

This module sees it. It walks the fork's own files, finds every call
into a first-party helper, reconstructs that helper's signature from
source at a chosen revision, and asks CPython's own binder whether the
call would still work.

Two modes
---------

``validate`` (default)
    Bind every fork call site against the code as it is *right now*.
    Answers "is the fork broken today". Cheap enough to run as a test.

``drift --against <rev>``
    Bind against another revision as well -- typically
    ``upstream/main`` before pulling it. Answers "will pulling this
    break us", and reports *why* by diffing the two signatures.

Usage
-----

    python scripts/fork/signature_drift.py
    python scripts/fork/signature_drift.py --against upstream/main
    python scripts/fork/signature_drift.py --against 1596148ff22 --base 26b2b475935

Exit status is 1 when any BREAK is found, 0 otherwise. UNKNOWN findings
(call sites that splat ``**kwargs``, so no static proof is possible) do
not fail the run; they are printed so a human can eyeball them.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]

# Only these top-level packages are treated as first-party. A call into
# `json` or `httpx` is not our problem; a call into `tools.approval` is.
# Kept explicit rather than "anything importable" so a stdlib module that
# happens to share a name with one of ours cannot silently shadow it.
FIRST_PARTY_ROOTS = frozenset(
    {
        "agent",
        "cron",
        "gateway",
        "hermes_cli",
        "plugins",
        "providers",
        "tools",
        "tui_gateway",
        "utils",
    }
)

# Single-module first-party files that live at the repo root.
FIRST_PARTY_MODULES = frozenset(
    {
        "batch_runner",
        "hermes_constants",
        "hermes_logging",
        "hermes_state",
        "model_tools",
        "run_agent",
        "toolsets",
    }
)

BREAK = "BREAK"
UNKNOWN = "UNKNOWN"
INFO = "INFO"


class _HasDefault:
    """Stands in for a default whose real value was never evaluated.

    Only *whether* a default exists affects binding, so the AST reader
    never evaluates the expression (that would mean importing the
    module). Rendering it as ``...`` keeps the printed signature honest:
    a literal ``None`` here would read as "the default is None".
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "..."


HAS_DEFAULT = _HasDefault()


# --------------------------------------------------------------------------
# git / source access
# --------------------------------------------------------------------------


def _git(*args: str) -> str:
    """Run a git command in the repo and return stdout, or raise.

    Bytes in, explicit UTF-8 out. ``text=True`` would decode with the
    *locale* codec, which on Windows is cp1252 -- and this repo's source
    is full of non-ASCII (arrows in docstrings, box-drawing in banners).
    A cp1252 decode of that raises inside subprocess's reader thread, the
    call reports failure, and the caller concludes the file is missing.
    """
    proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout.decode("utf-8", "replace")


class SourceTree:
    """Reads files either from the working tree or from a git revision.

    ``rev=None`` means the working tree -- that is deliberately the
    default, because the question "is the fork broken *now*" must be
    answerable without any git state at all (a dirty tree included).
    """

    def __init__(self, rev: str | None = None, root: Path | None = None) -> None:
        self.rev = rev
        self.root = root or REPO_ROOT
        self._cache: dict[str, str | None] = {}

    @property
    def label(self) -> str:
        """Human-readable name. Display only -- see ``cache_key``."""
        return self.rev or "working tree"

    @property
    def cache_key(self) -> str:
        """Identity for caching. Must include the root.

        ``label`` is not enough: two trees can both be ``rev=None`` and
        differ only by root, which collides every cache entry between
        them and makes a diff of two worktrees silently report no
        changes at all.
        """
        return f"{self.rev or '<worktree>'}@{self.root}"

    def read(self, relpath: str) -> str | None:
        """Return file contents, or None when the file does not exist."""
        if relpath in self._cache:
            return self._cache[relpath]
        text: str | None
        if self.rev is None:
            path = self.root / relpath
            text = path.read_text(encoding="utf-8") if path.is_file() else None
        else:
            # Bytes, then explicit UTF-8 -- see _git for why text=True is a trap.
            proc = subprocess.run(
                ["git", "show", f"{self.rev}:{relpath}"],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            text = (
                proc.stdout.decode("utf-8", "replace")
                if proc.returncode == 0
                else None
            )
        self._cache[relpath] = text
        return text


# --------------------------------------------------------------------------
# module resolution
# --------------------------------------------------------------------------


def is_first_party(module: str) -> bool:
    root = module.split(".", 1)[0]
    return root in FIRST_PARTY_ROOTS or module in FIRST_PARTY_MODULES


def module_candidates(module: str) -> list[str]:
    """Candidate repo-relative paths for a dotted module name."""
    parts = module.split(".")
    return ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]


def resolve_module_source(module: str, tree: SourceTree) -> tuple[str, str] | None:
    """Return (relpath, source) for a first-party module, or None."""
    for relpath in module_candidates(module):
        text = tree.read(relpath)
        if text is not None:
            return relpath, text
    return None


def resolve_relative(module: str | None, level: int, containing: str) -> str | None:
    """Resolve a relative import to an absolute dotted module name.

    ``containing`` is the repo-relative path of the importing file.
    ``level`` is the number of leading dots.
    """
    pkg_parts = Path(containing).parts[:-1]  # drop the filename
    if level > len(pkg_parts) + 1:
        return None
    base = list(pkg_parts[: len(pkg_parts) - (level - 1)])
    if module:
        base.extend(module.split("."))
    return ".".join(base) if base else None


# --------------------------------------------------------------------------
# import index
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedName:
    """A local name bound to a first-party module attribute or module."""

    module: str
    attr: str | None  # None => the name is a module alias
    lineno: int


class ImportIndex:
    """Maps local names to first-party modules for one source file.

    Walks the WHOLE tree, not just ``tree.body``. The break this tool
    exists to catch is reached through a *function-scoped* import
    (``agent/copilot_acp_client.py`` imports ``_run_approval_gate``
    inside the permission handler), so a module-level-only index would
    report the file as calling nothing at all.

    Scoping is deliberately flattened: a name imported anywhere in the
    file is treated as visible everywhere in it. That can only ever
    over-report, and an over-report is a human glancing at a line
    number -- whereas under-reporting is the exact failure mode that
    let the original break ship.
    """

    def __init__(self, relpath: str, tree: ast.AST) -> None:
        self.relpath = relpath
        self.names: dict[str, ImportedName] = {}
        self._walk(tree)

    def _walk(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self._add_from(node)
            elif isinstance(node, ast.Import):
                self._add_plain(node)

    def _add_from(self, node: ast.ImportFrom) -> None:
        if node.level:
            module = resolve_relative(node.module, node.level, self.relpath)
        else:
            module = node.module
        if not module or not is_first_party(module):
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.names[local] = ImportedName(module, alias.name, node.lineno)

    def _add_plain(self, node: ast.Import) -> None:
        for alias in node.names:
            if not is_first_party(alias.name):
                continue
            # `import a.b.c` binds `a`; only an asname gives a usable handle
            # on the leaf module, so plain dotted imports are skipped.
            if alias.asname:
                self.names[alias.asname] = ImportedName(alias.name, None, node.lineno)
            elif "." not in alias.name:
                self.names[alias.name] = ImportedName(alias.name, None, node.lineno)

    def lookup_call(self, func: ast.expr) -> tuple[str, str] | None:
        """Resolve a call's ``func`` node to (module, attribute)."""
        if isinstance(func, ast.Name):
            entry = self.names.get(func.id)
            if entry and entry.attr:
                return entry.module, entry.attr
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            entry = self.names.get(func.value.id)
            if entry and entry.attr is None:
                return entry.module, func.attr
        return None


# --------------------------------------------------------------------------
# call sites
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    caller_path: str
    lineno: int
    module: str
    attr: str
    positional: int
    keywords: tuple[str, ...]
    star_args: bool  # call passes *something
    star_kwargs: bool  # call passes **something

    @property
    def target(self) -> str:
        return f"{self.module}.{self.attr}"

    @property
    def where(self) -> str:
        return f"{self.caller_path}:{self.lineno}"


def collect_call_sites(relpath: str, source: str) -> list[CallSite]:
    """Find every call into a first-party helper in one file."""
    try:
        tree = ast.parse(source, filename=relpath)
    except SyntaxError:
        return []
    index = ImportIndex(relpath, tree)
    if not index.names:
        return []

    sites: list[CallSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = index.lookup_call(node.func)
        if resolved is None:
            continue
        module, attr = resolved

        positional = sum(1 for a in node.args if not isinstance(a, ast.Starred))
        star_args = any(isinstance(a, ast.Starred) for a in node.args)
        keywords = tuple(k.arg for k in node.keywords if k.arg is not None)
        star_kwargs = any(k.arg is None for k in node.keywords)

        sites.append(
            CallSite(
                caller_path=relpath,
                lineno=node.lineno,
                module=module,
                attr=attr,
                positional=positional,
                keywords=keywords,
                star_args=star_args,
                star_kwargs=star_kwargs,
            )
        )
    return sites


# --------------------------------------------------------------------------
# signature reconstruction
# --------------------------------------------------------------------------


@dataclass
class TargetSignature:
    signature: inspect.Signature
    decorators: tuple[str, ...]
    kind: str  # "function" | "class"


def _decorator_names(node: ast.AST) -> tuple[str, ...]:
    out: list[str] = []
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        try:
            out.append(ast.unparse(target))
        except Exception:  # pragma: no cover - unparse is total in 3.9+
            out.append("<decorator>")
    return tuple(out)


def signature_from_arguments(
    args: ast.arguments, *, drop_first: bool = False
) -> inspect.Signature:
    """Convert an ``ast.arguments`` node into a real ``inspect.Signature``.

    Building a genuine Signature (rather than hand-rolling the binding
    rules) means ``Signature.bind`` decides what is legal -- so
    positional-only markers, keyword-only markers, defaults and varargs
    all behave exactly as the interpreter would at runtime.

    Annotations and default *values* are discarded: only arity, names,
    kinds and whether-a-default-exists affect binding, and evaluating
    real defaults would mean importing the module.
    """
    P = inspect.Parameter
    params: list[inspect.Parameter] = []

    posonly = list(args.posonlyargs)
    normal = list(args.args)
    positional = posonly + normal

    # `defaults` fills the tail of posonly+args.
    n_defaults = len(args.defaults)
    first_defaulted = len(positional) - n_defaults

    for i, arg in enumerate(positional):
        kind = P.POSITIONAL_ONLY if i < len(posonly) else P.POSITIONAL_OR_KEYWORD
        default = P.empty if i < first_defaulted else HAS_DEFAULT
        params.append(P(arg.arg, kind, default=default))

    if args.vararg:
        params.append(P(args.vararg.arg, P.VAR_POSITIONAL))

    for arg, default_node in zip(args.kwonlyargs, args.kw_defaults):
        default = P.empty if default_node is None else HAS_DEFAULT
        params.append(P(arg.arg, P.KEYWORD_ONLY, default=default))

    if args.kwarg:
        params.append(P(args.kwarg.arg, P.VAR_KEYWORD))

    if drop_first and params and params[0].kind in (
        P.POSITIONAL_ONLY,
        P.POSITIONAL_OR_KEYWORD,
    ):
        params = params[1:]

    return inspect.Signature(params)


def find_target(source: str, relpath: str, attr: str) -> TargetSignature | None:
    """Locate a module-level function or class and build its signature."""
    try:
        tree = ast.parse(source, filename=relpath)
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == attr:
            return TargetSignature(
                signature=signature_from_arguments(node.args),
                decorators=_decorator_names(node),
                kind="function",
            )
        if isinstance(node, ast.ClassDef) and node.name == attr:
            for sub in node.body:
                if (
                    isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == "__init__"
                ):
                    return TargetSignature(
                        signature=signature_from_arguments(sub.args, drop_first=True),
                        decorators=_decorator_names(node),
                        kind="class",
                    )
            # No __init__ of its own: inherited, so arity is unknowable here.
            return None
    return None


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str
    site: CallSite
    message: str
    detail: str = ""

    def render(self) -> str:
        head = f"{self.severity:<7} {self.site.where}  {self.site.target}"
        body = f"\n          {self.message}"
        tail = f"\n          {self.detail}" if self.detail else ""
        return head + body + tail


def _positional_names(sig: inspect.Signature) -> list[str]:
    P = inspect.Parameter
    return [
        p.name
        for p in sig.parameters.values()
        if p.kind in (P.POSITIONAL_ONLY, P.POSITIONAL_OR_KEYWORD)
    ]


def _has_var_keyword(sig: inspect.Signature) -> bool:
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def check_binding(site: CallSite, target: TargetSignature) -> Finding | None:
    """Ask CPython's binder whether this call still fits this signature."""
    sig = target.signature

    # A `**kwargs` splat at the call site hides which names are supplied,
    # so a missing-required-argument verdict cannot be proven. Only the
    # inverse is still provable: a name we pass EXPLICITLY that the
    # target no longer accepts.
    if site.star_kwargs or site.star_args:
        if not _has_var_keyword(sig):
            unknown = [k for k in site.keywords if k not in sig.parameters]
            if unknown:
                return Finding(
                    BREAK,
                    site,
                    f"passes keyword(s) the target does not accept: {', '.join(sorted(unknown))}",
                    f"signature: {sig}",
                )
        return None

    args = [object()] * site.positional
    kwargs = {name: object() for name in site.keywords}
    try:
        sig.bind(*args, **kwargs)
    except TypeError as exc:
        return Finding(
            BREAK,
            site,
            str(exc),
            f"signature: {sig}",
        )
    return None


def check_positional_drift(
    site: CallSite, before: TargetSignature, after: TargetSignature
) -> Finding | None:
    """Catch a silent reorder of parameters we pass positionally.

    ``Signature.bind`` is blind to this: swapping two same-arity
    parameters still binds, and the call quietly means something else.
    Only reachable in drift mode, where both revisions are available.
    """
    if site.positional == 0 or site.star_args:
        return None
    old = _positional_names(before.signature)[: site.positional]
    new = _positional_names(after.signature)[: site.positional]
    if old and new and old != new:
        return Finding(
            BREAK,
            site,
            "positional parameters were reordered or renamed under a call "
            "that passes them positionally -- binds fine, means something else",
            f"was ({', '.join(old)}) -> now ({', '.join(new)})",
        )
    return None


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def fork_files(base: str, head: str = "HEAD") -> list[str]:
    """Python files the fork touches, relative to a merge base."""
    merge_base = _git("merge-base", head, base).strip()
    out = _git("diff", "--name-only", f"{merge_base}..{head}")
    return sorted(
        p
        for p in out.splitlines()
        if p.endswith(".py") and not p.startswith("tests/")
    )


def gather_call_sites(paths: Iterable[str], tree: SourceTree) -> list[CallSite]:
    sites: list[CallSite] = []
    for relpath in paths:
        source = tree.read(relpath)
        if source is None:
            continue
        sites.extend(collect_call_sites(relpath, source))
    return sites


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    resolved: int = 0

    @property
    def breaks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BREAK]


# Verdicts from the three-way resolution below.
OWNED_UPSTREAM = "upstream"  # upstream changed it -- diff against ours
OWNED_FORK = "fork"  # we changed or added it -- ours survives the rebase
DELETED = "deleted"  # upstream removed a helper we call
CONFLICT = "conflict"  # both sides changed the same signature


@dataclass
class Resolution:
    """What a call site will actually bind against after a rebase."""

    effective: TargetSignature | None
    baseline: TargetSignature | None  # None => nothing to diff against
    verdict: str


def resolve_effective(
    upstream: TargetSignature | None,
    ours: TargetSignature | None,
    base: TargetSignature | None,
) -> Resolution:
    """Apply git's own three-way merge rule to a single signature.

    A rebase does not simply adopt upstream's version of everything --
    it keeps our hunks too. So "what does this call bind against
    afterwards" depends on *which side changed it* relative to the
    merge base, not merely on what upstream currently has.
    """
    if upstream is None:
        if ours is None:
            return Resolution(None, None, OWNED_UPSTREAM)
        # Gone upstream. Only a deletion if upstream ever had it;
        # otherwise it is a fork addition that rebases across with us.
        if base is not None:
            return Resolution(ours, ours, DELETED)
        return Resolution(ours, None, OWNED_FORK)

    if ours is None or base is None:
        # Nothing of ours to preserve, or brand new upstream.
        return Resolution(upstream, None, OWNED_UPSTREAM)

    ours_changed = str(ours.signature) != str(base.signature)
    upstream_changed = str(upstream.signature) != str(base.signature)

    if ours_changed and upstream_changed:
        return Resolution(upstream, ours, CONFLICT)
    if ours_changed:
        # Our hunk wins the rebase; we cannot drift from ourselves.
        return Resolution(ours, None, OWNED_FORK)
    return Resolution(upstream, ours, OWNED_UPSTREAM)


def analyse(
    sites: Iterable[CallSite],
    tree: SourceTree,
    *,
    base_tree: SourceTree | None = None,
    merge_base_tree: SourceTree | None = None,
) -> Report:
    """Bind every call site against ``tree``; diff against ``base_tree``.

    In drift mode the question is not "does this symbol exist at
    ``tree``" but "after rebasing our fork onto ``tree``, will this call
    still bind" -- and a rebase carries our own additions across. So a
    symbol missing from ``tree`` is resolved three ways, exactly as git
    resolves a merge:

    ``tree`` has it
        Upstream owns it. Bind against that; any change is real drift.
    ``tree`` lacks it, the merge base HAS it
        Upstream deleted it out from under us. BREAK.
    ``tree`` lacks it, the merge base lacks it too
        We added it. It survives the rebase unchanged, so bind against
        our copy -- we cannot drift from ourselves.

    Without the merge-base arm, every fork-only helper reads as
    "deleted upstream" and the report is pure noise. ``merge_base_tree``
    defaults to ``base_tree`` so a plain two-revision A-to-B comparison
    still behaves as you would expect.
    """
    report = Report()
    sig_cache: dict[tuple[str, str, str], TargetSignature | None] = {}
    ancestor = merge_base_tree if merge_base_tree is not None else base_tree

    def signature_for(
        module: str, attr: str, src: SourceTree
    ) -> TargetSignature | None:
        key = (src.cache_key, module, attr)
        if key not in sig_cache:
            found = resolve_module_source(module, src)
            if found is None:
                sig_cache[key] = None
            else:
                relpath, source = found
                sig_cache[key] = find_target(source, relpath, attr)
        return sig_cache[key]

    for site in sites:
        report.checked += 1
        at_tree = signature_for(site.module, site.attr, tree)

        if base_tree is None:
            # Validate mode: one revision, nothing to merge.
            resolution = Resolution(at_tree, None, OWNED_UPSTREAM)
        else:
            resolution = resolve_effective(
                at_tree,
                signature_for(site.module, site.attr, base_tree),
                signature_for(site.module, site.attr, ancestor)
                if ancestor is not None
                else None,
            )

        if resolution.verdict == DELETED:
            report.findings.append(
                Finding(
                    BREAK,
                    site,
                    f"target no longer exists at {tree.label}",
                    f"was: {resolution.baseline.signature}",
                )
            )
            continue

        target = resolution.effective
        if target is None:
            # Unresolvable statically (re-export, conditional def, C
            # extension). Silence here is what keeps validate mode
            # usable as a test.
            continue

        report.resolved += 1
        finding = check_binding(site, target)
        if finding is not None:
            report.findings.append(finding)
            continue

        if resolution.verdict == CONFLICT:
            report.findings.append(
                Finding(
                    BREAK,
                    site,
                    "both the fork and upstream changed this signature -- "
                    "the rebase will need a human",
                    f"ours: {resolution.baseline.signature}\n"
                    f"          theirs: {target.signature}",
                )
            )
            continue

        previously = resolution.baseline
        if previously is None:
            continue
        reorder = check_positional_drift(site, previously, target)
        if reorder is not None:
            report.findings.append(reorder)
        elif str(previously.signature) != str(target.signature):
            report.findings.append(
                Finding(
                    INFO,
                    site,
                    "signature changed but the call still binds",
                    f"{previously.signature}\n          -> {target.signature}",
                )
            )
    return report


def iter_unresolved(sites: Iterable[CallSite]) -> Iterator[CallSite]:
    for site in sites:
        if site.star_kwargs or site.star_args:
            yield site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect signature drift between fork call sites and upstream helpers.",
    )
    parser.add_argument(
        "--against",
        metavar="REV",
        help="Also bind against this revision (e.g. upstream/main) to predict a pull.",
    )
    parser.add_argument(
        "--base",
        default="upstream/main",
        metavar="REV",
        help="Ref used to compute the merge base that defines 'fork files'.",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        metavar="REV",
        help="Fork tip (default HEAD).",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        metavar="PATH",
        help="Check these files instead of deriving them from the merge base.",
    )
    parser.add_argument(
        "--show-info",
        action="store_true",
        help="Print INFO findings (signature changed but the call still binds).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print findings only, no summary.",
    )
    opts = parser.parse_args(argv)

    if opts.paths:
        paths = list(opts.paths)
    else:
        try:
            paths = fork_files(opts.base, opts.head)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not paths:
        print("no fork files to check", file=sys.stderr)
        return 0

    working = SourceTree(None)
    sites = gather_call_sites(paths, working)

    if opts.against:
        against = SourceTree(opts.against)
        # The merge base is what separates "upstream deleted this" from
        # "we added this" -- without it every fork-only helper reports
        # as a break. See analyse().
        try:
            merge_base = _git("merge-base", opts.head, opts.against).strip()
            ancestor = SourceTree(merge_base)
        except RuntimeError:
            ancestor = None
        report = analyse(
            sites, against, base_tree=working, merge_base_tree=ancestor
        )
        headline = f"binding fork call sites against {opts.against}"
        if ancestor is not None:
            headline += f" (merge base {merge_base[:11]})"
    else:
        report = analyse(sites, working)
        headline = "binding fork call sites against the working tree"

    if not opts.quiet:
        print(headline)
        print(
            f"  {len(paths)} fork file(s), {report.checked} first-party call(s), "
            f"{report.resolved} resolved to a signature"
        )
        unresolved = list(iter_unresolved(sites))
        if unresolved:
            print(
                f"  {len(unresolved)} call(s) splat *args/**kwargs -- "
                "arity not statically provable"
            )
        print()

    shown = [
        f for f in report.findings if opts.show_info or f.severity != INFO
    ]
    for finding in shown:
        print(finding.render())
        print()

    breaks = report.breaks
    if not opts.quiet:
        if breaks:
            print(f"{len(breaks)} BREAK finding(s)")
        else:
            print("no breaks")
    return 1 if breaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
