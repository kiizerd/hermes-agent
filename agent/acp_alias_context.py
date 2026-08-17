"""Context windows for bare Claude Code model aliases on ACP providers.

FORK-ONLY MODULE. Upstream has no file at this path, so it can never produce a
rebase conflict. It exists to keep this table out of ``agent/model_metadata.py``
-- 105 upstream commits in the 90 days before this extraction, most of them
appending to ``DEFAULT_CONTEXT_LENGTHS``, which is precisely where the fork's
lines used to sit.

The split rule this follows: code that is purely ADDITIVE to upstream moves into
a fork-only file; code that MODIFIES upstream's own logic stays where it is.
Everything here is additive -- a lookup table, a provider set, and a resolver
upstream has no concept of. The one thing that cannot move is the *call site*
inside ``get_model_context_length()``, which stays behind as step 5a0.

The bug this fixes
------------------
The ``copilot-acp`` provider spawns whatever ``HERMES_COPILOT_ACP_COMMAND``
points at; locally that is claude-agent-acp, whose ``session/new`` configOptions
advertise short aliases rather than raw API ids (see
``hermes_cli/models.py::_PROVIDER_MODELS["copilot-acp"]``). They carry no vendor
prefix, so the step-8 fuzzy match over ``DEFAULT_CONTEXT_LENGTHS`` misses every
one of them and they land on the 256K hard fallback -- under-reporting a 1M
window by ~4x on the context gauge, and making the compressor summarize roughly
three quarters of a conversation early.

Why these keys are NOT in ``DEFAULT_CONTEXT_LENGTHS``
----------------------------------------------------
That dict is matched as unanchored substrings across every provider, sorted
longest key first. ``"sonnet"`` and ``"haiku"`` tie with the ``"claude"``
catch-all at six characters, so a bare ``"sonnet"`` key could win that tie and
promote every older Sonnet on every provider to 1M. Here the keys are matched
EXACTLY and only for ACP providers, so there is no shared namespace to poison.

This invariant is enforced by
``tests/agent/test_acp_claude_alias_context.py`` -- do not rely on a comment in
``model_metadata.py`` to hold it.
"""

from typing import Dict, Optional

# An alias tracks whatever the CLI currently points it at, so these are
# floor-of-the-family values, not per-snapshot facts:
#   opus / sonnet  -> Claude 5 generation, 1M
#   haiku          -> Haiku 4.5 is 200K; deliberately NOT 1M, because
#                     over-reporting lets the conversation grow past the real
#                     window and the API rejects the turn. Under-reporting only
#                     compresses early.
#   opusplan       -> Opus for planning, Sonnet for execution; both 1M.
ACP_CLAUDE_ALIAS_CONTEXT: Dict[str, int] = {
    "opus": 1_000_000,
    "opusplan": 1_000_000,
    "sonnet": 1_000_000,
    "haiku": 200_000,
}

# Providers whose model names may be bare Claude Code aliases.
ACP_CLAUDE_ALIAS_PROVIDERS = frozenset({"copilot-acp", "claude-acp", "claude-code-acp"})


def resolve_acp_claude_alias_context(model: str, provider: str) -> Optional[int]:
    """Context window for a bare Claude Code alias on an ACP provider.

    Returns ``None`` for anything that is not an exact alias match, so a raw
    API id (``claude-opus-4-8``) or a suffixed one (``claude-fable-5[1m]``)
    keeps flowing through the normal resolution chain.

    Deliberately does no logging: the caller owns that, so the log record's
    module stays ``agent.model_metadata`` where anyone grepping agent.log for
    context-resolution already looks.
    """
    if (provider or "").strip().lower() not in ACP_CLAUDE_ALIAS_PROVIDERS:
        return None
    return ACP_CLAUDE_ALIAS_CONTEXT.get((model or "").strip().lower())
