"""Per-target approval routing for ACP session/request_permission.

These tests drive the REAL ``CopilotACPClient._handle_server_message`` --
not a copy of its routing branch -- with a fake process capturing the
JSON-RPC response, so what is pinned is the shipped handler.

Wire-shape note (learned the hard way): a real session/request_permission
from claude-agent-acp carries ``kind="execute"`` and NO toolName on the
toolCall (toolName rides the session/update notifications instead). The
"unnamed execute" cases below are that real shape; the ``_meta.claudeCode``
cases cover Claude's sidecar when it is present.

Env pinning: these tests may run on a machine whose real .env reroutes
copilot-acp onto a live agent. Nothing here constructs ``CopilotACPClient``
(its env-derived command resolution is how a test ends up spawning a real
subprocess), and the autouse fixture pins every ambient input the approval
gate reads so no branch is inherited from the developer's environment.
"""

from __future__ import annotations

import io
import json

import pytest

import tools.approval as approval
from agent.copilot_acp_client import CopilotACPClient

OPTIONS = [
    {"optionId": "allow", "kind": "allow_once"},
    {"optionId": "allow-always", "kind": "allow_always"},
    {"optionId": "deny", "kind": "reject_once"},
]


class FakeProc:
    """Just enough of Popen for _handle_server_message: a stdin to write
    the JSON-RPC response into, and poll() saying "still alive"."""

    def __init__(self):
        self.stdin = io.StringIO()

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def pinned_approval_env(monkeypatch):
    """Pin every ambient input the approval gate reads.

    - No interactive user, gateway, cron, or yolo: the fail-closed branch
      is deterministic instead of inheriting the developer's real .env.
    - The approval stores are module-global and survive across tests in
      one process; snapshot and restore them so an [a]lways granted here
      cannot leak into other tests (or the developer's session).
    """
    for var in (
        "HERMES_INTERACTIVE",
        "HERMES_GATEWAY_SESSION",
        "HERMES_CRON_SESSION",
        "HERMES_EXEC_ASK",
        "HERMES_YOLO",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    ctx_token = approval._hermes_interactive_ctx.set(None)

    permanent = set(approval._permanent_approved)
    session = {k: set(v) for k, v in approval._session_approved.items()}
    yield
    approval._hermes_interactive_ctx.reset(ctx_token)
    with approval._lock:
        approval._permanent_approved.clear()
        approval._permanent_approved.update(permanent)
        approval._session_approved.clear()
        approval._session_approved.update(session)


def run_permission(tool_call: dict, message_id: int = 42) -> dict:
    """One permission RPC through the real handler; returns the JSON-RPC
    response it wrote to the (fake) agent process."""
    client = object.__new__(CopilotACPClient)  # routing reads no __init__ state
    proc = FakeProc()
    handled = client._handle_server_message(
        {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "toolCall": tool_call, "options": OPTIONS},
        },
        process=proc,
        cwd="C:/tmp",
        text_parts=[],
        reasoning_parts=[],
        tool_lines={},
    )
    assert handled is True
    return json.loads(proc.stdin.getvalue())


def approved(response: dict) -> bool:
    outcome = (response.get("result") or {}).get("outcome") or {}
    return outcome.get("outcome") == "selected"


def bash(cmd: str) -> dict:
    return {
        "kind": "execute",
        "title": cmd,
        "rawInput": {"command": cmd},
        "_meta": {"claudeCode": {"toolName": "Bash"}},
    }


def execute_unnamed(cmd: str) -> dict:
    """The REAL wire shape: kind=execute, no claudeCode sidecar."""
    return {"kind": "execute", "title": cmd, "rawInput": {"command": cmd}}


def write_tool(path: str) -> dict:
    return {
        "kind": "edit",
        "title": f"Write {path}",
        "rawInput": {"file_path": path, "content": "x" * 500},
        "_meta": {"claudeCode": {"toolName": "Write"}},
    }


def gate_spy(monkeypatch) -> list[dict]:
    """Wrap the real _run_approval_gate, recording kwargs. The handler
    imports it at call time, so patching the module attribute intercepts."""
    real = approval._run_approval_gate
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(approval, "_run_approval_gate", spy)
    return calls


class TestShellRouting:
    def test_safe_reads_auto_approve_without_a_card(self, monkeypatch):
        calls = gate_spy(monkeypatch)
        for cmd in ("ls -la", "git status --short"):
            assert approved(run_permission(bash(cmd))), cmd
        assert calls == [], "safe reads must never reach the approval gate"

    def test_hardline_blocks_and_cannot_be_user_approved(self, monkeypatch):
        calls = gate_spy(monkeypatch)
        assert not approved(run_permission(bash("rm -rf /")))
        assert calls == [], "hardline decisions never reach the gate"
        # Not even yolo mode may unblock it: the floor fires before the
        # bypass, so there is no grant a user could give that approves it.
        monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
        assert not approved(run_permission(bash("rm -rf /")))

    def test_unnamed_execute_still_reaches_the_shell_branch(self, monkeypatch):
        """Real permission RPCs carry no toolName -- kind=execute alone must
        route to the dangerous-command matcher, hardline floor included."""
        calls = gate_spy(monkeypatch)
        assert not approved(run_permission(execute_unnamed("rm -rf /")))
        assert approved(run_permission(execute_unnamed("ls -la")))
        assert calls == []


class TestNonShellGate:
    def test_gates_and_fails_closed_with_no_human(self, monkeypatch):
        calls = gate_spy(monkeypatch)
        response = run_permission(write_tool("C:/tmp/probe-a.txt"))
        assert not approved(response)
        assert len(calls) == 1
        assert calls[0]["pattern_key"].startswith("copilot-acp:Write:")
        assert calls[0]["fail_closed_when_no_human"] is True
        # The card must show the real target, not a generic label.
        assert "probe-a.txt" in calls[0]["display_target"]

    def test_gate_exception_denies(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(approval, "_run_approval_gate", boom)
        assert not approved(run_permission(write_tool("C:/tmp/x.txt")))


class TestPatternKeyGrain:
    def _key_for(self, monkeypatch, tool_call) -> str:
        calls = gate_spy(monkeypatch)
        run_permission(tool_call)
        assert calls, "expected the gate to be consulted"
        return calls[-1]["pattern_key"]

    def test_key_stable_across_content_churn(self, monkeypatch):
        key_a = self._key_for(monkeypatch, write_tool("C:/tmp/probe-a.txt"))
        churned = write_tool("C:/tmp/probe-a.txt")
        churned["rawInput"]["content"] = "totally different content " * 40
        key_a2 = self._key_for(monkeypatch, churned)
        assert key_a == key_a2

    def test_different_target_different_key(self, monkeypatch):
        key_a = self._key_for(monkeypatch, write_tool("C:/tmp/probe-a.txt"))
        key_b = self._key_for(monkeypatch, write_tool("C:/tmp/probe-b.txt"))
        assert key_a != key_b

    def test_always_on_one_path_does_not_bless_another(self, monkeypatch):
        key_a = self._key_for(monkeypatch, write_tool("C:/tmp/probe-a.txt"))
        approval.approve_permanent(key_a)
        # Approved target now auto-approves: the session-cache check fires
        # before the fail-closed branch, so no human is needed.
        assert approved(run_permission(write_tool("C:/tmp/probe-a.txt")))
        # A different path -- and the sensitive one -- still gate.
        assert not approved(run_permission(write_tool("C:/tmp/probe-b.txt")))
        assert not approved(run_permission(write_tool("~/.ssh/authorized_keys")))

    def test_is_approved_session_cache_api(self, monkeypatch):
        key_a = self._key_for(monkeypatch, write_tool("C:/tmp/probe-a.txt"))
        session_key = approval.get_current_session_key()
        assert not approval.is_approved(session_key, key_a)
        approval.approve_permanent(key_a)
        assert approval.is_approved(session_key, key_a)
        assert approval.is_approved("some-other-session", key_a), (
            "permanent grants are session-independent"
        )
