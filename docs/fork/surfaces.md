# Surfaces

Everything the fork exposes to a user, an operator, or another part of the app.
If you add one, add it here.

## Config keys

Live in `DEFAULT_CONFIG` (`hermes_cli/config_defaults.py`, after `bedrock`).
`CONFIG_SCHEMA` derives itself from `DEFAULT_CONFIG`, so `hermes config set` and
the dashboard form work with no extra wiring.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `copilot_acp.permission_mode` | str | `""` | Mode requested via `session/set_mode`. Empty leaves the agent on its own choice. `claude-agent-acp` advertises `default`, `plan`, `acceptEdits`, `bypassPermissions` |
| `copilot_acp.thinking_display` | str | `""` | `summarized` puts real thinking text on the wire; `omitted` streams signature-only blocks (blank Thought pane); `off` leaves the option unset. Empty is treated as `summarized` |
| `copilot_acp.system_prompt_append` | str | `""` | Text appended to the agent's own system prompt. Rides **after** the memory block below, so it can override it |
| `copilot_acp.hermes_memory_instructions` | bool | `true` | Prepend the fork's memory/skill standing instructions to that append. See below |
| `copilot_acp.allowed_tools` | list | `[]` | Tool-name allow list, e.g. `["Bash(git status)", "Read"]` |
| `copilot_acp.disallowed_tools` | list | `[]` | Deny list; concatenated with the agent's own |
| `copilot_acp.additional_directories` | list | `[]` | Extra readable dirs (the `--add-dir` equivalent) |
| `approvals.tool_allowlist` | list | — | Globs matching non-shell tool keys that auto-approve. Grant-only — cannot deny |
| `mcp_servers.<name>` | map | — | Upstream key. The fork now **forwards these to the ACP agent** (see below) |

Read on the Python side by `_acp_config` / `_acp_config_str` / `_acp_config_list`
(`copilot_acp_client.py:506,525,531`) — lazy `load_config_readonly`, so probe
scripts and early CLI startup cannot die on a missing config.

**These are read once per ACP session, at `session/new`.** With persistent
sessions on (the default) an existing chat keeps what it started with. The one
exception is `permission_mode`, which `_sync_acp_mode()` re-applies to a live
session.

### Memory / skill standing instructions

`_HERMES_MEMORY_INSTRUCTIONS` + `_hermes_system_prompt_append()`
(`copilot_acp_client.py:546`) put a fixed block into
`_meta.systemPrompt.append` naming the `mcp__hermes-tools__*` memory, skill and
session-search tools, and telling the agent not to satisfy a memory or skill
request from its own built-in store.

Why it is needed at all — a native agent brings two things Hermes does not
control:

1. **Tool search.** Claude Code withholds MCP tool *schemas* on a first-party
   host and surfaces only names until a `ToolSearch` call fetches them. A tool
   with no schema is one the model never spontaneously reaches for, so
   `memory` and `skill_manage` simply never fire. Disabled at the transport by
   `ENABLE_TOOL_SEARCH=false` in the launcher's env allowlist (see the
   `hermes-fork-maintenance` skill — the launcher lives outside this repo).
2. **A competing store.** Claude Code has its own memory directory and skill
   loader, both named in its *real* system prompt. Left alone it writes there,
   somewhere Hermes cannot read. Shut off at the transport by
   `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the same launcher env allowlist —
   that var is read before the `autoMemoryEnabled` setting, so it scopes to ACP
   and a plain `claude` CLI session keeps its own memory.

Placement is the point: anything Hermes sends as a `system` message is rendered
into the `Conversation transcript:` block by `_render_prompt`, where it reads as
relayed context and loses to the agent's actual system prompt. The append path
is the only route to instruction-level priority.

Suppressed for `advisory=True` sessions, which get no `mcpServers` at all;
**kept** for the restricted background-review fork, which keeps hermes-tools and
is exactly the caller that must write memories and skills.

Pinned by `~/.hermes-acp/probe_system_prompt_append.py` (12 checks, including an
opted-out arm and an advisory arm as negative controls).

### Bridge vs native (`system_prompt_mode`)

Which system prompt the session runs on. Two values:

| Value | `_meta.systemPrompt` | Effect |
|---|---|---|
| `bridge` (default) | `{"append": …}` | Hermes' text rides on top of Claude Code's own preset |
| `native` | plain string | Hermes' full `build_system_prompt_parts` output **replaces** the preset — the session runs as Hermes, not as Claude Code |

The object/string split and why it is silent when wrong are in
[`wire-contracts.md`](wire-contracts.md). What matters here:

- The fork's own text (memory/skill block + operator `system_prompt_append`)
  applies in **both** modes. Native has no `append` channel, so it is
  concatenated last.
- Native mode carries a tool-name reconciliation block, because Hermes' prompt
  is rendered off `agent.valid_tool_names` and only `EXPOSED_TOOLS` crosses the
  MCP boundary (prefixed). Without it the prompt names tools the session does
  not have.
- **Excluded regardless of the pick**: advisory sessions (no agent to build a
  prompt from, no tools to use it with) and the background memory/skill review
  fork (runs deliberately tool-starved against its own harness prompt; the full
  Hermes operating brief would talk over it).

#### Per-session pick and the `locked` contract

`config.yaml`'s `copilot_acp.system_prompt_mode` is only the **default for new
chats**. The live pick is per-session, on the `CopilotACPClient`
(`set_system_prompt_mode` / `system_prompt_mode_state`), reported on
`session.info` as `acp_system_prompt_mode` and set through gateway
`config.set {key: "acp_system_prompt_mode", session_id, value}` — the same
session-scoped shape as `permission_mode`.

The difference from permission mode is the one that shapes the UI. There is no
`session/set_mode` equivalent for `systemPrompt`, so:

- `locked` goes true the moment `session/new` fires, and `config.set` **rejects**
  the write rather than pocketing a pick that can never take effect.
- `locked` is scoped to the ACP **session**, not the chat. Anything that tears
  the subprocess down clears `_session_id` and reopens the window — a mid-chat
  model switch is the realistic case, and that is the intended escape hatch.
- The editable window is therefore the **draft**, before the first turn. The
  desktop keeps that pick in `$draftAcpSystemPromptMode` (sticky localStorage,
  same contract as model/effort/fast) and `createBackendSessionForSend` replays
  it onto the new session between `session.create` and the first prompt — the
  same way an armed YOLO is replayed. That replay is not a convenience; it is
  the only delivery path.

Composer surface: `BridgeModePill` (`apps/desktop/src/app/chat/composer/`), a
click-toggle rather than a dropdown, gated on `available` exactly like
`PermissionModePill`.

Pinned by `~/.hermes-acp/probe_system_prompt_mode.py` (17 checks, advisory and
restricted arms as negative controls), `tests/agent/test_copilot_acp_system_prompt_mode.py`
and `apps/desktop/src/app/chat/composer/bridge-mode-pill.test.tsx`.

### MCP forwarding contract

`config.yaml`'s `mcp_servers:` entries are translated to ACP shape and passed at
`session/new`, alongside the built-in `hermes-tools` bridge. Translation is
`_acp_mcp_server_entry()` (`copilot_acp_client.py:572`); gathering and filtering
is `_config_mcp_servers()` (`:1799`).

Rules, each pinned by a test in `tests/agent/test_copilot_acp_client.py`:

- `command` wins over `url` — an entry with both is stdio.
- `enabled: false` is dropped. (`config.py` auto-disables exfiltration-shaped
  entries after migration; forwarding blindly would re-enable them.)
- An unexpanded `${VAR}` in any value drops the entry with a warning. Use
  `load_config_readonly()`, never `read_raw_config()` — the raw reader returns
  the literal `${...}` and a placeholder API key fails as a silent 401.
- A config entry named `hermes-tools` is dropped; the bridge wins.
- Both tool-suppression paths (`advisory`, `_restrict_to_hermes_tools`) still
  return `[]`, so a background review cannot gain network MCP access.

Passing `mcpServers` at `session/new` **merges** with what the agent's own
settings load — it does not replace them. See [`wire-contracts.md`](wire-contracts.md).

## Environment variables

Env always beats config, so a launcher can pin something a user's config cannot
loosen.

| Var | Effect |
|---|---|
| `HERMES_COPILOT_ACP_COMMAND` | The subprocess to run. Setting it to `claude-agent-acp` is what makes `copilot-acp` "Claude" on this machine. Also the truth test behind `_copilot_acp_is_rerouted()` |
| `HERMES_COPILOT_ACP_ARGS` | Extra args for that subprocess |
| `HERMES_ACP_TOOL_MODE` | Force `native` or `bridge` instead of letting `_resolve_tool_mode()` decide |
| `HERMES_ACP_PERSISTENT_SESSION` | Turn persistent sessions off |
| `HERMES_ACP_PERMISSION_MODE` | Pin the permission mode. Wins over config *and* the UI; surfaces to the desktop as `locked: true` |
| `HERMES_ACP_THINKING_DISPLAY` | Pin the thinking display |
| `HERMES_ACP_HERMES_TOOLS` | Turn the hermes-tools MCP bridge off |
| `HERMES_ACP_CONFIG_MCP` | `off` disables config MCP forwarding |

## Client API

`CopilotACPClient` (`agent/copilot_acp_client.py:1179`). The methods other parts
of the app are allowed to call:

| Method | Line | Contract |
|---|---|---|
| `bind_agent(agent)` | 1263 | Weakref to the owning `AIAgent`. The client is built inside `AIAgent.__init__`, so markers the fork stamps *after* construction are read off the bound agent, not a constructor arg |
| `set_permission_mode(mode)` | 1311 | Sets the session override and returns the effective mode. Idempotent — a no-op when unchanged, so it is safe to call every turn |
| `permission_mode_state()` | 1329 | `{value, source, options, locked}` for a UI to render |
| `close()` | 1354 | Shut the subprocess down |

Resolution ladder, `_effective_acp_mode()` (`:1301`) then `_requested_acp_mode()`
(`:673`): **session override → `HERMES_ACP_PERMISSION_MODE` → config → `""`**.
`source` reports which rung won.

## Gateway RPC

Added in `tui_gateway/`. The desktop pill uses these; so can any other surface.

**Implementation lives in `tui_gateway/acp_session_modes.py`**, a fork-only
module — upstream has no file at that path, so it cannot conflict on a rebase.
`server.py` keeps four call sites (~42 lines, down from 371) and re-exports
`_acp_permission_mode_info` / `_acp_system_prompt_mode_info` by importing them.
**That re-export is load-bearing:** `methods_config.py` handler bodies are
rebound onto `server.py`'s globals at install time (`method_ctx.py`) and resolve
those two names from there at call time, so dropping either import breaks
`config.get`. The `config.set` arms are delegated to `handle_acp_config_set()`,
which takes `_ok` / `_err` / `_emit` / `_session_info` as injected arguments —
the module never imports `server.py`, because the reverse import would be a cycle.

### `session.info` → `acp_permission`

Published on every `session.info`. Built by `_acp_permission_mode_info()`
(`tui_gateway/acp_session_modes.py`).

```jsonc
{
  "available": true,      // visibility gate — the ONLY reroute test a renderer may use
  "value": "plan",        // effective mode; "" means "leave the agent alone"
  "source": "session",    // env | session | config | agent
  "locked": false,        // operator pinned HERMES_ACP_PERMISSION_MODE
  "options": ["default", "plan", "acceptEdits", "bypassPermissions"]
}
```

When the session is not Claude-over-ACP every field is falsy and `available` is
`false`. That is the whole gating contract — see invariant 1 in the
[README](README.md).

### `config.get` / `config.set`, key `permission_mode`

`config.get` returns the same block `session.info` publishes, so a surface that
polls and a surface that listens can never disagree about `available`/`locked`.

`config.set` requires a `session_id` (`4002 permission_mode requires a session`)
and refuses when the session is not Claude-over-ACP
(`4002 permission_mode is only available on Claude over ACP`). It validates
against the agent's advertised `options` before applying.

The override is stored on the session dict under `permission_mode_override`, not
only on the client — a model switch rebuilds the client, and the pick must
survive that. `_apply_session_permission_mode()` re-applies it at turn start.

**No mid-run guard.** Model switching refuses while the agent is running;
`session/set_mode` is valid on a live session, so mode switching does not need it.

## Desktop UI

`AcpPermissionState` (`apps/desktop/src/app/types.ts`) mirrors the
`acp_permission` block one-for-one.

| Piece | Path |
|---|---|
| The pill | `app/chat/composer/permission-mode-pill.tsx` |
| State, normalizer, RPC call | `lib/acp-permission.ts` — `normalizeAcpPermission`, `acpPermissionEquals`, `setSessionPermissionMode`, `EMPTY_ACP_PERMISSION`, `BYPASS_PERMISSION_MODE` |
| Per-view atom | `store/session.ts` |
| Mount point | `app/chat/composer/controls.tsx` |
| Strings | `i18n/en.ts`, `i18n/zh.ts`, `i18n/types.ts` |

Scoped per view via `useSessionView()`, following `model-pill.tsx` — two panes
hold independent modes. `bypassPermissions` shows a confirm before applying.
When `locked` is true the control renders disabled with an explanation rather
than silently failing.

**The desktop is TypeScript compiled into `resources/app.asar`.** A source edit
does nothing to the running app until `npm run build`, `npm run builder`, and a
binary swap. Restarting Hermes reloads the *old* bundle. Python changes, by
contrast, do take effect on restart.
