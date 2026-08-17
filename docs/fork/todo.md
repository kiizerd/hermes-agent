# Open items

Confirmed gaps, not yet patched. Each entry: what's broken, the evidence, the
fix. Move a row to [`changes.md`](changes.md) once it lands.

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

## AskUserQuestion tool disabled for every Claude-via-ACP session

**Found:** 2026-08-17

`claude-agent-acp`'s `dist/acp-agent.js` (~line 4186-4193) gates the
`AskUserQuestion` tool on a client-declared capability:

```js
const elicitationSupport = { form: !!this.clientCapabilities?.elicitation?.form, ... };
const disallowedTools = elicitationSupport.form ? [] : ["AskUserQuestion"];
```

Hermes' handshake in `agent/copilot_acp_client.py:2229-2234` never sends an
`elicitation` key:

```python
"clientCapabilities": {
    "fs": {"readTextFile": True, "writeTextFile": True}
},
```

so `elicitationSupport.form` is always false and `AskUserQuestion` is
unconditionally in `disallowedTools`. This is **not** mode-gated — the
Bridge/Native `system_prompt_mode` switch (`_effective_system_prompt_mode()`,
`copilot_acp_client.py:1579`) only touches the `systemPrompt` payload sent at
`session/new`, never `clientCapabilities`, which is negotiated once at
`initialize` before any session opens (same "no resend RPC" constraint that
makes the Bridge pill pre-session-only). Confirmed via grep — no
`elicitation` string anywhere in `copilot_acp_client.py`.

**Fix:** two parts, not a flag flip.
1. Add `"elicitation": {"form": True}` (and maybe `"url"`) to the
   `clientCapabilities` dict at `copilot_acp_client.py:2229`.
2. Implement the matching render/response leg on the Hermes side: once the
   capability is declared, `claude-agent-acp` will start sending elicitation
   requests over the ACP connection when `AskUserQuestion` is called. Nothing
   in `copilot_acp_client.py` currently handles that RPC — declaring the
   capability without a handler means the request gets dropped on the floor
   and the tool call hangs or errors. Needs: (a) the ACP method name/shape
   claude-agent-acp uses for the elicitation request (check
   `acp-agent.js` for what it sends when `elicitationSupport.form` is true —
   likely a `session/request_permission`-style extension, not a stock ACP
   method), (b) a UI surface to render the form (desktop composer prompt,
   similar to the existing permission-gate dialog), (c) wiring the reply back
   through whatever RPC id/session key the request carried.

Verify out-of-process per invariant 5: drive a live Claude Sub ACP session,
trigger a real `AskUserQuestion` call, and confirm a form actually renders
in the desktop app and the answer round-trips back to the agent — not just
that the tool stops appearing in `disallowedTools`.

## Permission dropdown has no "leave the agent alone" option

**Found:** 2026-08-17

The desktop permission-mode dropdown offers only ids the ACP agent
advertises — `default`, `plan`, `acceptEdits`, `bypassPermissions` for
`claude-agent-acp@0.64.2`. There is no entry for the *unset* state, even
though unset is a real, documented, and behaviourally distinct mode.

`_requested_acp_mode()` (`agent/copilot_acp_client.py:820-845`) returns the
raw configured string. `_select_acp_mode()` (`:848-881`) matches it against
`_acp_mode_ids(session)` exactly (`:862`) then case-insensitively (`:864`);
on no match it logs and **returns without sending `session/set_mode` at all**
(`:865-872`), leaving the child on whatever mode it started in. An empty
string takes the same no-RPC path. The docstring at `:823-825` states this is
deliberate — "an unset value leaves the agent on whatever mode it chose for
itself" — so *passthrough is a supported mode*; it simply has no id, so
nothing can offer it in a list built from advertised ids.

**The user reached it by accident.** `copilot_acp.permission_mode: auto` was
set in `~/.hermes/config.yaml`. `auto` is not an advertised id, so it fell
down the same `:865-872` no-match path as empty, no `session/set_mode` was
ever sent, and the child ran on its own start mode — which asked for zero
permissions, so Hermes' `session/request_permission` handler (`:2835-2977`)
never fired and no approval cards appeared. Changing the value to `default`
made the RPC land for the first time and the cards returned. Note the config
file is **not** validated on load: `tui_gateway/acp_session_modes.py:385-425`
rejects an unknown id with error 4002, but only for RPC-driven `config.set`,
so a junk value in YAML degrades silently into passthrough.

Two things are unresolved and must be settled before implementing:

1. **Why the child's own start mode asks for nothing.** Not established.
   `_select_acp_mode`'s docstring (`:851-854`) notes a `settings.json`
   `defaultMode` is only read on paths that see the real `HOME`, which the
   `claude-acp-run.js` launcher wrapper hides — so the child is falling back
   to some built-in default. Whether that default is genuinely permissive, or
   whether the launcher's env allowlist is what suppresses the prompts, needs
   to be read out of `claude-agent-acp`'s `dist/acp-agent.js` and the
   launcher. Shipping an "Auto" that silently means bypass-everything is the
   thing to avoid.
2. **What "Auto" should map to**, once (1) is known: an explicit
   passthrough sentinel (honest — "don't manage the mode", the current
   behaviour given a name), an alias for an advertised permissive id
   (predictable, but `acceptEdits`/`bypassPermissions` already exist and say
   what they do), or a Hermes-side auto-approve allowlist that answers
   `session/request_permission` without a card (keeps Hermes in the loop, but
   duplicates `approvals.tool_allowlist`).

**Fix sketch (option 1, passthrough sentinel):** reserve an id the agent can
never advertise (e.g. `""` rendered as `Auto`), inject it as the first choice
in the list the dropdown is built from, exempt it from the 4002 validation in
`acp_session_modes.py:416-424`, and let it flow to the existing no-match path
unchanged. Label it for what it does — "Agent's own default (no override)" —
not "Auto", which reads like a Hermes feature rather than an abdication.

Verify out-of-process per invariant 5: pick the option in a live Claude Sub
ACP session and confirm from the log that no `session/set_mode` is sent
(`:878` stays silent) and that the pill still reads back the chosen value
after a reconnect.
