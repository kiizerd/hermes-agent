"""Tests for the hermes-tools-as-MCP server module surface.

We don't run a live MCP session in unit tests — that requires the codex
subprocess + client + an event loop. These tests pin the static
contract: the module imports, the EXPOSED_TOOLS list is sane, and the
build helper assembles a server when the SDK is present.
"""

from __future__ import annotations

import inspect
from typing import get_args

import pytest

from agent.transports.hermes_tools_mcp_server import (
    _signature_from_schema,
)


class TestSignatureFromSchema:
    """Test the JSON Schema -> Python signature conversion."""

    def test_simple_required_string_param(self):
        """A required string param becomes str with no default."""
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        sig, annots = _signature_from_schema(schema)

        assert len(sig.parameters) == 1
        param = sig.parameters["query"]
        assert param.name == "query"
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert annots["query"] == str
        assert param.default is inspect.Parameter.empty



    def test_skip_private_params(self):
        """Params starting with '_' are excluded from the signature."""
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "_internal": {"type": "string"},
            },
            "required": ["query", "_internal"],
        }
        sig, annots = _signature_from_schema(schema)

        assert "_internal" not in sig.parameters
        assert "_internal" not in annots
        assert "query" in sig.parameters

    def test_all_json_types(self):
        """All JSON schema types map to correct Python types."""
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
            },
            "required": ["s", "i", "n", "b", "a", "o"],
        }
        sig, annots = _signature_from_schema(schema)

        assert annots["s"] == str
        assert annots["i"] == int
        assert annots["n"] == float
        assert annots["b"] == bool
        assert annots["a"] == list
        assert annots["o"] == dict








class TestModuleSurface:
    def test_module_imports_clean(self):
        from agent.transports import hermes_tools_mcp_server as m
        assert callable(m.main)
        assert callable(m._build_server)
        assert isinstance(m.EXPOSED_TOOLS, tuple)
        assert len(m.EXPOSED_TOOLS) > 0

    def test_exposed_tools_are_safe_subset(self):
        """We MUST NOT expose tools codex already has, because codex'
        own builtins are better-integrated with its sandbox + approvals.
        Specifically: no terminal/shell, no read_file/write_file, no
        patch — those are codex's built-in tools."""
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
        forbidden = {
            "terminal", "shell", "read_file", "write_file", "patch",
            "search_files", "process",
        }
        leaked = forbidden & set(EXPOSED_TOOLS)
        assert not leaked, (
            f"these tools must NOT be exposed via the codex callback "
            f"because codex has built-in equivalents: {leaked}"
        )


class TestAgentLoopDispatch:
    """model_tools.handle_function_call refuses `_AGENT_LOOP_TOOLS` by name.
    The ones we expose must therefore route around it, or every call comes
    back as "must be handled by the agent loop"."""

    def test_exposed_agent_loop_tools_all_have_an_override(self):
        from model_tools import _AGENT_LOOP_TOOLS
        from agent.transports.hermes_tools_mcp_server import (
            _AGENT_LOOP_DISPATCH,
            EXPOSED_TOOLS,
        )

        exposed_loop_tools = _AGENT_LOOP_TOOLS & set(EXPOSED_TOOLS)
        missing = exposed_loop_tools - set(_AGENT_LOOP_DISPATCH)
        assert not missing, (
            f"agent-loop tools exposed with no dispatch override would always "
            f"fail with the agent-loop refusal: {missing}"
        )

    def test_no_override_for_tools_needing_live_agent_state(self):
        """todo and delegate_task are not reconstructible from disk -- they
        need agent._todo_store and the running loop. An override for them
        would be silently wrong rather than merely unavailable."""
        from agent.transports.hermes_tools_mcp_server import (
            _AGENT_LOOP_DISPATCH,
            EXPOSED_TOOLS,
        )

        for name in ("todo", "delegate_task"):
            assert name not in _AGENT_LOOP_DISPATCH
            assert name not in EXPOSED_TOOLS

    def test_dispatch_prefers_override_over_handle_function_call(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        calls: list[str] = []
        monkeypatch.setitem(
            m._AGENT_LOOP_DISPATCH, "memory", lambda args: f"override:{args}"
        )

        fake_defs = [
            {
                "type": "function",
                "function": {
                    "name": "memory",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {"action": {"type": "string"}},
                    },
                },
            }
        ]

        class FakeMCP:
            def __init__(self, *a, **kw):
                self.handlers = {}

            def add_tool(self, fn, name=None, description=None):
                self.handlers[name] = fn

        monkeypatch.setattr(
            "mcp.server.fastmcp.FastMCP", FakeMCP, raising=False
        )
        monkeypatch.setattr(
            "model_tools.get_tool_definitions", lambda **kw: fake_defs
        )
        monkeypatch.setattr(
            "model_tools.handle_function_call",
            lambda *a, **kw: calls.append("handle_function_call") or "wrong",
        )

        server = m._build_server()
        result = server.handlers["memory"](action="add")

        assert result == "override:{'action': 'add'}"
        assert calls == [], "handle_function_call must not be reached"

    def test_session_search_opens_db_read_only_and_closes_it(self, monkeypatch):
        """A writable handle would contend with the live Hermes process for
        the same SQLite file, including the TRUNCATE-WAL on close()."""
        import agent.transports.hermes_tools_mcp_server as m

        opened: dict = {}

        class FakeDB:
            def __init__(self, read_only=False):
                opened["read_only"] = read_only
                opened["closed"] = False

            def close(self):
                opened["closed"] = True

        monkeypatch.setattr("hermes_state.SessionDB", FakeDB)
        monkeypatch.setattr(
            "tools.session_search_tool.session_search",
            lambda **kw: f"searched:{kw['query']}:{kw['current_session_id']}",
        )
        monkeypatch.setenv("HERMES_SESSION_ID", "sess-xyz")

        out = m._dispatch_session_search({"query": "hello"})

        assert out == "searched:hello:sess-xyz"
        assert opened["read_only"] is True
        assert opened["closed"] is True

    def test_session_search_closes_db_even_when_search_raises(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        state = {"closed": False}

        class FakeDB:
            def __init__(self, read_only=False):
                pass

            def close(self):
                state["closed"] = True

        def boom(**kw):
            raise RuntimeError("search exploded")

        monkeypatch.setattr("hermes_state.SessionDB", FakeDB)
        monkeypatch.setattr("tools.session_search_tool.session_search", boom)

        with pytest.raises(RuntimeError):
            m._dispatch_session_search({"query": "x"})
        assert state["closed"] is True, "DB handle leaked on the error path"

    def test_memory_store_is_rebuilt_per_call(self, monkeypatch):
        """A cached store would drift from disk whenever another Hermes
        session wrote memory, and the tool's own save_to_disk would then
        persist the stale snapshot over it."""
        import agent.transports.hermes_tools_mcp_server as m

        built: list[dict] = []

        class FakeStore:
            def __init__(self, memory_char_limit=0, user_char_limit=0):
                built.append(
                    {"memory": memory_char_limit, "user": user_char_limit}
                )
                self.loaded = False

            def load_from_disk(self):
                self.loaded = True

        monkeypatch.setattr("tools.memory_tool.MemoryStore", FakeStore)
        monkeypatch.setattr(
            "tools.memory_tool.memory_tool",
            lambda **kw: f"mem:{kw['action']}:{kw['store'].loaded}",
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"memory": {"memory_char_limit": 999, "user_char_limit": 5}},
        )

        assert m._dispatch_memory({"action": "add"}) == "mem:add:True"
        assert m._dispatch_memory({"action": "add"}) == "mem:add:True"
        assert len(built) == 2, "store must not be cached across calls"
        assert built[0] == {"memory": 999, "user": 5}

    def test_memory_falls_back_to_defaults_when_config_unreadable(
        self, monkeypatch
    ):
        import agent.transports.hermes_tools_mcp_server as m

        built: list[dict] = []

        class FakeStore:
            def __init__(self, memory_char_limit=0, user_char_limit=0):
                built.append(
                    {"memory": memory_char_limit, "user": user_char_limit}
                )

            def load_from_disk(self):
                pass

        def boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("tools.memory_tool.MemoryStore", FakeStore)
        monkeypatch.setattr("tools.memory_tool.memory_tool", lambda **kw: "ok")
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)

        assert m._dispatch_memory({"action": "add"}) == "ok"
        assert built == [{"memory": 2200, "user": 1375}]






class TestMain:
    def test_main_returns_2_when_mcp_unavailable(self, monkeypatch):
        """When the mcp package isn't installed, main() should exit
        cleanly with code 2 and an install hint, not crash."""
        import agent.transports.hermes_tools_mcp_server as m

        def boom_build(*a, **kw):
            raise ImportError("mcp not installed")

        monkeypatch.setattr(m, "_build_server", boom_build)
        rc = m.main(["--verbose"])
        assert rc == 2

    def test_main_handles_keyboard_interrupt(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class FakeServer:
            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(m, "_build_server", lambda: FakeServer())
        rc = m.main([])
        assert rc == 0

    def test_main_returns_1_on_runtime_error(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class CrashingServer:
            def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(m, "_build_server", lambda: CrashingServer())
        rc = m.main([])
        assert rc == 1


class TestSkillManageExposure:
    """turn_finalizer.py gates background skill review on skill_manage being
    a valid tool. Under a native ACP runtime the review fork reaches Hermes
    tools only through this server, so without skill_manage here memory
    review can write but skill review has no tool."""

    def test_skill_manage_is_exposed(self):
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS

        assert "skill_manage" in EXPOSED_TOOLS

    def test_skill_manage_stays_plain_dispatch(self):
        """skill_manage takes pure kwargs (tools/skill_manager_tool.py) and
        must stay dispatchable through handle_function_call. If it ever
        joins _AGENT_LOOP_TOOLS, it needs an override in
        _AGENT_LOOP_DISPATCH or every call comes back refused -- the
        invariant test above only sees tools already in both sets."""
        from model_tools import _AGENT_LOOP_TOOLS

        assert "skill_manage" not in _AGENT_LOOP_TOOLS

    def test_skill_manage_dispatches_via_handle_function_call(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        calls: list = []

        fake_defs = [
            {
                "type": "function",
                "function": {
                    "name": "skill_manage",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "name": {"type": "string"},
                        },
                    },
                },
            }
        ]

        class FakeMCP:
            def __init__(self, *a, **kw):
                self.handlers = {}

            def add_tool(self, fn, name=None, description=None):
                self.handlers[name] = fn

        monkeypatch.setattr(
            "mcp.server.fastmcp.FastMCP", FakeMCP, raising=False
        )
        monkeypatch.setattr(
            "model_tools.get_tool_definitions", lambda **kw: fake_defs
        )
        monkeypatch.setattr(
            "model_tools.handle_function_call",
            lambda name, args: calls.append((name, args)) or "ok",
        )

        server = m._build_server()
        result = server.handlers["skill_manage"](action="create", name="x")

        assert result == "ok"
        assert calls == [("skill_manage", {"action": "create", "name": "x"})]
