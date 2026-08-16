# Open items

Confirmed gaps, not yet patched. Each entry: what's broken, the evidence, the
fix. Move a row to [`changes.md`](changes.md) once it lands.

## ACP session cwd never reaches the subprocess

**Found:** 2026-08-15

`agent/runtime_cwd.py::resolve_agent_cwd()` is Hermes' single source of truth
for the configured working directory (`_SESSION_CWD` contextvar →
`TERMINAL_CWD` → launch-dir fallback). The Codex provider wires it in:

```
agent/codex_runtime.py:704
cwd = getattr(agent, "session_cwd", None) or str(resolve_agent_cwd())
```

`CopilotACPClient.__init__` never does:

```
agent/copilot_acp_client.py:1247
self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
```

`acp_cwd` is a real constructor kwarg, but nothing in production ever passes
it — grepped every `CopilotACPClient(...)` / `client_kwargs` build site
(`agent/agent_runtime_helpers.py:2365`, `agent/auxiliary_client.py:6766`,
`agent/agent_init.py`); only `tests/agent/test_copilot_acp_client.py` sets it.
So the Claude-via-ACP subprocess always launches with `os.getcwd()` of the
Hermes backend process — never the session/profile cwd a desktop chat is
configured for. A new session pinned at a project directory still lands the
agent (and its shell tools) at the backend's own launch dir.

**Fix:** wherever `client_kwargs` is assembled for `provider == "copilot-acp"`
(`agent/agent_init.py`, mirrors the `agent_runtime_helpers.py:2365` construction
site), add `client_kwargs["acp_cwd"] = str(resolve_agent_cwd())` — same pattern
as `codex_runtime.py:704`. Verify out-of-process per invariant 5: drive a real
session pinned at a directory other than the backend's launch dir and confirm
the spawned `claude-agent-acp` subprocess's actual cwd (not just the value
passed) matches.

## Desktop UI tools (open_preview, read_terminal, focus_pane…) never reach the ACP subprocess

**Found:** 2026-08-15

`toolsets.py:275-285` defines the `desktop_ui` toolset — `read_terminal`,
`close_terminal`, `open_preview`, `read_preview`, `read_window_below`,
`focus_pane`, `react_to_message`, `setup_mcp` — enabled only when the GUI
gateway detects a desktop-app session (`tui_gateway/server.py:4383`,
`surfaces.add("desktop_ui")`).

`agent/transports/hermes_tools_mcp_server.py:129-181` is the MCP tool
catalogue both `codex_app_server` and `copilot-acp` (Claude-via-ACP)
subprocesses get instead of Hermes' native loop. `EXPOSED_TOOLS` omits every
name in `desktop_ui`. The comment at `:120-128` justifies dropping
terminal/shell/file tools because Codex has its own built-in equivalents —
but `open_preview`, `read_preview`, `focus_pane`, `react_to_message` have no
ACP-native equivalent and aren't mentioned in that rationale; they're just
missing. Confirmed live: this session (Claude via ACP, desktop source) had no
`open_preview` tool and could not open a generated HTML file in the in-app
preview pane — had to hand the user a `MEDIA:` link instead.

Adding the names to `EXPOSED_TOOLS` is necessary but not sufficient.
`tools/desktop_ui.py` dispatches through a module-level `_emit` callback
wired once per process by `tui_gateway/server.py::_wire_desktop_ui()`
(`:10114-10130`), closed over the live WebSocket for a session. The MCP
server is a *separate* OS process — spawned per
`agent/copilot_acp_client.py:1840` (`-m
agent.transports.hermes_tools_mcp_server`) — so that `_emit` is never set
there; calling `open_preview` from inside it today would find no emitter
installed. The subprocess is only handed `HERMES_SESSION_ID`
(`copilot_acp_client.py:1829`), and that env var is currently read by exactly
one dispatcher, `_dispatch_session_search`
(`hermes_tools_mcp_server.py:220-248`) — nothing routes a desktop_ui call
back into the gateway process for that session id.

**Fix:** two parts.
1. Add `open_preview` (and `read_preview`, `focus_pane` if useful) to
   `EXPOSED_TOOLS`.
2. Build the missing cross-process leg: a loopback endpoint on the gateway,
   keyed by `HERMES_SESSION_ID`, that the MCP server calls and which forwards
   into that session's `tools/desktop_ui.py` `_emit` closure. Without this,
   step 1 alone registers a tool that silently no-ops for every ACP-hosted
   agent.

Verify out-of-process per invariant 5: from a live Claude Sub ACP session,
call the exposed tool and confirm the preview pane actually opens in the
desktop app — not just that the MCP call returns without error.
