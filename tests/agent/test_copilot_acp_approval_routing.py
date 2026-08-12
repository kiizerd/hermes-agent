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


def make_client() -> CopilotACPClient:
    """A client with only the state the message handler reads.

    ``__init__`` resolves the ACP command from the environment and can spawn a
    real agent, so it is deliberately skipped; the two attributes seeded here
    are the ones ``_handle_server_message`` touches (the toolName cache and the
    weakref back to the owning agent, which stays unbound so the tool-card
    callbacks no-op).
    """
    client = object.__new__(CopilotACPClient)
    client._tool_names_by_call_id = {}
    client._agent_ref = None
    return client


def run_permission(
    tool_call: dict,
    message_id: int = 42,
    client: CopilotACPClient | None = None,
) -> dict:
    """One permission RPC through the real handler; returns the JSON-RPC
    response it wrote to the (fake) agent process."""
    client = client if client is not None else make_client()
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


def run_tool_call_update(
    client: CopilotACPClient,
    update: dict,
    tool_lines: dict | None = None,
) -> None:
    """Feed one streamed ``session/update`` tool_call through the real
    handler, exactly as the stdout reader does."""
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
        tool_lines={} if tool_lines is None else tool_lines,
    )


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


def skill_manage_tool(name: str = "throwaway") -> dict:
    """A non-shell MCP-style tool call -- the real driver of the else branch
    and the concrete case approvals.tool_allowlist exists to auto-approve."""
    return {
        "kind": "other",
        "title": f"skill_manage create {name}",
        "rawInput": {"action": "create", "name": name},
        "_meta": {"claudeCode": {"toolName": "skill_manage"}},
    }


MCP_SKILL_MANAGE = "mcp__hermes-tools__skill_manage"
CALL_ID = "toolu_015iwx9AcvQ2p6pyxQC7vD8V"


def mcp_permission_toplevel(call_id: str = CALL_ID, name: str = "throwaway") -> dict:
    """The REAL top-level wire shape, captured from an out-of-process dump of
    claude-agent-acp: a toolCallId and ``kind="other"``, and NO claudeCode
    sidecar. acp-agent.js only attaches ``_meta.claudeCode.toolName`` to a
    permission request when ``parentToolUseId`` exists (subagent calls), so a
    tool the user triggers in the main session arrives anonymous."""
    return {
        "toolCallId": call_id,
        "kind": "other",
        "title": f"skill_manage create {name}",
        "rawInput": {"action": "create", "name": name},
    }


def mcp_tool_call_notification(
    call_id: str = CALL_ID,
    tool_name: str = MCP_SKILL_MANAGE,
    status: str = "pending",
) -> dict:
    """The streamed ``tool_call`` that precedes the permission request and
    DOES carry toolName, keyed by the same id."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "kind": "other",
        "title": "skill_manage",
        "status": status,
        "rawInput": {"action": "create"},
        "_meta": {"claudeCode": {"toolName": tool_name}},
    }


def allowlist(monkeypatch, patterns) -> None:
    """Pin approvals.tool_allowlist for the real handler's gate check."""
    monkeypatch.setattr(
        approval, "_get_approval_config",
        lambda: {"mode": "manual", "tool_allowlist": list(patterns)},
    )


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


class TestToolAllowlist:
    """approvals.tool_allowlist auto-approves non-shell tools by their stable
    provider key -- the thing command_allowlist can't do because the tool
    branch keys each call with an unpredictable per-target hash."""

    def test_allowlisted_tool_auto_approves_without_a_card(self, monkeypatch):
        allowlist(monkeypatch, ["copilot-acp:skill_manage"])
        calls = gate_spy(monkeypatch)
        assert approved(run_permission(skill_manage_tool()))
        assert calls == [], "an allowlisted tool must never reach the gate"

    def test_allowlist_ignores_the_per_target_hash(self, monkeypatch):
        # One entry covers every target of that tool: the match is on the
        # hash-free key, so different targets need no separate approval.
        allowlist(monkeypatch, ["copilot-acp:skill_manage"])
        calls = gate_spy(monkeypatch)
        assert approved(run_permission(skill_manage_tool("alpha")))
        assert approved(run_permission(skill_manage_tool("beta")))
        assert calls == []

    def test_glob_covers_every_non_shell_tool(self, monkeypatch):
        allowlist(monkeypatch, ["copilot-acp:*"])
        calls = gate_spy(monkeypatch)
        assert approved(run_permission(write_tool("C:/tmp/probe-a.txt")))
        assert approved(run_permission(skill_manage_tool()))
        assert calls == []

    def test_non_allowlisted_tool_still_gates(self, monkeypatch):
        allowlist(monkeypatch, ["copilot-acp:skill_manage"])
        calls = gate_spy(monkeypatch)
        # Write is not on the list, so it still gates and fails closed.
        assert not approved(run_permission(write_tool("C:/tmp/probe-a.txt")))
        assert len(calls) == 1

    def test_empty_allowlist_preserves_gating(self, monkeypatch):
        allowlist(monkeypatch, [])
        calls = gate_spy(monkeypatch)
        assert not approved(run_permission(skill_manage_tool()))
        assert len(calls) == 1

    def test_allowlist_does_not_bypass_shell_hardline(self, monkeypatch):
        # The allowlist only relaxes the NON-shell branch. Shell commands
        # still run the dangerous-pattern matcher, hardline floor included,
        # so `copilot-acp:*` cannot unblock `rm -rf /`.
        allowlist(monkeypatch, ["copilot-acp:*"])
        calls = gate_spy(monkeypatch)
        assert not approved(run_permission(bash("rm -rf /")))
        assert not approved(run_permission(execute_unnamed("rm -rf /")))
        assert calls == []


class TestToolNameEnrichment:
    """A top-level permission request carries no toolName, so the gate used to
    key every MCP tool on the single catch-all ``copilot-acp:other``. The name
    does cross the wire on the preceding ``tool_call`` notification under the
    same id; these pin the recovery, its fallback, and its precedence."""

    def test_streamed_tool_call_yields_the_narrow_key(self, monkeypatch):
        client = make_client()
        run_tool_call_update(client, mcp_tool_call_notification())
        calls = gate_spy(monkeypatch)
        run_permission(mcp_permission_toplevel(), client=client)
        assert len(calls) == 1
        assert calls[0]["pattern_key"].startswith(
            f"copilot-acp:{MCP_SKILL_MANAGE}:"
        )

    def test_cache_miss_falls_back_to_kind(self, monkeypatch):
        """No notification seen for this id: behave exactly as before."""
        calls = gate_spy(monkeypatch)
        run_permission(mcp_permission_toplevel(), client=make_client())
        assert len(calls) == 1
        assert calls[0]["pattern_key"].startswith("copilot-acp:other:")

    def test_per_tool_allowlist_entry_now_covers_a_top_level_call(
        self, monkeypatch
    ):
        """The point of the whole change: grant the specific tool, not the
        `other` catch-all that blesses every kind-"other" tool at once."""
        allowlist(monkeypatch, [f"copilot-acp:{MCP_SKILL_MANAGE}"])
        calls = gate_spy(monkeypatch)
        client = make_client()
        run_tool_call_update(client, mcp_tool_call_notification())
        assert approved(run_permission(mcp_permission_toplevel(), client=client))
        assert calls == [], "the narrow allowlist entry must skip the gate"

    def test_narrow_grant_does_not_cover_a_different_tool(self, monkeypatch):
        allowlist(monkeypatch, [f"copilot-acp:{MCP_SKILL_MANAGE}"])
        client = make_client()
        other_id = "toolu_other"
        run_tool_call_update(
            client,
            mcp_tool_call_notification(
                call_id=other_id, tool_name="mcp__hermes-tools__memory"
            ),
        )
        calls = gate_spy(monkeypatch)
        assert not approved(
            run_permission(mcp_permission_toplevel(call_id=other_id), client=client)
        )
        assert len(calls) == 1
        assert calls[0]["pattern_key"].startswith(
            "copilot-acp:mcp__hermes-tools__memory:"
        )

    def test_meta_tool_name_wins_over_the_cache(self, monkeypatch):
        """Subagent calls DO carry the sidecar; it is authoritative, and a
        stale cache entry under the same id must not override it."""
        client = make_client()
        run_tool_call_update(
            client, mcp_tool_call_notification(tool_name="StaleName")
        )
        subagent_call = mcp_permission_toplevel()
        subagent_call["_meta"] = {
            "claudeCode": {"toolName": MCP_SKILL_MANAGE, "parentToolUseId": "toolu_p"}
        }
        calls = gate_spy(monkeypatch)
        run_permission(subagent_call, client=client)
        assert len(calls) == 1
        assert calls[0]["pattern_key"].startswith(
            f"copilot-acp:{MCP_SKILL_MANAGE}:"
        )

    def test_shell_routing_unaffected_by_a_cached_name(self, monkeypatch):
        """Recovering "Bash" must not divert an execute call out of the
        dangerous-command matcher -- the hardline floor still applies."""
        client = make_client()
        run_tool_call_update(
            client,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bash",
                "kind": "execute",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Bash"}},
            },
        )
        calls = gate_spy(monkeypatch)
        call = execute_unnamed("rm -rf /")
        call["toolCallId"] = "toolu_bash"
        assert not approved(run_permission(call, client=client))
        safe = execute_unnamed("ls -la")
        safe["toolCallId"] = "toolu_bash"
        assert approved(run_permission(safe, client=client))
        assert calls == []


class TestToolNameCacheLifetime:
    def test_terminal_status_evicts(self):
        client = make_client()
        run_tool_call_update(client, mcp_tool_call_notification())
        assert client._recall_tool_name(CALL_ID) == MCP_SKILL_MANAGE
        run_tool_call_update(
            client,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": CALL_ID,
                "status": "completed",
                "_meta": {"claudeCode": {"toolName": MCP_SKILL_MANAGE}},
            },
        )
        assert client._recall_tool_name(CALL_ID) == ""
        assert client._tool_names_by_call_id == {}

    def test_denied_call_is_evicted_too(self):
        """A rejected permission resolves the call as `cancelled`, which is
        terminal -- the entry must not linger for the rest of the session."""
        client = make_client()
        run_tool_call_update(client, mcp_tool_call_notification())
        run_tool_call_update(
            client,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": CALL_ID,
                "status": "cancelled",
            },
        )
        assert client._tool_names_by_call_id == {}

    def test_map_is_bounded(self):
        from agent.copilot_acp_client import _TOOL_NAME_CACHE_MAX

        client = make_client()
        for i in range(_TOOL_NAME_CACHE_MAX + 25):
            run_tool_call_update(
                client,
                mcp_tool_call_notification(call_id=f"toolu_{i}", tool_name=f"T{i}"),
            )
        assert len(client._tool_names_by_call_id) == _TOOL_NAME_CACHE_MAX
        # Oldest evicted, newest kept.
        assert client._recall_tool_name("toolu_0") == ""
        last = _TOOL_NAME_CACHE_MAX + 24
        assert client._recall_tool_name(f"toolu_{last}") == f"T{last}"

    def test_update_without_a_name_does_not_clear_a_known_call(self):
        """Later refinements can omit the sidecar; they must not wipe the
        name the opening tool_call established."""
        client = make_client()
        run_tool_call_update(client, mcp_tool_call_notification())
        run_tool_call_update(
            client,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": CALL_ID,
                "status": "in_progress",
            },
        )
        assert client._recall_tool_name(CALL_ID) == MCP_SKILL_MANAGE
