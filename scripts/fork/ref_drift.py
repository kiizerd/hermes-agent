#!/usr/bin/env python3
"""Post-rebase reference-drift check for the fork knowledge base.

`docs/fork/*.md` is dense with two kinds of pointer, and a rebase breaks both
without touching a single character of the docs:

1. ``path/to/file.py:123`` refs into upstream code. Upstream inserts a hundred
   lines above a function and every ref below it now names an unrelated line.
   The ref still *resolves*, which is why eyeballing misses it.
2. ``### <sha>`` ledger headings. A rebase rewrites every fork commit, so the
   heading names an object unreachable from HEAD and absent from a fresh clone.

Neither shows up in a diff, a test run, or a lint pass. The only prior defence
was remembering to re-walk them by hand.

WHAT GATES, AND WHAT ONLY ADVISES
---------------------------------
The default checks are exact, and each one is a fact about the repo rather than
a guess about intent:

* a ref names a file that does not exist;
* a ref names a line past the end of that file (this is what catches a bare
  ``:1027`` continuation ref that silently inherited the wrong filename);
* a ``### <sha>`` names a commit unreachable from HEAD, or no commit at all.

``--anchors`` adds a heuristic pass: the doc usually names its target in
backticks in the same paragraph, so it checks whether any of those identifiers
appears near the line the ref points at. It is useful for a manual sweep and
useless as a gate -- on the 2026-08-26 rebase it produced 27 false positives
against 3 real finds, because a paragraph routinely names the Hermes-side
function while the ref points at its codex-side counterpart. Off by default for
exactly that reason.

Nothing here is verification. A clean run means no ref is provably broken; it
cannot tell you a ref that still resolves now points at the wrong function.
Advancing the `Last verified` marker in changes.md still means reading them.

Exit 1 on any exact failure (plus anchor misses when ``--anchors`` is passed).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "fork"

# path/to/file.ext:123, file.ext:12,34, file.ext:12-34
REF = re.compile(
    r"(?P<path>[A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|mjs|cjs|yaml|yml|json|sh|ps1))"
    r":(?P<lines>\d+(?:[,\-]\d+)*)"
)
# `:1799` -- continuation ref, inherits the last file named in the doc
BARE = re.compile(r"`:(?P<lines>\d+(?:[,\-]\d+)*)`")
# `_acp_config` / `EXPOSED_TOOLS` / `foo()` -- candidate anchors
IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})(?:\(\))?`")
SHA = re.compile(r"\b([0-9a-f]{9,40})\b")

# Not in this repo: the adapter bundle we read line numbers out of, which the
# docs already flag as "re-check after an agent upgrade".
EXTERNAL_SUFFIXES = ("acp-agent.js",)

ANCHOR_WINDOW = 4


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def paragraph_bounds(lines: list[str], i: int) -> tuple[int, int]:
    """Blank-line-delimited paragraph around doc line index ``i``."""
    start = i
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = i
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return start, end


def check_refs(
    verbose: bool = False,
    anchors_on: bool = False,
    docs: Path | None = None,
    root: Path | None = None,
) -> list[str]:
    """``docs``/``root`` are injectable so tests can drive a synthetic tree."""
    docs = docs or DOCS
    root = root or ROOT
    problems: list[str] = []
    for doc in sorted(docs.glob("*.md")):
        lines = doc.read_text(encoding="utf-8").splitlines()
        last_path: str | None = None
        for i, line in enumerate(lines):
            named = list(REF.finditer(line))
            hits: list[tuple[str, str]] = [
                (m.group("path"), m.group("lines")) for m in named
            ]
            if named:
                last_path = named[-1].group("path")
            # Bare refs are collected even on a line that also names a file:
            # `foo.py:20` and `:1027` routinely share one sentence, and an
            # `elif` here silently drops the second one.
            if last_path:
                hits += [(last_path, m.group("lines")) for m in BARE.finditer(line)]

            if not hits:
                continue

            start, end = paragraph_bounds(lines, i)
            anchors = {
                m.group(1)
                for para_line in lines[start:end + 1]
                for m in IDENT.finditer(para_line)
            }

            for path_str, spec in hits:
                if path_str.endswith(EXTERNAL_SUFFIXES):
                    continue
                target = root / path_str
                if not target.is_file():
                    matches = [
                        h for h in root.rglob(path_str)
                        if ".git" not in h.parts and "node_modules" not in h.parts
                    ]
                    if not matches:
                        problems.append(f"{doc.name}:{i + 1}  MISSING FILE  {path_str}")
                        continue
                    target = matches[0]

                try:
                    src = target.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError as exc:
                    problems.append(f"{doc.name}:{i + 1}  UNREADABLE  {path_str}: {exc}")
                    continue

                nums = [int(n) for n in re.split(r"[,\-]", spec)]
                for n in nums:
                    if n > len(src):
                        problems.append(
                            f"{doc.name}:{i + 1}  OUT OF RANGE  {path_str}:{n} "
                            f"(file has {len(src)} lines)"
                        )
                        continue
                    if not anchors_on or not anchors:
                        continue
                    lo = max(0, n - 1 - ANCHOR_WINDOW)
                    hi = min(len(src), n + ANCHOR_WINDOW)
                    window = "\n".join(src[lo:hi])
                    if not any(a in window for a in anchors):
                        problems.append(
                            f"{doc.name}:{i + 1}  NO ANCHOR  {path_str}:{n} -> "
                            f"{src[n - 1].strip()[:60]!r} (expected one of: "
                            f"{', '.join(sorted(anchors)[:4])})"
                        )
                    elif verbose:
                        print(f"  ok  {doc.name}:{i + 1}  {path_str}:{n}")
    return problems


def check_shas(verbose: bool) -> list[str]:
    problems: list[str] = []
    mb = git("merge-base", "HEAD", "upstream/main").stdout.strip()
    if not mb:
        return ["cannot resolve merge-base with upstream/main -- is the remote fetched?"]

    fork = {
        line.split(" ", 1)[0]
        for line in git("log", "--format=%H", f"{mb}..HEAD").stdout.splitlines()
    }

    seen: dict[str, list[str]] = {}
    for doc in sorted(DOCS.glob("*.md")):
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            # only headings and inline-code SHAs; a bare hex run in prose is
            # usually a hash, a socket name, or a regex fixture
            for m in SHA.finditer(line):
                tok = m.group(1)
                if tok.isdigit():
                    continue
                if f"`{tok}`" not in line:
                    continue
                seen.setdefault(tok, []).append(f"{doc.name}:{i}")

    for tok, sites in sorted(seen.items()):
        r = git("rev-parse", "--verify", "--quiet", f"{tok}^{{commit}}")
        if r.returncode != 0:
            problems.append(f"{sites[0]}  UNKNOWN COMMIT  {tok}")
            continue
        full = r.stdout.strip()
        if full in fork:
            if verbose:
                print(f"  ok  {sites[0]}  {tok} (fork)")
            continue
        if git("merge-base", "--is-ancestor", full, "HEAD").returncode == 0:
            if verbose:
                print(f"  ok  {sites[0]}  {tok} (upstream)")
            continue
        subj = git("log", "-1", "--format=%s", full).stdout.strip()
        problems.append(
            f"{sites[0]}  DANGLING  {tok} — {subj[:60]!r}; re-key by subject"
        )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", "-v", action="store_true", help="print passing refs too")
    ap.add_argument("--refs-only", action="store_true")
    ap.add_argument("--shas-only", action="store_true")
    ap.add_argument(
        "--anchors", action="store_true",
        help="also flag refs whose paragraph names a symbol that is not near the "
             "target line. Noisy — a manual-sweep aid, not a gate.",
    )
    args = ap.parse_args()

    problems: list[str] = []
    if not args.shas_only:
        problems += check_refs(args.verbose, args.anchors)
    if not args.refs_only:
        problems += check_shas(args.verbose)

    if problems:
        print(f"{len(problems)} drifted reference(s):\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\nRe-point each ref by locating the symbol its paragraph names, "
            "not by applying an offset — the shift is not uniform across a file."
        )
        return 1

    print("docs/fork: every ref resolves, every SHA is reachable from HEAD")
    print("(this does not prove a ref points at the right function — read them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
