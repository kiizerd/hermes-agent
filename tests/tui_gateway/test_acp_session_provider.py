"""``_session_provider`` must not stringify a missing override provider to "None".

Regression cover for the desktop's permission-mode / bridge-mode pills going
silently inert: ``session.create`` omits the ``provider`` param whenever the
desktop's provider selection is empty at creation time (it only ships
``provider`` when both ``model`` AND ``provider`` are set -- see
``desktopSessionCreateParams`` in the desktop app), so
``session["model_override"]`` can legitimately be ``{"model": ..., "provider":
None}``. The old code did ``str((override or {}).get("provider") if
isinstance(override, dict) else "")`` -- for a dict override with
``provider=None`` that evaluates to ``str(None)`` == the 4-char string
``"None"``, which is truthy and therefore skipped the ``if not provider``
fallback to the live agent's real provider a few lines below. Every
Claude-over-ACP session hitting this shape got permanently misidentified as
non-Claude, and ``handle_acp_config_set`` rejected `permission_mode` /
`acp_system_prompt_mode` writes with 4002 -- the composer pill accepted the
click but the pin never reached the backend.
"""

from __future__ import annotations

from tui_gateway.acp_session_modes import _session_provider


class _StubAgent:
    def __init__(self, provider: str) -> None:
        self.provider = provider


def test_provider_none_in_override_falls_back_to_agent() -> None:
    """model_override present, but its provider key is explicitly None."""
    session = {
        "model_override": {"model": "sonnet", "provider": None},
        "agent": _StubAgent("copilot-acp"),
    }
    assert _session_provider(session) == "copilot-acp"


def test_provider_missing_key_in_override_falls_back_to_agent() -> None:
    """model_override present, provider key entirely absent."""
    session = {
        "model_override": {"model": "sonnet"},
        "agent": _StubAgent("copilot-acp"),
    }
    assert _session_provider(session) == "copilot-acp"


def test_no_override_falls_back_to_agent() -> None:
    session = {"model_override": None, "agent": _StubAgent("copilot-acp")}
    assert _session_provider(session) == "copilot-acp"


def test_override_provider_wins_over_agent() -> None:
    session = {
        "model_override": {"model": "gpt-5", "provider": "openai"},
        "agent": _StubAgent("copilot-acp"),
    }
    assert _session_provider(session) == "openai"


def test_no_agent_no_override_is_empty() -> None:
    assert _session_provider({}) == ""
    assert _session_provider({"agent": None, "model_override": None}) == ""
