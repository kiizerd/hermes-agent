"""Context-window resolution for bare Claude Code aliases on ACP providers.

The ``copilot-acp`` provider advertises claude-agent-acp's short aliases
("opus", "sonnet", "haiku") rather than raw API ids. Those carry no vendor
prefix, so the fuzzy DEFAULT_CONTEXT_LENGTHS match misses them entirely and
they used to land on the 256K hard fallback while the real window is 1M.

These tests assert the resolution *contract*, not literal token counts, so a
future model generation that moves the family window does not break them.
"""

import pytest

from agent.model_metadata import (
    DEFAULT_CONTEXT_LENGTHS,
    DEFAULT_FALLBACK_CONTEXT,
    _ACP_CLAUDE_ALIAS_CONTEXT,
    _ACP_CLAUDE_ALIAS_PROVIDERS,
    _resolve_acp_claude_alias_context,
    get_model_context_length,
)

ACP_BASE_URL = "acp://copilot"


def _resolve(model: str, provider: str = "copilot-acp") -> int:
    """Full-chain resolution. Alias hits return at step 5a0, before any I/O."""
    return get_model_context_length(
        model, base_url=ACP_BASE_URL, api_key="", provider=provider
    )


# ── the reported bug ────────────────────────────────────────────────────────

@pytest.mark.parametrize("alias", sorted(_ACP_CLAUDE_ALIAS_CONTEXT))
def test_alias_does_not_land_on_the_hard_fallback(alias):
    """Every alias must resolve from real metadata, not the 256K default."""
    assert _resolve(alias) != DEFAULT_FALLBACK_CONTEXT


@pytest.mark.parametrize("alias", sorted(_ACP_CLAUDE_ALIAS_CONTEXT))
def test_alias_resolution_matches_the_alias_table(alias):
    assert _resolve(alias) == _ACP_CLAUDE_ALIAS_CONTEXT[alias]


# ── relationships to the concrete catalog (not literal token counts) ────────

def test_frontier_aliases_match_their_concrete_generation():
    """"opus"/"sonnet" point at the current frontier models — same window."""
    opus = DEFAULT_CONTEXT_LENGTHS["claude-opus-5"]
    sonnet = DEFAULT_CONTEXT_LENGTHS["claude-sonnet-5"]
    assert _ACP_CLAUDE_ALIAS_CONTEXT["opus"] == opus
    assert _ACP_CLAUDE_ALIAS_CONTEXT["sonnet"] == sonnet


def test_opusplan_tracks_opus():
    """opusplan plans on Opus and executes on Sonnet; window is the shared one."""
    assert (
        _ACP_CLAUDE_ALIAS_CONTEXT["opusplan"]
        == _ACP_CLAUDE_ALIAS_CONTEXT["opus"]
        == _ACP_CLAUDE_ALIAS_CONTEXT["sonnet"]
    )


def test_haiku_is_not_promoted_to_the_frontier_window():
    """Over-reporting is the unsafe direction — the API rejects an oversized turn.

    Haiku sits on the older Claude catch-all, so it must stay below the
    frontier aliases rather than inheriting 1M.
    """
    assert _ACP_CLAUDE_ALIAS_CONTEXT["haiku"] == DEFAULT_CONTEXT_LENGTHS["claude"]
    assert _ACP_CLAUDE_ALIAS_CONTEXT["haiku"] < _ACP_CLAUDE_ALIAS_CONTEXT["opus"]


# ── the namespace hazard the table exists to avoid ──────────────────────────

@pytest.mark.parametrize("alias", sorted(_ACP_CLAUDE_ALIAS_CONTEXT))
def test_aliases_stay_out_of_the_global_fuzzy_catalog(alias):
    """DEFAULT_CONTEXT_LENGTHS is substring-matched across every provider.

    "sonnet" and "haiku" tie with the "claude" catch-all at 6 characters, so a
    bare key there could win that tie and promote every older Claude on every
    provider. Alias resolution is provider-scoped and exact-match precisely so
    this dict never has to carry them.
    """
    assert alias not in DEFAULT_CONTEXT_LENGTHS


def test_older_claude_slug_still_resolves_to_the_catch_all():
    """Regression guard for the tie described above."""
    assert (
        _resolve_acp_claude_alias_context("claude-sonnet-4.5", "copilot-acp") is None
    )


# ── scoping: exact match, ACP providers only ────────────────────────────────

@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "copilot", ""])
def test_aliases_do_not_leak_to_non_acp_providers(provider):
    assert _resolve_acp_claude_alias_context("sonnet", provider) is None


@pytest.mark.parametrize("provider", sorted(_ACP_CLAUDE_ALIAS_PROVIDERS))
def test_every_acp_provider_alias_resolves(provider):
    assert _resolve_acp_claude_alias_context("opus", provider) is not None


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-fable-5[1m]", "opus-x"])
def test_non_alias_models_flow_through_the_normal_chain(model):
    """Only exact aliases short-circuit; raw and suffixed ids resolve normally."""
    assert _resolve_acp_claude_alias_context(model, "copilot-acp") is None


@pytest.mark.parametrize("variant", ["OPUS", " opus ", "Sonnet"])
def test_alias_match_is_case_and_whitespace_tolerant(variant):
    assert _resolve_acp_claude_alias_context(variant, "copilot-acp") is not None


def test_raw_ids_advertised_by_the_provider_still_resolve_above_fallback():
    """The picker also offers raw ids; those must not regress to the fallback."""
    for model in ("claude-opus-4-8", "claude-fable-5[1m]"):
        assert _resolve(model) != DEFAULT_FALLBACK_CONTEXT


# ── contract with the model picker ──────────────────────────────────────────

def test_every_bare_alias_in_the_picker_has_a_context_entry():
    """Adding an alias to the picker without a context entry reintroduces the bug.

    Only vendor-prefixed ids are exempt — those resolve via the normal chain.
    """
    from hermes_cli.models import _PROVIDER_MODELS

    advertised = _PROVIDER_MODELS.get("copilot-acp", [])
    assert advertised, "copilot-acp must advertise at least one model"

    uncovered = [
        m
        for m in advertised
        if "claude" not in m.lower() and m.strip().lower() not in _ACP_CLAUDE_ALIAS_CONTEXT
    ]
    assert not uncovered, (
        f"bare alias(es) {uncovered} advertised by the copilot-acp picker have no "
        "_ACP_CLAUDE_ALIAS_CONTEXT entry and will fall back to "
        f"{DEFAULT_FALLBACK_CONTEXT:,} tokens"
    )
