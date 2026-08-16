# Change ledger

Every commit the fork carries on top of `upstream/main`, oldest first. Net diff
against the merge base: **67 files, +7,782 / −282**.

Last verified against `upstream/main` at `423f92e607d` (2026-08-13). When you
rebase, re-run the numbers below and re-check the `file.py:line` refs in
[surfaces.md](surfaces.md) and [wire-contracts.md](wire-contracts.md) — they are
the first thing an upstream merge invalidates.

Regenerate the raw numbers with:

```bash
MB=$(git merge-base HEAD upstream/main)
git diff --numstat "$MB"..main
for c in $(git rev-list --reverse "$MB"..main); do
  echo "--- $c $(git log -1 --format=%s "$c")"
  git show --pretty="" --numstat "$c"
done
```

## Ledger

### `43f3599` — Claude Code as a first-class ACP provider

The foundation. 25 files, +2,397 / −239; `copilot_acp_client.py` alone is
+1,423 / −150.

Turns the provider from prompt-scraping into a native ACP client:

- **Native tool mode.** When the target is a real agent, stop injecting a tool
  catalog into the prompt and stop parsing `<tool_call>` blocks out of the reply.
  `_resolve_tool_mode()` picks the mode; `HERMES_ACP_TOOL_MODE` forces it.
- **Persistent sessions.** One ACP session spans turns instead of one per
  completion. `_ensure_session()` builds or reuses; turn 2+ logs
  `ACP session REUSED: sending N new message(s)`.
- **Streaming.** `agent/conversation_loop.py` stops excluding `copilot-acp` from
  the streaming path; `_acp_stream_chunk()` shapes ACP updates into the chunk
  form the rest of Hermes expects.
- **Permission gate.** `session/request_permission` is routed to Hermes' real
  approval gate instead of being auto-answered.
- **Hermes tools over MCP.** `_hermes_tools_mcp_servers()` hands the agent a
  stdio MCP server exposing Hermes' own tool surface, so the native agent can
  reach `memory`, `session_search`, skills, web, and browser tools.
- **Cross-process memory.** `tools/memory_tool.py` + `agent/turn_context.py`
  reload a `MemoryStore` that another process has written, since the ACP agent
  and Hermes are separate processes.
- **Tool cards.** `agent/display.py` gains `build_tool_preview` fallback keys
  (`file_path`, `pattern`, `skill`, `description`, …) so a native agent's tools
  render a target instead of a blank card.
- **Desktop:** bare-fence streaming fixes in `markdown-preprocess.ts`, and an
  explicit `claude-opus-4-8` → "Opus 4.8" entry in `model-status-label.ts`.
- **CLI:** provider label `Claude Sub ACP`, Opus 4.8 catalog entry.

### `6f8c4b6` — per-target approval keys for ACP tool calls

`copilot_acp_client.py` only, +30 / −12. The approval pattern key becomes
`copilot-acp:{tool}:{sha256(target)[:12]}`. Choosing `[a]lways` on one path no
longer blesses every other path through the same tool, and content churn does
not invalidate the key because only the target string is hashed.

### `9e5814e` — expose `skill_manage` over the Hermes tools MCP bridge

The bridge exposed `skill_view`/`skills_list` but not `skill_manage`, so an ACP
agent could read the skill library and not maintain it.

### `1f898b8` — test ACP tool-call approval routing

+225 lines of test driving the real `_handle_server_message` with a fake
process. Written after the routing shipped broken once (see
[`wire-contracts.md`](wire-contracts.md) — permission RPCs carry no `toolName`).

### `fe73e9f` — plumb per-turn token usage from ACP prompt results

`_acp_usage_chunk()` reads the usage block off the `session/prompt` result so
per-turn token counts stop reading as zero.

### `00c714b` — auto-approve non-shell ACP tools via `approvals.tool_allowlist`

Adds `tools/approval.py::is_tool_allowlisted()` and `_match_tool_allowlist()`.
`command_allowlist` only ever reached shell commands; non-shell tools (`Edit`,
`Write`, `skill_manage`, …) had no auto-approval path at all and gated on exact
match. Grant-only: the list can approve, never deny.

### `2672ba7` — recover ACP permission `toolName` from the streamed `tool_call`

A real `session/request_permission` carries `kind`, not `toolName` — that rides
the `session/update` notifications. `_remember_tool_name()` / `_recall_tool_name()`
keep a call-id → name map from the stream so the permission card can show what
tool is actually asking.

### `dc9daf0` — build MoA reference advisors tool-less over ACP

`agent/auxiliary_client.py`, +14 / −2. `CopilotACPClient(advisory=True)` opens
the session with `tools: []` and no MCP servers, so a MoA reference advisor
holds zero tools. Also keys the client cache by task for `copilot-acp`, or an
advisory client could be handed to a `moa_aggregator` call.

### `9532b62` — log the background-review fork lifecycle at INFO

`agent/background_review.py`, +41. The fork had three `logger.warning` and zero
`logger.info`, so "never fired" and "fired and wrote nothing" looked identical.
Three INFO lines now: requested / fork starting / finished with an action count.

### `b7f7fc8` — offer ACP agent models in the setup wizard, not the GitHub catalog

`_model_flow_copilot_acp` branches on `_copilot_acp_is_rerouted()`. Rerouted, it
offers `provider_model_ids("copilot-acp")` and skips `fetch_github_model_catalog`
+ `normalize_copilot_model_id` — GitHub-id mappers with no Claude counterpart.
The `/model` picker already took this route; only the wizard showed GitHub ids.

### `9611aca` — per-session ACP permission mode, a desktop pill, and config MCP forwarding

24 files, +1,606 / −10. Three related pieces:

**1. Per-session permission mode.** `_requested_acp_mode()` was module-level and
read global config, so two panes shared one mode. Resolution is now a ladder on
the client instance — `_effective_acp_mode()` consults a session override ahead
of env and config — with `set_permission_mode()` / `permission_mode_state()` as
the public surface. `_sync_acp_mode()` re-applies a *changed* mode to a live
session via `session/set_mode`; no session rebuild.

**2. Desktop permission pill.** `permission-mode-pill.tsx` in the composer,
shaped after `model-pill.tsx` (view-scoped via `useSessionView()`, so panes do
not bleed). Visible only when the backend says `acp_permission.available`;
`bypassPermissions` behind a confirm. Wire path is the gateway `config.get` /
`config.set` key `permission_mode`, plus an `acp_permission` block on
`session.info`.

**3. Config MCP forwarding.** `_acp_mcp_server_entry()` translates a
`config.yaml` `mcp_servers:` entry into ACP shape (stdio and http/sse);
`_config_mcp_servers()` gathers and filters them; both are concatenated with
`hermes-tools` at `session/new`. Four guards: `${VAR}` placeholders must be
expanded (`load_config_readonly`, not `read_raw_config`) or the entry is dropped,
`enabled: false` is honoured, both tool-suppression paths still return `[]`, and
a config entry cannot shadow `hermes-tools`. Kill switch:
`HERMES_ACP_CONFIG_MCP=off`.

### `69b6454` — memory/skill standing instructions, and the Bridge pill

Two related pieces, both about who owns the ACP session's system prompt. They
interleave in `_build_session_meta`, so they landed together.

**1. Memory/skill standing instructions.** `_HERMES_MEMORY_INSTRUCTIONS` +
`_hermes_system_prompt_append()` name the `mcp__hermes-tools__*` tools in
`_meta.systemPrompt.append`, so the agent stops satisfying "remember that" from
its own memory directory — a store Hermes cannot read. Config:
`copilot_acp.hermes_memory_instructions` (default true). Suppressed for
advisory sessions, kept for the restricted review fork. See
[`surfaces.md`](surfaces.md).

**2. Bridge/native system-prompt mode.** A composer pill that switches the
session between riding on Claude Code's preset (`bridge`) and replacing it with
Hermes' own full system prompt (`native`). Config default
`copilot_acp.system_prompt_mode`; per-session pick on the client
(`set_system_prompt_mode` / `system_prompt_mode_state`), published as
`acp_system_prompt_mode` on `session.info`, written through gateway
`config.set`.

Four things here that are not obvious from the shape, each of which was a bug
first:

- **Native is a plain string, and a string replaces the preset.** The SDK
  accepts both shapes and errors on neither, so the wrong one boots clean and
  degrades silently. Now pinned in [`wire-contracts.md`](wire-contracts.md).
- **Native has no `append` channel**, so the memory block and the operator's
  `system_prompt_append` are concatenated rather than dropped. Dropping them
  cost native mode the block that maps bare tool names onto
  `mcp__hermes-tools__*` — the mode that needs it most.
- **Hermes' prompt describes Hermes' toolset, which is not this session's.**
  `_NATIVE_TOOL_NAME_MAPPING` reconciles: `EXPOSED_TOOLS` crosses prefixed,
  `terminal`/`read_file`/`write_file` do not cross at all and the agent's own
  `Bash`/`Read`/`Write` cover that ground.
- **The editable window is the draft, and only the draft.** `systemPrompt` is
  sent once at `session/new` and has no re-send RPC, so `locked` goes true on
  the first turn. The desktop parks the pick in `$draftAcpSystemPromptMode`
  (sticky localStorage) and replays it in `createBackendSessionForSend`, in the
  gap between `session.create` and the first prompt. Without that replay the
  pill is unreachable in every state a user can actually get to.

Excluded from native mode regardless of the pick: advisory sessions and the
background memory/skill review fork.

## File map

Where the fork touches upstream code, and what to check after a rebase.

### Python — core

| File | Δ | Role |
|---|---|---|
| `agent/copilot_acp_client.py` | +2023 / −156 | The fork. Native tool mode, sessions, streaming, permission gate, thinking, modes, MCP wiring |
| `agent/transports/hermes_tools_mcp_server.py` | +126 / −11 | `memory`, `session_search`, `skill_manage` added to the exposed tool surface |
| `agent/auxiliary_client.py` | +14 / −2 | Advisory (tool-less) client for `moa_reference`; task-keyed client cache |
| `agent/background_review.py` | +41 | INFO lifecycle logging; stamps `_acp_restrict_to_hermes_tools` |
| `agent/agent_runtime_helpers.py` | +4 | `client.bind_agent(agent)` |
| `agent/conversation_loop.py` | +14 / −9 | Stops excluding `copilot-acp` from streaming |
| `agent/display.py` | +13 / −1 | `build_tool_preview` fallback keys |
| `agent/turn_context.py` | +12 | MemoryStore staleness reload |
| `tools/memory_tool.py` | +71 | Cross-process MemoryStore sync |
| `tools/approval.py` | +48 | `approvals.tool_allowlist` for non-shell tools |

### Python — CLI, config, gateway

| File | Δ | Role |
|---|---|---|
| `hermes_cli/config_defaults.py` | +33 | The `copilot_acp:` config block |
| `hermes_cli/model_setup_flows.py` | +55 / −33 | Wizard offers agent models when rerouted |
| `hermes_cli/model_switch.py` | +22 | `/model` picker routing |
| `hermes_cli/models.py` | +48 / −2 | `_copilot_acp_is_rerouted()`, Opus 4.8 entry |
| `hermes_cli/providers.py`, `auth.py` | +1 / −1 each | Provider label `Claude Sub ACP` |
| `plugins/model-providers/copilot-acp/__init__.py` | +2 / −2 | Plugin metadata |
| `tui_gateway/server.py` | +215 | `acp_permission` on `session.info`; `config.set permission_mode`; per-turn mode apply |
| `tui_gateway/methods_config.py` | +10 | `config.get permission_mode` |

### Desktop (TypeScript — needs a rebuild to take effect)

| File | Δ | Role |
|---|---|---|
| `app/chat/composer/permission-mode-pill.tsx` | +190 | The pill |
| `app/chat/composer/bridge-mode-pill.tsx` | +150 | The Bridge/Native toggle. Editable on a draft, locked once the ACP session opens |
| `lib/acp-permission.ts` | +95 | State shape, normalizer, `setSessionPermissionMode()` |
| `lib/acp-system-prompt-mode.ts` | +95 | Same for bridge/native; `setSessionSystemPromptMode()` |
| `app/session/hooks/use-session-actions/index.ts` | +25 | Replays the sticky draft pick onto the new session, before the first turn |
| `app/types.ts` | +22 | `AcpPermissionState` |
| `i18n/en.ts`, `zh.ts`, `types.ts` | +50 | Pill strings |
| `store/session.ts` | +17 / −1 | Per-view permission atom |
| `app/session/hooks/use-message-stream/{gateway-event,utils}.ts` | +29 / −1 | `acp_permission` off `session.info` |
| `app/chat/{session-view,session-tile}.tsx`, `composer/controls.tsx` | +15 / −1 | Mounting and view scoping |
| `lib/{chat-messages,chat-runtime,icons}.ts` | +9 | Plumbing and the shield icon |
| `lib/markdown-preprocess.ts` | +66 / −7 | Bare-fence streaming fix |
| `lib/model-status-label.ts` | +28 | `claude-opus-4-8` → "Opus 4.8" |

### Tests

| File | Δ |
|---|---|
| `tests/agent/test_copilot_acp_approval_routing.py` | +530 |
| `tests/agent/test_copilot_acp_client.py` | +270 / −1 |
| `tests/agent/test_copilot_acp_system_prompt_mode.py` | +280 (bridge/native wire shape, exclusions, lock) |
| `tests/agent/transports/test_hermes_tools_mcp_server.py` | +258 |
| `tests/tools/test_memory_disk_sync.py` | +128 |
| `tests/tools/test_approval_tool_allowlist.py` | +85 |
| `tests/agent/test_copilot_acp_usage.py` | +79 |
| `tests/run_agent/test_streaming.py` | +53 / −47 |
| `tests/hermes_cli/test_setup_model_provider.py` | +52 / −1 |
| `tests/agent/test_empty_tool_name_loop_dampening.py` | +17 / −2 (restores `sys.modules` — upstream bug, see verification) |
| `tests/hermes_cli/test_{api_key_providers,model_validation}.py` | +1 / −1 each (label) |
| Desktop `*.test.tsx` / `*.test.ts` | +386 across 5 files |
