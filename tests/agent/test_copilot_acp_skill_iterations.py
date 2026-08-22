"""Native-ACP skill-nudge iteration accounting.

Hermes decides when to offer a background skill review from
``_iters_since_skill``, bumped once per pass through the chat-completions loop
(``agent/conversation_loop.py``). On an ordinary provider one user turn makes
many passes -- one per tool batch -- so the nudge threshold arrives after a
handful of turns.

Native ACP breaks that: ``tool_calls`` is forced empty for the mode, so the
loop exits after a single pass however much work the sub-agent did behind the
wire. A turn where Claude ran twenty tools ticked the counter exactly once.
``CopilotACPClient._credit_native_tool_iterations`` supplies the difference,
the same compensation ``agent/codex_runtime.py`` applies for the codex
app-server path.

These tests drive the REAL client methods on a hand-seeded instance;
``__init__`` resolves the ACP command from the environment and can spawn a
subprocess, so it is skipped deliberately (same approach as
``test_copilot_acp_approval_routing.py``).
"""

import io
import os
import weakref

import pytest

from agent.copilot_acp_client import CopilotACPClient


class FakeAgent:
    """Only the attributes the credit path reads and writes.

    ``valid_tool_names`` is here because the loop this compensates for gates
    its own ``+= 1`` on it too (``agent/conversation_loop.py``); an agent that
    cannot call ``skill_manage`` is never counted on either path.
    """

    def __init__(
        self,
        nudge_interval: int = 10,
        iters: int = 0,
        tools: tuple[str, ...] = ("skill_manage",),
    ):
        self._skill_nudge_interval = nudge_interval
        self._iters_since_skill = iters
        self.valid_tool_names = tools


class FakeProc:
    def __init__(self):
        self.stdin = io.StringIO()

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def pinned_tool_mode(monkeypatch):
    """Pin the tool mode instead of inheriting it.

    ``_resolve_tool_mode`` reads the launch argv, and on a machine whose
    ``.env`` reroutes copilot-acp onto another CLI a bare client silently
    resolves to a mode the test did not choose. Each test sets the var it
    means; the default here is native because that is the mode under test.
    """
    monkeypatch.setenv("HERMES_ACP_TOOL_MODE", "native")


def make_client(agent: FakeAgent | None = None) -> CopilotACPClient:
    client = object.__new__(CopilotACPClient)
    client._tool_names_by_call_id = {}
    client._last_turn_tool_calls = 0
    client._acp_command = "claude-agent-acp"
    client._acp_args = []
    client._agent_ref = weakref.ref(agent) if agent is not None else None
    return client


def feed(client: CopilotACPClient, update: dict) -> None:
    """One streamed ``session/update`` through the real handler."""
    client._handle_server_message(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "s", "update": update},
        },
        process=FakeProc(),
        cwd="C:/tmp",
        text_parts=[],
        reasoning_parts=[],
    )


def tool_call(call_id: str, name: str = "Bash") -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "kind": "execute",
        "title": name,
        "status": "pending",
        "_meta": {"claudeCode": {"toolName": name}},
    }


def tool_call_update(call_id: str, status: str = "completed") -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": status,
    }


class TestTallyingToolCalls:
    def test_each_tool_call_counts_once(self):
        client = make_client()
        for i in range(4):
            feed(client, tool_call(f"call-{i}"))
        assert client._last_turn_tool_calls == 4

    def test_updates_do_not_inflate_the_tally(self):
        """``tool_call`` announces a call; ``tool_call_update`` only refines it.

        Counting both would multiply the tally by however many refinements the
        adapter happened to stream -- which varies per tool and per run, so the
        credit would be noise rather than a measurement.
        """
        client = make_client()
        feed(client, tool_call("call-1"))
        for _ in range(5):
            feed(client, tool_call_update("call-1", status="in_progress"))
        feed(client, tool_call_update("call-1"))
        assert client._last_turn_tool_calls == 1


class TestCreditingTheAgent:
    def test_credits_all_but_the_turn_the_loop_already_counted(self):
        agent = FakeAgent(iters=3)
        client = make_client(agent)
        client._last_turn_tool_calls = 9
        client._credit_native_tool_iterations()
        # 9 tools ran; conversation_loop counted this turn once already.
        assert agent._iters_since_skill == 3 + 8

    @pytest.mark.parametrize("observed", [0, 1])
    def test_a_quiet_turn_is_not_credited(self, observed):
        """0 or 1 tools is exactly what the loop's own +1 already covers."""
        agent = FakeAgent(iters=5)
        client = make_client(agent)
        client._last_turn_tool_calls = observed
        client._credit_native_tool_iterations()
        assert agent._iters_since_skill == 5

    def test_bridge_mode_is_not_credited(self, monkeypatch):
        """Bridge runs tools through Hermes' own loop, which counts them."""
        monkeypatch.setenv("HERMES_ACP_TOOL_MODE", "bridge")
        agent = FakeAgent(iters=2)
        client = make_client(agent)
        client._last_turn_tool_calls = 7
        client._credit_native_tool_iterations()
        assert agent._iters_since_skill == 2, "bridge would double-count"

    def test_a_disabled_nudge_is_left_alone(self):
        """A zero interval means the nudge is off for this agent.

        The background-review fork sets it to 0 precisely so it cannot spawn
        another review; accumulating a tally there would be dead weight at
        best and recursive at worst.
        """
        agent = FakeAgent(nudge_interval=0, iters=0)
        client = make_client(agent)
        client._last_turn_tool_calls = 12
        client._credit_native_tool_iterations()
        assert agent._iters_since_skill == 0

    def test_an_agent_without_skill_manage_is_not_credited(self):
        """The loop's ``+= 1`` is gated on this too, so the credit must be.

        ``agent/conversation_loop.py`` only counts an iteration when
        ``skill_manage`` is in ``valid_tool_names``. A leaf sub-agent with
        restricted toolsets fails that test, so the loop counts 0 -- and the
        ``- 1`` this method subtracts would then under-credit by one while
        stacking a tally onto an agent that can never act on the nudge.
        Skipping outright is the only arithmetic that stays exact.
        """
        agent = FakeAgent(iters=4, tools=("terminal", "read_file"))
        client = make_client(agent)
        client._last_turn_tool_calls = 9
        client._credit_native_tool_iterations()
        assert agent._iters_since_skill == 4

    def test_a_missing_valid_tool_names_does_not_raise(self):
        """Not every object bound here is a fully-built AIAgent."""

        class Bare:
            _skill_nudge_interval = 10
            _iters_since_skill = 0

        agent = Bare()
        client = make_client(agent)
        client._last_turn_tool_calls = 9
        client._credit_native_tool_iterations()  # must not raise
        assert agent._iters_since_skill == 0

    def test_unbound_agent_does_not_raise(self):
        """The weakref is deliberately weak, so it can be dead mid-session."""
        client = make_client(None)
        client._last_turn_tool_calls = 6
        client._credit_native_tool_iterations()  # must not raise

    def test_collected_agent_does_not_raise(self):
        agent = FakeAgent()
        client = make_client(agent)
        client._last_turn_tool_calls = 6
        del agent
        import gc

        gc.collect()
        client._credit_native_tool_iterations()  # must not raise


class TestEndToEndTally:
    def test_streamed_calls_credit_the_agent(self):
        """The two halves join up: notifications tally, the tally credits."""
        agent = FakeAgent(iters=0)
        client = make_client(agent)
        for i in range(3):
            feed(client, tool_call(f"c-{i}"))
            feed(client, tool_call_update(f"c-{i}"))
        client._credit_native_tool_iterations()
        assert agent._iters_since_skill == 2

    def test_threshold_is_now_reachable_within_a_turn(self):
        """The behavioural point of the fix.

        Before it, a 10-tool turn advanced the counter by 1 and the skill
        review needed ~10 user turns to trigger. It should now clear a
        default-interval threshold in a single busy turn.
        """
        agent = FakeAgent(nudge_interval=10, iters=0)
        client = make_client(agent)
        for i in range(11):
            feed(client, tool_call(f"c-{i}"))
        client._credit_native_tool_iterations()
        assert agent._iters_since_skill >= agent._skill_nudge_interval
