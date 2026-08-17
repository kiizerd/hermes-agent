"""``permission_mode_state()`` never publishes a value the UI has no widget for.

The composer's permission dropdown is a radio group keyed on exact mode ids: it
paints ``value`` as the selected option and every entry of ``options`` as a
choice. So the two have to agree. When they don't -- ``copilot_acp.permission_mode``
holding an id this agent does not advertise, say -- the menu renders with nothing
selected and a label the user cannot pick back to, which reads as "the mode I
want is missing" rather than "the mode I asked for was rejected".

``_select_acp_mode`` already handles the wire side correctly: it logs the
unadvertised id and leaves the agent on its own mode. The contract asserted here
is that the STATE published to the UI says the same thing.
"""

from __future__ import annotations

import pytest

from agent.copilot_acp_client import CopilotACPClient

_ADVERTISED = ["default", "plan", "acceptEdits", "bypassPermissions"]

_LIVE_SESSION = {
    "modes": {
        "availableModes": [{"id": mode} for mode in _ADVERTISED],
        "currentModeId": "default",
    }
}


@pytest.fixture(autouse=True)
def _no_env_pin(monkeypatch):
    """An env pin outranks config and would short-circuit every case here."""
    monkeypatch.delenv("HERMES_ACP_PERMISSION_MODE", raising=False)


def _client(tmp_path, monkeypatch, configured: str):
    monkeypatch.setattr(
        "agent.copilot_acp_client._acp_config_str",
        lambda key, default="": configured if key == "permission_mode" else default,
    )

    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="claude-agent-acp",
        acp_args=[],
        acp_cwd=str(tmp_path),
    )


def test_unadvertised_mode_reports_what_the_agent_is_actually_on(tmp_path, monkeypatch):
    """``auto`` is not an ACP mode id; claude-agent-acp advertises four, none of them that."""
    client = _client(tmp_path, monkeypatch, "auto")
    client._session_info = _LIVE_SESSION

    state = client.permission_mode_state()

    assert state["value"] == "default"
    assert state["source"] == "agent"


def test_a_case_variant_normalizes_to_the_advertised_id(tmp_path, monkeypatch):
    """``_select_acp_mode`` matches case-insensitively, so this WAS applied.

    Reporting the user's spelling back would still have painted an unselected
    menu -- the radio group compares ids exactly.
    """
    client = _client(tmp_path, monkeypatch, "AcceptEdits")
    client._session_info = _LIVE_SESSION

    state = client.permission_mode_state()

    assert state["value"] == "acceptEdits"
    assert state["source"] == "config"


def test_an_advertised_mode_passes_through(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, "plan")
    client._session_info = _LIVE_SESSION

    state = client.permission_mode_state()

    assert state["value"] == "plan"
    assert state["source"] == "config"


@pytest.mark.parametrize("configured", ["auto", "AcceptEdits", "plan", ""])
def test_value_is_always_selectable_or_empty(tmp_path, monkeypatch, configured):
    """The invariant the dropdown depends on, across every input shape."""
    client = _client(tmp_path, monkeypatch, configured)
    client._session_info = _LIVE_SESSION

    state = client.permission_mode_state()

    assert state["value"] in set(state["options"]) | {""}


def test_without_a_live_session_the_configured_value_is_reported_verbatim(
    tmp_path, monkeypatch
):
    """Nothing to reconcile against, and nothing to lie about.

    This is the draft pill, which is read-only anyway: it reports what the next
    chat is configured to start on, so echoing a bad config value back is the
    honest answer rather than a guess at what some future agent would do.
    """
    client = _client(tmp_path, monkeypatch, "auto")

    state = client.permission_mode_state()

    assert state["value"] == "auto"
    assert state["advertised"] is False
