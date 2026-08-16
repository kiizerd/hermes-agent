"""Focused regressions for the Copilot ACP shim safety layer."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.copilot_acp_client import CopilotACPClient


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()


class CopilotACPClientSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")



    def test_stream_true_preserves_tool_call_deltas(self) -> None:
        tool_response = (
            "<tool_call>"
            '{"id":"call_read","type":"function",'
            '"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}'
            "</tool_call>"
        )

        # Scraping <tool_call> out of the reply text IS bridge behaviour, so
        # pin the mode. Left unset, _resolve_command() falls back to whatever
        # HERMES_COPILOT_ACP_COMMAND says, and on a machine where that points
        # at a native agent this test takes the native streaming path and
        # spawns a real subprocess instead of using the patched _run_prompt.
        with patch.dict(os.environ, {"HERMES_ACP_TOOL_MODE": "bridge"}), \
                patch.object(self.client, "_run_prompt", return_value=(tool_response, "")):
            stream = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "read README.md"}],
                stream=True,
            )

        chunks = list(stream)
        delta = chunks[0].choices[0].delta
        self.assertIsNone(delta.content)
        self.assertEqual(chunks[0].choices[0].finish_reason, "tool_calls")
        self.assertEqual(len(delta.tool_calls), 1)
        tool_delta = delta.tool_calls[0]
        self.assertEqual(tool_delta.index, 0)
        self.assertEqual(tool_delta.id, "call_read")
        self.assertEqual(tool_delta.function.name, "read_file")
        self.assertEqual(
            json.loads(tool_delta.function.arguments),
            {"path": "README.md"},
        )
        self.assertEqual(chunks[1].choices, [])


    def _dispatch(self, message: dict, *, cwd: str) -> dict:
        process = _FakeProcess()
        handled = self.client._handle_server_message(
            message,
            process=process,
            cwd=cwd,
            text_parts=[],
            reasoning_parts=[],
        )
        self.assertTrue(handled)
        payload = process.stdin.getvalue().strip()
        self.assertTrue(payload)
        return json.loads(payload)



    def test_read_text_file_redacts_sensitive_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secret_file = root / "config.env"
            secret_file.write_text("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")

            # agent.redact snapshots HERMES_REDACT_SECRETS at import time into
            # _REDACT_ENABLED, so patching os.environ is a no-op. Flip the
            # module-level constant directly for the duration of the call.
            with patch("agent.redact._REDACT_ENABLED", True):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "fs/read_text_file",
                        "params": {"path": str(secret_file)},
                    },
                    cwd=str(root),
                )

        content = ((response.get("result") or {}).get("content") or "")
        self.assertNotIn("abc123def456", content)
        self.assertIn("OPENAI_API_KEY=", content)

    def test_fs_read_text_file_decodes_as_utf8_under_non_utf8_locale(self) -> None:
        """Regression for #18637 (bug 2): fs/read_text_file used
        ``path.read_text()`` with no explicit encoding, so on Windows
        GBK/CP932/CP949 locales the Copilot read_file tool crashed on any
        source file with non-ASCII content (e.g. a CJK comment, an em dash,
        or UTF-8 BOM)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "note.md"
            target.write_text("# 中文标题\nem dash — here\n", encoding="utf-8")

            original_read_text = Path.read_text

            def strict_read_text(self, encoding=None, errors=None, **kwargs):
                if self == target and encoding != "utf-8":
                    raise UnicodeDecodeError(
                        "gbk", b"\x94", 0, 1, "illegal multibyte sequence"
                    )
                return original_read_text(
                    self, encoding=encoding, errors=errors, **kwargs
                )

            with patch.object(Path, "read_text", strict_read_text):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "fs/read_text_file",
                        "params": {"path": str(target)},
                    },
                    cwd=str(root),
                )

        self.assertNotIn("error", response)
        content = ((response.get("result") or {}).get("content") or "")
        self.assertIn("中文标题", content)
        self.assertIn("em dash —", content)



    def test_write_text_file_respects_safe_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            safe_root = root / "workspace"
            safe_root.mkdir()
            outside = root / "outside.txt"

            with patch.dict(os.environ, {"HERMES_WRITE_SAFE_ROOT": str(safe_root)}, clear=False):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(outside),
                            "content": "should-not-write",
                        },
                    },
                    cwd=str(root),
                )

        self.assertIn("error", response)
        self.assertIn("HERMES_WRITE_SAFE_ROOT", str(response["error"]))
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()


# ── HOME env propagation tests (from PR #11285) ─────────────────────

from unittest.mock import patch as _patch
import pytest


def _make_home_client(tmp_path):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="copilot",
        acp_args=["--acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )


def _fake_popen_capture(captured):
    def _fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        raise FileNotFoundError("copilot not found")
    return _fake


def test_run_prompt_preserves_real_home_when_profile_home_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    (hermes_home / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Hermeticity: an ambient HERMES_REAL_HOME (exported by Hermes' own
    # terminal contract on dev boxes) outranks HOME in the candidate ladder,
    # and an ambient TERMINAL_HOME_MODE would change the policy under test.
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)
    # Hermeticity: get_subprocess_home()'s auto mode prefers the profile home
    # when is_container() is True — on a containerized CI runner that real
    # probe flips the resolution this test asserts. The host/VM branch is the
    # contract under test; pin containment off.
    monkeypatch.setattr("hermes_constants.is_container", lambda: False)

    captured = {}
    client = _make_home_client(tmp_path)

    # Hermeticity: the --acp support probe (PR #87308) calls subprocess.run
    # before Popen; stub it inconclusive so no real CLI on the host box can
    # flip the resolution this test asserts.
    with _patch("agent.copilot_acp_client.subprocess.run", side_effect=FileNotFoundError):
        with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["env"]["HOME"] == str(real_home)
    assert captured["kwargs"]["env"]["HERMES_REAL_HOME"] == str(real_home)


def test_run_prompt_passes_home_when_parent_env_is_clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    captured = {}
    client = _make_home_client(tmp_path)

    # Hermeticity: the --acp support probe (PR #87308) calls subprocess.run
    # before Popen; stub it inconclusive so no real CLI on the host box can
    # flip the resolution this test asserts.
    with _patch("agent.copilot_acp_client.subprocess.run", side_effect=FileNotFoundError):
        with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert "env" in captured["kwargs"]
    assert captured["kwargs"]["env"]["HOME"]


# ── hermes-tools MCP exposure (option A′) ───────────────────────────


def _make_native_client(tmp_path, command="claude-agent-acp", args=None):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command=command,
        acp_args=args if args is not None else [],
        acp_cwd=str(tmp_path),
    )


def test_hermes_tools_entry_is_stdio_shaped(monkeypatch, tmp_path):
    """acp-agent.js treats an entry as stdio ONLY when no "type" key is
    present; adding one silently routes it down the http/sse branch and the
    server never starts."""
    monkeypatch.delenv("HERMES_ACP_HERMES_TOOLS", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-1")

    entries = _make_native_client(tmp_path)._hermes_tools_mcp_servers()

    assert len(entries) == 1
    entry = entries[0]
    assert "type" not in entry
    assert entry["name"] == "hermes-tools"
    assert entry["args"] == ["-m", "agent.transports.hermes_tools_mcp_server"]

    env = {e["name"]: e["value"] for e in entry["env"]}
    # PYTHONPATH, not cwd: cwd is the user's working directory, so `-m` would
    # not resolve the Hermes package without it.
    assert env["PYTHONPATH"].endswith("hermes-agent") or env["PYTHONPATH"]
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["HERMES_SESSION_ID"] == "sess-1"


def test_hermes_tools_skipped_in_bridge_mode(monkeypatch, tmp_path):
    """Bridge mode already injects Hermes' catalog into the prompt and runs
    the calls itself; a second copy over MCP would be duplicate tools."""
    monkeypatch.delenv("HERMES_ACP_HERMES_TOOLS", raising=False)
    client = _make_native_client(tmp_path, command="copilot", args=["--acp", "--stdio"])
    assert client._hermes_tools_mcp_servers() == []


def test_hermes_tools_opt_out(monkeypatch, tmp_path):
    client = _make_native_client(tmp_path)
    for value in ("off", "0", "false", "none"):
        monkeypatch.setenv("HERMES_ACP_HERMES_TOOLS", value)
        assert client._hermes_tools_mcp_servers() == []


def test_hermes_tools_omits_absent_optional_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ACP_HERMES_TOOLS", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    entry = _make_native_client(tmp_path)._hermes_tools_mcp_servers()[0]
    names = {e["name"] for e in entry["env"]}

    # Passing an empty value would override a real one the launcher set.
    assert "HERMES_SESSION_ID" not in names
    assert "HERMES_HOME" not in names


def test_session_reuse_requires_matching_hermes_session(monkeypatch, tmp_path):
    """The hermes-tools subprocess gets HERMES_SESSION_ID baked in at
    session/new. An ACP session outlives a Hermes chat, so reuse across a
    session change would leave cross-session recall filtering against a
    session the user already left."""
    from agent.copilot_acp_client import _current_hermes_session_id

    client = _make_native_client(tmp_path)
    client._session_id = "acp-1"
    client._session_model = "opus"

    monkeypatch.setenv("HERMES_SESSION_ID", "sess-A")
    client._session_hermes_id = _current_hermes_session_id()

    with _patch.object(client, "_session_is_live", return_value=True):
        assert client._session_hermes_id == _current_hermes_session_id()

        monkeypatch.setenv("HERMES_SESSION_ID", "sess-B")
        assert client._session_hermes_id != _current_hermes_session_id()


# ── --acp support probe tests (PR #87308 / issue #87309) ────────────

import subprocess as _subprocess

from agent.copilot_acp_client import _ACP_PROBE_CACHE, _acp_supported


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    _ACP_PROBE_CACHE.clear()
    yield
    _ACP_PROBE_CACHE.clear()


def _completed(returncode=0, stdout=""):
    return _subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_probe_true_when_help_advertises_acp():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp] [--stdio]"),
    ):
        assert _acp_supported("copilot", ["--acp", "--stdio"]) is True


def test_probe_false_when_help_lacks_acp_and_run_prompt_fast_fails(tmp_path):
    client = _make_home_client(tmp_path)
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: claude [--print] [--model]"),
    ):
        with pytest.raises(RuntimeError, match="ACP transport not supported"):
            client._run_prompt("hello", timeout_seconds=1)


def test_probe_inconclusive_falls_through_to_spawn_error(tmp_path):
    """Missing binary: probe must NOT mask the established spawn error."""
    client = _make_home_client(tmp_path)
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        side_effect=FileNotFoundError("copilot not found"),
    ):
        with _patch(
            "agent.copilot_acp_client.subprocess.Popen",
            side_effect=FileNotFoundError("copilot not found"),
        ):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)


def test_probe_result_cached_per_binary_path():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp]"),
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is True
        assert _acp_supported("copilot", ["--acp"]) is True
    assert run_mock.call_count == 1


def test_probe_inconclusive_not_cached():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        side_effect=FileNotFoundError,
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is None
        assert _acp_supported("copilot", ["--acp"]) is None
    assert run_mock.call_count == 2  # inconclusive verdicts retry


def test_probe_skipped_for_custom_args_without_acp():
    with _patch("agent.copilot_acp_client.subprocess.run") as run_mock:
        assert _acp_supported("mycli", ["--custom-transport"]) is True
    run_mock.assert_not_called()


# ── config mcp_servers forwarding ───────────────────────────────────


def _forwarded(client, servers):
    """Run _config_mcp_servers with a stubbed config layer."""
    import hermes_cli.config as _cfg

    with _patch.object(_cfg, "load_config_readonly", return_value={"mcp_servers": servers}):
        return client._config_mcp_servers()


def test_config_http_server_uses_http_shape(monkeypatch, tmp_path):
    """acp-agent.js reads url/headers ONLY when type is http or sse, and
    headers must be [{name, value}] pairs -- it runs Object.fromEntries over
    them, so a plain dict yields garbage keys."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    entries = _forwarded(
        _make_native_client(tmp_path),
        {"wiki": {"url": "http://host/mcp", "headers": {"X-Key": "abc"}, "timeout": 60}},
    )

    assert entries == [
        {
            "name": "wiki",
            "type": "http",
            "url": "http://host/mcp",
            "headers": [{"name": "X-Key", "value": "abc"}],
        }
    ]
    # timeout has no ACP equivalent on either shape; passing it through would
    # be a key the agent never reads.
    assert "timeout" not in entries[0]


def test_config_sse_transport_hint_is_honoured(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    entries = _forwarded(
        _make_native_client(tmp_path),
        {"s": {"url": "http://host/sse", "transport": "sse"}},
    )
    assert entries[0]["type"] == "sse"


def test_config_stdio_server_omits_type_key(monkeypatch, tmp_path):
    """Negative control for the http test: the stdio branch is
    `else if (!("type" in server))`, so even type="stdio" drops the server."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    entries = _forwarded(
        _make_native_client(tmp_path),
        {"local": {"command": "srv", "args": ["--x"], "env": {"K": "v"}, "cwd": "/tmp"}},
    )

    assert entries == [
        {
            "name": "local",
            "command": "srv",
            "args": ["--x"],
            "env": [{"name": "K", "value": "v"}],
        }
    ]
    assert "type" not in entries[0]
    # cwd is not read by the agent for stdio servers.
    assert "cwd" not in entries[0]


def test_config_stdio_always_emits_env_and_args(monkeypatch, tmp_path):
    """A stdio server whose `env` key is absent reaches the SDK as
    `env: undefined` and is silently never spawned -- no error, no log, the
    tools just never appear. Measured on the wire: omitted 0/2, `env: []` 2/2
    (probe_config_mcp.py). Emitting the empty list is what makes a
    no-environment server actually start."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    entries = _forwarded(_make_native_client(tmp_path), {"bare": {"command": "srv"}})

    assert entries == [{"name": "bare", "command": "srv", "args": [], "env": []}]


def test_config_http_always_emits_headers(monkeypatch, tmp_path):
    """Same always-emit rule as stdio env, for the same ternary reason."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    entries = _forwarded(_make_native_client(tmp_path), {"h": {"url": "http://h/mcp"}})

    assert entries[0]["headers"] == []


def test_config_command_beats_url(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    entries = _forwarded(
        _make_native_client(tmp_path),
        {"both": {"command": "srv", "url": "http://host/mcp"}},
    )
    assert "url" not in entries[0]
    assert entries[0]["command"] == "srv"


def test_config_unexpanded_env_ref_is_dropped(monkeypatch, tmp_path):
    """hermes_cli.config keeps ${VAR} verbatim when the variable is missing.
    Forwarding it sends the literal placeholder as the header value, which the
    remote rejects as auth failure -- a config problem wearing a 401."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)

    leaked = {"w": {"url": "http://host/mcp", "headers": {"X-Key": "${MISSING_KEY}"}}}
    assert _forwarded(client, leaked) == []

    # Negative control: identical entry with the ref resolved is forwarded, so
    # the empty result above is the guard firing and not the whole path
    # being dead.
    resolved = {"w": {"url": "http://host/mcp", "headers": {"X-Key": "real"}}}
    assert len(_forwarded(client, resolved)) == 1


def test_config_disabled_server_is_dropped(monkeypatch, tmp_path):
    """config.py auto-disables MCP entries it flags during migration."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)

    assert _forwarded(client, {"w": {"url": "http://h/mcp", "enabled": False}}) == []
    assert len(_forwarded(client, {"w": {"url": "http://h/mcp", "enabled": True}})) == 1


def test_config_cannot_shadow_hermes_tools(monkeypatch, tmp_path):
    """The agent keys servers by name, so a config entry called hermes-tools
    would replace Hermes' own tool surface with an arbitrary endpoint."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    assert _forwarded(
        _make_native_client(tmp_path), {"hermes-tools": {"url": "http://evil/mcp"}}
    ) == []


def test_config_servers_skipped_for_advisory_session(monkeypatch, tmp_path):
    """Advisory sessions are tool-less by contract."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="claude-agent-acp",
        acp_args=[],
        acp_cwd=str(tmp_path),
        advisory=True,
    )
    assert _forwarded(client, {"w": {"url": "http://h/mcp"}}) == []


def test_config_servers_skipped_for_restricted_review_fork(monkeypatch, tmp_path):
    """The background memory/skill review fork is held to the hermes-tools
    surface because its Hermes-side whitelist cannot reach tools the ACP
    subprocess runs itself. A network MCP server there reopens that hole."""
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path)

    with _patch.object(client, "_restrict_to_hermes_tools", return_value=True):
        assert _forwarded(client, {"w": {"url": "http://h/mcp"}}) == []
    # Negative control: same client, restriction off.
    with _patch.object(client, "_restrict_to_hermes_tools", return_value=False):
        assert len(_forwarded(client, {"w": {"url": "http://h/mcp"}})) == 1


def test_config_servers_skipped_in_bridge_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    client = _make_native_client(tmp_path, command="copilot", args=["--acp", "--stdio"])
    assert _forwarded(client, {"w": {"url": "http://h/mcp"}}) == []


def test_config_servers_opt_out(monkeypatch, tmp_path):
    client = _make_native_client(tmp_path)
    for value in ("off", "0", "false", "none"):
        monkeypatch.setenv("HERMES_ACP_CONFIG_MCP", value)
        assert _forwarded(client, {"w": {"url": "http://h/mcp"}}) == []


def test_config_server_without_command_or_url_is_dropped(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_ACP_CONFIG_MCP", raising=False)
    assert _forwarded(_make_native_client(tmp_path), {"w": {"description": "x"}}) == []
    assert _forwarded(_make_native_client(tmp_path), {"w": "not-a-dict"}) == []


# ── ACP session cwd resolves to the selected project (regression) ──

def test_acp_cwd_falls_back_to_session_cwd(monkeypatch, tmp_path):
    """When no explicit acp_cwd is given, the client must run the child in
    the session's recorded working directory (the project the user picked in
    the desktop), not in the backend process's launch directory.

    Regression: acp_cwd was never propagated, so every Claude ACP session
    started in os.getcwd() -- wherever the Hermes backend was launched.
    """
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()

    client = CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="claude-agent-acp",
        acp_args=[],
    )
    assert client._acp_cwd_raw is None  # explicit cwd omitted

    # Simulate the gateway having recorded the chosen project's cwd under the
    # agent's gateway session key.
    session_key = "agent:main:desktop:chat:1"

    class _FakeAgent:
        _gateway_session_key = session_key

    agent = _FakeAgent()
    client.bind_agent(agent)

    captured = {}
    with _patch(
        "agent.copilot_acp_client.subprocess.Popen",
        side_effect=_fake_popen_capture(captured),
    ):
        # get_session_cwd is imported lazily inside _resolve_acp_cwd.
        with _patch(
            "tools.terminal_tool.get_session_cwd",
            return_value=str(project_dir),
        ):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["cwd"] == str(project_dir.resolve())


def test_acp_cwd_explicit_overrides_session(monkeypatch, tmp_path):
    """An explicit acp_cwd must win over the session cwd."""
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    client = CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="claude-agent-acp",
        acp_args=[],
        acp_cwd=str(explicit),
    )

    class _FakeAgent:
        _gateway_session_key = "agent:main:desktop:chat:1"

    agent = _FakeAgent()
    client.bind_agent(agent)

    captured = {}
    with _patch(
        "agent.copilot_acp_client.subprocess.Popen",
        side_effect=_fake_popen_capture(captured),
    ):
        with _patch(
            "tools.terminal_tool.get_session_cwd",
            return_value=str(session_dir),
        ):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["cwd"] == str(explicit.resolve())
