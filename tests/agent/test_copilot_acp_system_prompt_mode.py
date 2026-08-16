"""Bridge vs native system-prompt mode for the Claude-over-ACP client.

The split under test is a WIRE contract, not a Python one. `_meta.systemPrompt`
takes two shapes and claude-agent-acp treats them very differently:

    {"append": "..."}   -> forwarded onto the locked claude_code preset
    "..."               -> REPLACES the preset outright

The SDK accepts both and errors on neither, so getting this wrong produces a
session that boots fine and silently runs a downgraded agent. Every assertion
here is therefore made against the params actually handed to `session/new`,
never against a helper's return value.
"""

from __future__ import annotations

from unittest.mock import patch

from agent.copilot_acp_client import CopilotACPClient


def _make_native_client(tmp_path, command="claude-agent-acp", args=None):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command=command,
        acp_args=args if args is not None else [],
        acp_cwd=str(tmp_path),
    )


def _capture_session_new(client) -> dict:
    """Drive _ensure_session far enough to capture session/new's params.

    The process spawn and the `initialize` round trip are the only things
    stubbed; the params under assertion are built by the real code path.
    """
    captured: dict = {}

    def _fake_rpc(method, params=None, **kwargs):
        if method == "session/new":
            captured["params"] = params
            return {"sessionId": "sess-1"}
        return {}

    with patch.object(client, "_spawn_process", return_value=None), patch.object(
        client, "_shutdown_process", return_value=None
    ), patch.object(client, "_rpc", side_effect=_fake_rpc), patch.object(
        client, "_sync_acp_mode", return_value=None
    ):
        client._ensure_session(model=None, timeout_seconds=1)

    return captured.get("params") or {}


def _system_prompt(client):
    return (_capture_session_new(client).get("_meta") or {}).get("systemPrompt")


class _StubAgent:
    """Minimal stand-in for the bound AIAgent.

    `_cached_system_prompt_static` is pre-seeded so a test can prove the native
    build never writes it.
    """

    def __init__(self) -> None:
        self._cached_system_prompt_static = "UNTOUCHED"
        self.statuses: list[str] = []

    def _emit_status(self, message) -> None:
        self.statuses.append(message)


def _bind(client, agent):
    client._agent_ref = lambda: agent


# ── wire shape ──────────────────────────────────────────────────────


def test_bridge_mode_sends_object_form(monkeypatch, tmp_path):
    """Bridge is the default and must use the object form -- the string form
    would strip Claude Code's identity, tool-schema guidance and CLAUDE.md
    loading."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    prompt = _system_prompt(_make_native_client(tmp_path))

    assert isinstance(prompt, dict), "bridge must not send the replacing string form"
    assert "hermes-tools" in prompt["append"]


def test_native_mode_sends_string_form(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)
    client.set_system_prompt_mode("native")

    with patch.object(client, "_build_native_system_prompt", return_value="HERMES PROMPT"):
        prompt = _system_prompt(client)

    assert isinstance(prompt, str), "native must replace the preset, not append"
    assert prompt.startswith("HERMES PROMPT")


def test_native_mode_keeps_memory_block_and_operator_append(monkeypatch, tmp_path):
    """Regression. Native mode is one string, so it has no `append` channel --
    the fork's memory/skill block and the operator's own `system_prompt_append`
    must be concatenated instead.

    Losing them was silent, and cost native mode the very block that maps bare
    tool names onto `mcp__hermes-tools__*`.
    """
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)
    client.set_system_prompt_mode("native")

    def _cfg(key, **_kw):
        return "OPERATOR TEXT" if key == "system_prompt_append" else ""

    with patch.object(
        client, "_build_native_system_prompt", return_value="HERMES PROMPT"
    ), patch("agent.copilot_acp_client._acp_config_str", side_effect=_cfg):
        prompt = _system_prompt(client)

    assert "mcp__hermes-tools__memory" in prompt
    assert "OPERATOR TEXT" in prompt
    # Operator text rides last so it can override everything above it.
    assert prompt.rindex("OPERATOR TEXT") > prompt.rindex("mcp__hermes-tools__memory")


def test_native_mode_falls_back_to_bridge_when_prompt_unavailable(monkeypatch, tmp_path):
    """No resolvable agent (a standalone client) means no Hermes prompt. That
    has to degrade to the append form, not to an empty system prompt."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)
    client.set_system_prompt_mode("native")

    prompt = _system_prompt(client)

    assert isinstance(prompt, dict)
    assert "hermes-tools" in prompt["append"]


# ── excluded session kinds ──────────────────────────────────────────


def test_native_mode_refused_for_advisory_session(monkeypatch, tmp_path):
    """Advisory sessions get no mcpServers and have no agent to build a Hermes
    prompt from."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="claude-agent-acp",
        acp_args=[],
        acp_cwd=str(tmp_path),
        advisory=True,
    )
    client.set_system_prompt_mode("native")

    with patch.object(client, "_build_native_system_prompt", return_value="HERMES PROMPT"):
        assert not isinstance(_system_prompt(client), str)


def test_native_mode_refused_for_restricted_review_fork(monkeypatch, tmp_path):
    """The background memory/skill review fork runs deliberately tool-starved
    against a harness prompt of its own. The full Hermes operating brief --
    coding posture, workspace snapshot, whole skills index -- would talk over
    that harness and undo the narrowing."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)
    client.set_system_prompt_mode("native")

    with patch.object(client, "_build_native_system_prompt", return_value="HERMES PROMPT"):
        with patch.object(client, "_restrict_to_hermes_tools", return_value=True):
            assert not isinstance(_system_prompt(client), str)
        # Negative control: same client, restriction off.
        with patch.object(client, "_restrict_to_hermes_tools", return_value=False):
            assert isinstance(_system_prompt(client), str)


# ── native prompt construction ──────────────────────────────────────


def test_native_prompt_carries_tool_name_reconciliation(tmp_path):
    """The Hermes prompt names tools off `agent.valid_tool_names`, but only the
    EXPOSED_TOOLS allowlist crosses the MCP boundary (prefixed), and terminal /
    read_file / write_file do not cross at all. Without the reconciliation
    block the session is told to use tools it does not have."""
    client = _make_native_client(tmp_path)
    _bind(client, _StubAgent())

    with patch(
        "agent.system_prompt.build_system_prompt_parts",
        return_value={"stable": "STABLE", "context": "", "volatile": "VOLATILE"},
    ):
        built = client._build_native_system_prompt()

    assert built.startswith("STABLE\n\nVOLATILE")
    assert "mcp__hermes-tools__web_search" in built
    assert "Bash, Read, Write, Edit" in built


def test_native_prompt_does_not_touch_hermes_prompt_cache(tmp_path):
    """`build_system_prompt()` stamps `agent._cached_system_prompt_static`, the
    reconstruction anchor for Hermes' OWN prompt-cache accounting. This string
    goes out over ACP and is never sent by Hermes to a model API, so seeding
    that slot with it would describe a prefix no Hermes request ever used."""
    client = _make_native_client(tmp_path)
    agent = _StubAgent()
    _bind(client, agent)

    with patch(
        "agent.system_prompt.build_system_prompt_parts",
        return_value={"stable": "STABLE", "context": "", "volatile": ""},
    ):
        client._build_native_system_prompt()

    assert agent._cached_system_prompt_static == "UNTOUCHED"


def test_native_prompt_drains_truncation_warnings(tmp_path):
    """Context-file truncation warnings are queued as a side effect of the
    build. They still reach the user -- _ensure_session runs inside the first
    prompt's flow, so the status channel is live -- but leaving them queued
    would strand them for the life of the process."""
    client = _make_native_client(tmp_path)
    agent = _StubAgent()
    _bind(client, agent)

    with patch(
        "agent.system_prompt.build_system_prompt_parts",
        return_value={"stable": "STABLE", "context": "", "volatile": ""},
    ), patch(
        "agent.system_prompt.drain_truncation_warnings",
        return_value=["CLAUDE.md was truncated"],
    ):
        client._build_native_system_prompt()

    assert agent.statuses == ["CLAUDE.md was truncated"]


def test_native_prompt_empty_build_falls_back(tmp_path):
    """An empty parts dict must not produce a lone reconciliation block as the
    entire system prompt."""
    client = _make_native_client(tmp_path)
    _bind(client, _StubAgent())

    with patch(
        "agent.system_prompt.build_system_prompt_parts",
        return_value={"stable": "", "context": "", "volatile": ""},
    ):
        assert client._build_native_system_prompt() == ""


# ── mode resolution and lock ────────────────────────────────────────


def test_system_prompt_mode_precedence(tmp_path):
    """Session pick beats config; anything unrecognised falls back to bridge."""
    client = _make_native_client(tmp_path)

    with patch("agent.copilot_acp_client._acp_config_str", return_value=""):
        assert client._effective_system_prompt_mode() == "bridge"
        assert client.system_prompt_mode_state()["source"] == "default"
        # Case-insensitive, whitespace-tolerant.
        assert client.set_system_prompt_mode("  NATIVE ") == "native"
        assert client.system_prompt_mode_state()["source"] == "session"
        assert client.set_system_prompt_mode("nonsense") == "bridge"

    def _cfg(key, **_kw):
        return "native" if key == "system_prompt_mode" else ""

    with patch("agent.copilot_acp_client._acp_config_str", side_effect=_cfg):
        client.set_system_prompt_mode("")
        assert client._effective_system_prompt_mode() == "native"
        assert client.system_prompt_mode_state()["source"] == "config"
        # An explicit session pick still outranks a native config default --
        # this is the case the desktop draft replay depends on.
        assert client.set_system_prompt_mode("bridge") == "bridge"


def test_lock_follows_session_id_not_the_chat(tmp_path):
    """`locked` is scoped to the ACP SESSION. Anything that tears the
    subprocess down (a mid-chat model switch is the realistic one) clears
    `_session_id` and reopens the window -- the intended escape hatch, and
    what the UI copy now says."""
    client = _make_native_client(tmp_path)

    assert client.system_prompt_mode_state()["locked"] is False
    client._session_id = "sess-1"
    assert client.system_prompt_mode_state()["locked"] is True
    client._session_id = None
    assert client.system_prompt_mode_state()["locked"] is False


def test_state_reports_both_options(tmp_path):
    """The pill renders from `options`; an empty list would paint a dead
    toggle."""
    assert _make_native_client(tmp_path).system_prompt_mode_state()["options"] == [
        "bridge",
        "native",
    ]
