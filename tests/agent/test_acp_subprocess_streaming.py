"""The subprocess ACP client must be asked to stream.

Counterpart to ``test_acp_provider_rails.py``, which pins that a *generic*
``acp://`` turn never requests a stream. That exclusion is correct for a client
that returns a whole completion, but it must not swallow ``copilot-acp``:
``CopilotACPClient`` yields OpenAI-style chunks as the remote agent produces
them, and ``create_openai_client`` routes only ``copilot-acp`` / ``acp://copilot``
to it (agent_runtime_helpers.py, ~line 2601).

Why this is a behaviour contract and not a preference: ``build_assistant_message``
suppresses ``reasoning_callback`` whenever stream consumers are registered, on
the assumption that streaming already displayed the reasoning. Turn streaming
off for this provider and nothing ever displays it — the desktop Thought pane
stays empty for every ACP turn, with no error anywhere.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _chunk(content: str | None = None, *, finish: str | None = None):
    """One OpenAI-shaped stream chunk, as CopilotACPClient emits them."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning=None, tool_calls=None),
                finish_reason=finish,
            )
        ],
        usage=None,
    )


class _RecordingCompletions:
    """Streams when asked to; records every call so the test can assert on it."""

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([_chunk("ok"), _chunk(finish="stop")])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", reasoning=None, tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


class _FakeACPClient:
    """Stands in for CopilotACPClient without spawning the CLI subprocess."""

    def __init__(self, **_kwargs):
        self.chat = SimpleNamespace(completions=_RecordingCompletions())

    def bind_agent(self, _agent):
        return None


def _acp_agent(monkeypatch, **kwargs):
    import agent.copilot_acp_client as acp_module
    from run_agent import AIAgent

    client = _FakeACPClient()
    monkeypatch.setattr(acp_module, "CopilotACPClient", lambda **_kw: client)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *a, **k: [])
    built = AIAgent(
        model="opus",
        api_key="copilot-acp",
        base_url="acp://copilot",
        provider="copilot-acp",
        platform="cli",
        max_iterations=2,
        quiet_mode=True,
        skip_memory=True,
        **kwargs,
    )
    return built, client


def test_a_copilot_acp_turn_asks_for_a_stream(monkeypatch):
    """The carve-out that keeps the desktop Thought pane populated.

    A display consumer is registered, so the generic ``acp://`` exclusion — if
    it ever widened to cover this provider — would flip the call to
    non-streaming and this assertion would fail.
    """
    built, client = _acp_agent(
        monkeypatch, stream_delta_callback=lambda *_a, **_k: None
    )
    assert built._has_stream_consumers()

    built.run_conversation("hi")

    assert client.chat.completions.calls, "the client was never called"
    assert any(
        c.get("stream") for c in client.chat.completions.calls
    ), "copilot-acp was excluded from streaming; the Thought pane would stay empty"
