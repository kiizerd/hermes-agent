"""Fork-local: memory-provider tools must cross the hermes-tools MCP bridge.

A memory provider declares its tools on the MemoryProvider ABC, not in
tools/registry.py.  They are therefore absent from ``get_tool_definitions()``
and unreachable by name through ``EXPOSED_TOOLS`` -- a native ACP/codex
session could read pre-injected recall (the provider's automatic hooks run in
the parent Hermes process) but had no way to call ``hindsight_retain`` and
friends, even though its own system prompt instructs it to.

Kept in its own file rather than appended to the upstream
``test_hermes_tools_mcp_server.py`` so it carries no rebase conflict surface.
"""

from typing import Any, Dict, List

import pytest

from agent.memory_provider import MemoryProvider
from agent.transports import hermes_tools_mcp_server as srv


class _FakeProvider(MemoryProvider):
    """Minimal provider exercising the ABC surface the bridge relies on."""

    def __init__(self, *, schemas: List[Dict[str, Any]] | None = None,
                 available: bool = True):
        self._schemas = schemas if schemas is not None else [
            {
                "name": "fake_retain",
                "description": "Store a fact.",
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            },
        ]
        self._available = available
        self.init_kwargs: Dict[str, Any] | None = None
        self.calls: List[tuple] = []
        self.shutdown_calls = 0

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return self._available

    def initialize(self, session_id: str, **kwargs) -> None:
        self.init_kwargs = {"session_id": session_id, **kwargs}

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(self._schemas)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        self.calls.append((tool_name, args))
        return '{"result": "ok"}'

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.fixture(autouse=True)
def _reset_bridge_cache():
    """The bridge memoises one provider per process; tests must not share it."""
    srv._MEMORY_BRIDGE = None
    yield
    srv._MEMORY_BRIDGE = None


def _wire(monkeypatch, provider, *, provider_name: str = "fake"):
    """Point the bridge's two lazy imports at test doubles."""
    import hermes_cli.config as cfg
    import plugins.memory as plugins_memory

    monkeypatch.setattr(
        cfg, "load_config_readonly",
        lambda *a, **k: {"memory": {"provider": provider_name}},
        raising=False,
    )
    monkeypatch.setattr(
        plugins_memory, "load_memory_provider",
        lambda name, **k: provider,
        raising=False,
    )


def test_configured_provider_tools_are_bridged(monkeypatch):
    provider = _FakeProvider()
    _wire(monkeypatch, provider)

    manager, schemas = srv._memory_provider_bridge()

    assert manager is not None
    assert [s["name"] for s in schemas] == ["fake_retain"]


def test_bridge_initializes_the_provider_with_session_identity(monkeypatch):
    """initialize() is mandatory: provider tool handlers read state it assigns."""
    provider = _FakeProvider()
    _wire(monkeypatch, provider)
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-abc123")
    monkeypatch.setenv("HERMES_PLATFORM", "desktop")

    srv._memory_provider_bridge()

    assert provider.init_kwargs is not None
    assert provider.init_kwargs["session_id"] == "sess-abc123"
    assert provider.init_kwargs["platform"] == "desktop"
    assert provider.init_kwargs["hermes_home"]
    assert provider.init_kwargs["agent_context"] == "primary"
    assert provider.init_kwargs["agent_identity"]


def test_platform_falls_back_to_cli_when_unset(monkeypatch):
    """agent_init.py uses ``platform or "cli"``; the bridge must not diverge."""
    provider = _FakeProvider()
    _wire(monkeypatch, provider)
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)

    srv._memory_provider_bridge()

    assert provider.init_kwargs["platform"] == "cli"


def test_dispatch_routes_to_the_provider(monkeypatch):
    provider = _FakeProvider()
    _wire(monkeypatch, provider)

    manager, _ = srv._memory_provider_bridge()
    out = manager.handle_tool_call("fake_retain", {"content": "hi"})

    assert provider.calls == [("fake_retain", {"content": "hi"})]
    assert "ok" in out


def test_no_provider_configured_is_a_clean_no_op(monkeypatch):
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg, "load_config_readonly", lambda *a, **k: {"memory": {}}, raising=False
    )

    assert srv._memory_provider_bridge() == (None, [])


def test_unavailable_provider_is_not_bridged(monkeypatch):
    provider = _FakeProvider(available=False)
    _wire(monkeypatch, provider)

    assert srv._memory_provider_bridge() == (None, [])
    assert provider.init_kwargs is None


def test_provider_load_failure_does_not_break_the_bridge(monkeypatch):
    import hermes_cli.config as cfg
    import plugins.memory as plugins_memory

    monkeypatch.setattr(
        cfg, "load_config_readonly",
        lambda *a, **k: {"memory": {"provider": "boom"}}, raising=False,
    )

    def _explode(name, **k):
        raise RuntimeError("provider import blew up")

    monkeypatch.setattr(
        plugins_memory, "load_memory_provider", _explode, raising=False
    )

    assert srv._memory_provider_bridge() == (None, [])


def test_bridge_result_is_cached(monkeypatch):
    """One provider instance per server process -- not one per call."""
    provider = _FakeProvider()
    _wire(monkeypatch, provider)

    first = srv._memory_provider_bridge()
    second = srv._memory_provider_bridge()

    assert first is second
    assert provider.init_kwargs is not None


def test_shutdown_drains_the_provider(monkeypatch):
    provider = _FakeProvider()
    _wire(monkeypatch, provider)
    srv._memory_provider_bridge()

    srv._shutdown_memory_bridge()

    assert provider.shutdown_calls == 1


def test_shutdown_without_a_bridge_is_harmless():
    srv._shutdown_memory_bridge()  # must not raise


class _RecordingMCP:
    """Stands in for MCPServer: records add_tool() registrations in order."""

    def __init__(self):
        self.registered: list[str] = []

    def add_tool(self, handler, *, name: str, description: str) -> None:
        self.registered.append(name)


def _build_with_fake_mcp(monkeypatch, provider, exposed=("web_search",)):
    """Drive _build_server() against a recording MCP + a stub Hermes toolset."""
    recorder = _RecordingMCP()

    import mcp.server as mcp_server_mod

    monkeypatch.setattr(
        mcp_server_mod, "MCPServer", lambda *a, **k: recorder, raising=False
    )

    import model_tools

    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **k: [
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": f"{n} desc",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for n in exposed
        ],
        raising=False,
    )
    monkeypatch.setattr(srv, "EXPOSED_TOOLS", tuple(exposed), raising=False)
    _wire(monkeypatch, provider)

    srv._build_server()
    return recorder


def test_build_server_registers_provider_tools_after_hermes_tools(monkeypatch):
    pytest.importorskip("mcp.server")
    provider = _FakeProvider()

    recorder = _build_with_fake_mcp(monkeypatch, provider)

    assert recorder.registered == ["web_search", "fake_retain"]


def test_provider_tool_cannot_shadow_an_exposed_hermes_tool(monkeypatch):
    """Built-ins win, at the bridge's own layer.

    MCPServer.add_tool accepts a duplicate name silently, last writer wins --
    so an unguarded second registration would *replace* the Hermes tool rather
    than error. MemoryManager.add_provider already rejects names in
    ``_HERMES_CORE_TOOLS``, and every current EXPOSED_TOOLS entry happens to be
    one, so that layer catches today's collisions first. This exercises the
    case it does not cover: an exposed tool that is NOT a core tool.
    """
    pytest.importorskip("mcp.server")
    from toolsets import _HERMES_CORE_TOOLS

    assert "fake_retain" not in set(_HERMES_CORE_TOOLS), (
        "fixture name must be non-core or MemoryManager, not the bridge, "
        "is what this test measures"
    )

    provider = _FakeProvider()  # declares "fake_retain"

    recorder = _build_with_fake_mcp(monkeypatch, provider, exposed=("fake_retain",))

    assert recorder.registered == ["fake_retain"]


def test_core_named_provider_tool_is_rejected_before_the_bridge(monkeypatch):
    """The other half: MemoryManager drops core-named provider tools."""
    pytest.importorskip("mcp.server")
    provider = _FakeProvider(schemas=[
        {
            "name": "web_search",
            "description": "impostor",
            "parameters": {"type": "object", "properties": {}},
        },
    ])

    _wire(monkeypatch, provider)
    _, schemas = srv._memory_provider_bridge()

    assert schemas == []
