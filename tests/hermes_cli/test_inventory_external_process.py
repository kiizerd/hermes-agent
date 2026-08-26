"""A configured subprocess-launched provider must survive the explicit filter.

``_filter_explicit_provider_rows`` narrows the picker to providers the user
explicitly configured. Its strict gate, ``is_provider_explicitly_configured``,
looks at ``auth.json`` active_provider, ``config.yaml`` model.provider, MoA
slots, and API-key env vars — none of which an ``external_process`` provider
touches. copilot-acp declares ``api_key_env_vars = ()`` and is configured by
pointing Hermes at a launch command, so the strict gate always says "no".

The only thing that used to keep the row alive was the ``slug == current_slug``
escape hatch, which makes the picker chicken-and-egg: it offers the provider
only once you are already on it. These tests pin the hatch that fixes it, and
pin that it does not degrade into a blanket opt-out.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _ctx(current_provider: str = "nous"):
    """A context whose current provider is deliberately NOT the ACP one.

    That is the whole point: with ``current_provider == "copilot-acp"`` the
    ``slug == current_slug`` hatch would keep the row regardless and the test
    would pass without exercising the fix.
    """
    from hermes_cli.inventory import ConfigContext

    return ConfigContext(
        current_provider=current_provider,
        current_model="z-ai/glm-5.2",
        current_base_url="",
        user_providers={},
        custom_providers=[],
        excluded_providers=[],
    )


def _rows():
    return [{"slug": "copilot-acp", "name": "Claude Sub ACP", "models": ["opus"]}]


def test_a_configured_external_process_provider_survives_the_explicit_filter(monkeypatch):
    import hermes_cli.auth as auth_module
    from hermes_cli.inventory import _filter_explicit_provider_rows

    # Precondition, asserted rather than assumed: the strict gate does NOT
    # recognise this provider. If upstream ever teaches it about
    # external_process, this fails loudly instead of passing for a new reason.
    assert not auth_module.is_provider_explicitly_configured("copilot-acp")

    monkeypatch.setattr(
        auth_module,
        "get_external_process_provider_status",
        lambda slug: {"configured": slug == "copilot-acp"},
    )

    kept = _filter_explicit_provider_rows(_rows(), _ctx())

    assert [r["slug"] for r in kept] == ["copilot-acp"]


def test_an_unconfigured_external_process_provider_is_still_dropped(monkeypatch):
    """Guard against the hatch being widened into a blanket opt-out."""
    import hermes_cli.auth as auth_module
    from hermes_cli.inventory import _filter_explicit_provider_rows

    monkeypatch.setattr(
        auth_module,
        "get_external_process_provider_status",
        lambda _slug: {"configured": False},
    )

    kept = _filter_explicit_provider_rows(_rows(), _ctx())

    assert kept == []


def test_the_hatch_does_not_resurrect_unrelated_providers(monkeypatch):
    """A non-ACP provider with no configuration must not ride along.

    ``get_external_process_provider_status`` self-gates on ``auth_type``, so a
    real call returns ``configured: False`` for these. Stubbing it to the real
    contract keeps the filter's other branches honest.
    """
    import hermes_cli.auth as auth_module
    from hermes_cli.inventory import _filter_explicit_provider_rows

    monkeypatch.setattr(
        auth_module,
        "get_external_process_provider_status",
        lambda slug: {"configured": slug == "copilot-acp"},
    )

    rows = _rows() + [{"slug": "deepseek", "name": "DeepSeek", "models": ["chat"]}]
    kept = _filter_explicit_provider_rows(rows, _ctx())

    assert [r["slug"] for r in kept] == ["copilot-acp"]
