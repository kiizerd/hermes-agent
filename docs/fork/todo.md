# Open items

Confirmed gaps, not yet patched. Each entry: what's broken, the evidence, the
fix. Move a row to [`changes.md`](changes.md) once it lands.

## Desktop UI tools (open_preview, read_terminal, focus_pane…) never reach the ACP subprocess

**Found:** 2026-08-15

`toolsets.py:252-262` defines the `desktop_ui` toolset — `read_terminal`,
`close_terminal`, `open_preview`, `close_preview`, `read_preview`,
`drive_preview`, `annotate_preview`, `read_window_below`, `focus_pane`,
`react_to_message`, `setup_mcp`, `tour` — enabled only when the GUI
gateway detects a desktop-app session (`tui_gateway/server.py:5251`,
`surfaces.add("desktop_ui")`).

`agent/transports/hermes_tools_mcp_server.py:129-182` is the MCP tool
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
(`tui_gateway/server.py:11209-11225`), closed over the live WebSocket for a session. The MCP
server is a *separate* OS process — spawned per
`agent/copilot_acp_client.py:2150` (`-m
agent.transports.hermes_tools_mcp_server`) — so that `_emit` is never set
there; calling `open_preview` from inside it today would find no emitter
installed. The subprocess is only handed `HERMES_SESSION_ID`
(`copilot_acp_client.py:2139`), and that env var is currently read by exactly
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

Hermes' handshake in `agent/copilot_acp_client.py:2225-2247` never sends an
`elicitation` key:

```python
"clientCapabilities": {
    "fs": {"readTextFile": True, "writeTextFile": True},
    "_meta": {"jetbrains": {"air": {"version": 1,
                                     "capabilities": ["sessionFailure"]}}},
},
```

so `elicitationSupport.form` is always false and `AskUserQuestion` is
unconditionally in `disallowedTools`. This is **not** mode-gated — the
Bridge/Native `system_prompt_mode` switch (`_effective_system_prompt_mode()`,
`copilot_acp_client.py:1575`) only touches the `systemPrompt` payload sent at
`session/new`, never `clientCapabilities`, which is negotiated once at
`initialize` before any session opens (same "no resend RPC" constraint that
makes the Bridge pill pre-session-only). Confirmed via grep — no
`elicitation` string anywhere in `copilot_acp_client.py`.

**Fix:** two parts, not a flag flip.
1. Add `"elicitation": {"form": True}` (and maybe `"url"`) to the
   `clientCapabilities` dict at `copilot_acp_client.py:2225`.
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

`_requested_acp_mode()` (`agent/copilot_acp_client.py:969-994`) returns the
raw configured string. `_select_acp_mode()` (`:997-1029`) matches it against
`_acp_mode_ids(session)` exactly (`:1011`) then case-insensitively (`:1013`);
on no match it logs and **returns without sending `session/set_mode` at all**
(`:1014-1021`), leaving the child on whatever mode it started in. An empty
string takes the same no-RPC path. The docstring at `:972-974` states this is
deliberate — "an unset value leaves the agent on whatever mode it chose for
itself" — so *passthrough is a supported mode*; it simply has no id, so
nothing can offer it in a list built from advertised ids.

**The user reached it by accident.** `copilot_acp.permission_mode: auto` was
set in `~/.hermes/config.yaml`. `auto` is not an advertised id, so it fell
down the same `:1014-1021` no-match path as empty, no `session/set_mode` was
ever sent, and the child ran on its own start mode — which asked for zero
permissions, so Hermes' `session/request_permission` handler (`:3009-3256`)
never fired and no approval cards appeared. Changing the value to `default`
made the RPC land for the first time and the cards returned. Note the config
file is **not** validated on load: `tui_gateway/acp_session_modes.py:385-424`
rejects an unknown id with error 4002, but only for RPC-driven `config.set`,
so a junk value in YAML degrades silently into passthrough.

Two things are unresolved and must be settled before implementing:

1. **Why the child's own start mode asks for nothing.** Not established.
   `_select_acp_mode`'s docstring (`copilot_acp_client.py:1000-1003`) notes a `settings.json`
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
(`copilot_acp_client.py:1027` stays silent) and that the pill still reads back the chosen value
after a reconnect.

## Plugin-registered tools never reach an ACP subprocess (hindsight_retain and friends)

**Found:** 2026-08-26

`EXPOSED_TOOLS` (`agent/transports/hermes_tools_mcp_server.py:129-182`) is a
static tuple, and `_build_server()` iterates exactly it (`:306`). Nothing
consults the plugin registry. So a tool that a plugin registers at runtime can
never be offered to a native agent over the bridge, however configured it is.

The live case: `memory.provider: hindsight` registers `hindsight_retain`,
`hindsight_recall` and `hindsight_reflect`
(`plugins/memory/hindsight/__init__.py:355,386`). None is in `EXPOSED_TOOLS`.
Confirmed from inside a Claude-via-ACP session — the session's own system prompt
says *"Use hindsight_recall to search … hindsight_retain to store facts"*, and
none of the three is callable. The instruction is live, the tools are not.

Reads still work, which is what hides it. `HindsightProvider.prefetch()`
(`:1935`) and `sync_turn()` (`:2080`) run **server-side** in the Hermes process,
so recalled facts arrive injected into context and each turn is ingested
automatically. From the agent's side that looks like a working memory system
right up until it tries to retain a specific fact deliberately, which is exactly
when a user asks it to.

Net effect for this fork: the `memory` tool an ACP agent *can* reach is the
char-capped local store (which is what sits at 97% full), while the uncapped
provider the operator actually configured is write-only-by-accident — reachable
by automatic turn sync, unreachable on purpose.

Same shape as the `desktop_ui` gap above, one layer earlier: that one is a
missing name in the allowlist, this one is that the allowlist cannot express
plugin tools at all.

**Fix:** the narrow version is to fold the active memory provider's registered
tool names into `EXPOSED_TOOLS` at build time — the provider is already known
from `memory.provider`, and `_AGENT_LOOP_DISPATCH` (`:258`) is the precedent for
routing a name the plain dispatcher refuses. The broader version is to derive
the exposed set from the session's toolsets rather than a literal, which is what
"surface capability is a property of the SESSION" argues for in AGENTS.md.
Prefer the narrow one first; the broad one changes what every codex_app_server
run sees too.

Verify out-of-process per invariant 5: from a live Claude Sub ACP session, call
`hindsight_retain` and then confirm the fact comes back through a later
session's auto-recall — not merely that the tool appears in the list.
