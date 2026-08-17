"""The bridge/native pick is latched to the CHAT, not to the ACP subprocess.

Regression cover for a lock anchored one level too low. The client's own
``system_prompt_mode_state()["locked"]`` is ``bool(self._session_id)``, and
``_shutdown_process`` clears that id on a model switch, on transcript
divergence, and on a failed resync -- so the composer pill became editable again
mid-chat every time the child was rebuilt, and a pick made in that window really
did reach the next ``session/new``. systemPrompt is sent once per ACP session
with no RPC to re-send it, so that spliced a different system prompt into the
middle of one transcript.

Assertions are made against the ``session.info`` block and the ``config.set``
arm the desktop actually consumes, never against the latch helper alone: the
bug was never in a helper, it was in what the two publishing paths chose to
read.
"""

from __future__ import annotations

import pytest

from tui_gateway import acp_session_modes as modes


class _StubClient:
    """Duck-typed CopilotACPClient. ``locked`` is the SUBPROCESS's view only."""

    def __init__(self, *, locked: bool = False, value: str = "bridge") -> None:
        self.locked = locked
        self.value = value
        self.writes: list[str] = []

    def set_system_prompt_mode(self, mode: str) -> str:
        self.writes.append(mode)
        return mode or self.value

    def system_prompt_mode_state(self) -> dict:
        return {
            "value": self.value,
            "source": "config",
            "locked": self.locked,
            "options": ["bridge", "native"],
        }


class _StubAgent:
    def __init__(self, client=None) -> None:
        self.client = client
        self.provider = "copilot-acp"


def _session(client=None, **extra) -> dict:
    session: dict = {"agent": _StubAgent(client), "history": []}
    session.update(extra)
    return session


@pytest.fixture(autouse=True)
def _claude_provider(monkeypatch):
    """Both publishing paths gate on "is this copilot-acp actually Claude"."""
    import hermes_cli.models

    monkeypatch.setattr(hermes_cli.models, "_copilot_acp_is_rerouted", lambda: True)


def _info(session: dict) -> dict:
    return modes._acp_system_prompt_mode_info(session, "copilot-acp")


def _ok(rid, result):
    return {"id": rid, "result": result}


def _err(rid, code, message):
    return {"id": rid, "error": {"code": code, "message": message}}


def _set(session: dict, value: str):
    return modes.handle_acp_config_set(
        "acp_system_prompt_mode",
        value,
        session,
        1,
        {"session_id": "s1"},
        ok=_ok,
        err=_err,
        emit=lambda *a, **k: None,
        session_info=lambda agent, sess: {},
    )


# ── the latch ───────────────────────────────────────────────────────


def test_a_chat_that_has_not_run_is_editable():
    """The draft/pre-first-turn window is the whole point of the pill."""
    session = _session(_StubClient())

    assert _info(session)["locked"] is False
    assert _set(session, "native")["result"]["value"] == "native"


def test_turn_start_latches_the_chat():
    session = _session(_StubClient())

    modes.apply_session_acp_modes(session)

    assert _info(session)["locked"] is True


def test_latch_survives_a_subprocess_rebuild():
    """The regression. A rebuilt child reports unlocked; the chat does not.

    This is the model-switch / divergence / resync path: ``_shutdown_process``
    clears ``_session_id``, so the fresh client's own lock reads False.
    """
    session = _session(_StubClient(locked=True))
    modes.apply_session_acp_modes(session)

    session["agent"].client = _StubClient(locked=False)

    assert _info(session)["locked"] is True


def test_client_lock_still_stands_on_its_own():
    """The latch is ADDITIVE -- it never relaxes a lock the client asserts.

    Covers the gap between ``session/new`` and the first turn end, where the ACP
    session exists but no turn has completed.
    """
    session = _session(_StubClient(locked=True))

    assert _info(session)["locked"] is True


def test_non_acp_sessions_are_not_stamped():
    """No dead key on the OpenAI/Gemini chats sharing the process."""
    session = _session(None)

    modes.apply_session_acp_modes(session)

    assert modes._SYSTEM_PROMPT_MODE_LATCH_SESSION_KEY not in session


# ── resumed chats ───────────────────────────────────────────────────


def test_a_resumed_transcript_is_latched_before_its_agent_exists():
    """History is the durable tell; a gateway restart drops the flag, not the chat."""
    session = _session(None, history=[{"role": "user", "content": "hi"}])

    assert _info(session)["locked"] is True


def test_a_deferred_resume_is_latched_while_its_history_is_still_loading():
    """The desktop resumes with ``defer_history=True``: history is [] for a moment.

    Without the resume handle the pill would flash editable on a fifty-turn
    chat, which is long enough to click.
    """
    session = _session(None, resume_history_ready=object())

    assert _info(session)["locked"] is True


# ── the config.set arm ──────────────────────────────────────────────


def test_config_set_rejects_a_latched_chat_whose_client_reports_unlocked():
    session = _session(_StubClient(locked=True))
    modes.apply_session_acp_modes(session)
    client = _StubClient(locked=False)
    session["agent"].client = client

    response = _set(session, "native")

    assert response["error"]["code"] == 4002
    assert "new chat" in response["error"]["message"]
    assert client.writes == []
    assert modes._SYSTEM_PROMPT_MODE_SESSION_KEY not in session


def test_a_new_chat_is_the_escape_hatch():
    """A fresh session dict carries no latch, so nothing has to clear one."""
    latched = _session(_StubClient())
    modes.apply_session_acp_modes(latched)
    assert _set(latched, "native")["error"]["code"] == 4002

    fresh = _session(_StubClient())

    assert _set(fresh, "native")["result"]["value"] == "native"
    assert fresh[modes._SYSTEM_PROMPT_MODE_SESSION_KEY] == "native"
