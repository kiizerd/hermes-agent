# Change ledger

Every commit the fork carries on top of `upstream/main`, oldest first. Net diff
against the merge base: **70 files, +8,459 / −283**.

Last verified against `upstream/main` at `00c12dac613` (2026-08-16). When you
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

### `7317630d8d9` — Claude Code as a first-class ACP provider

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

### `12c997ba522` — per-target approval keys for ACP tool calls

`copilot_acp_client.py` only, +30 / −12. The approval pattern key becomes
`copilot-acp:{tool}:{sha256(target)[:12]}`. Choosing `[a]lways` on one path no
longer blesses every other path through the same tool, and content churn does
not invalidate the key because only the target string is hashed.

### `60d33231eb9` — expose `skill_manage` over the Hermes tools MCP bridge

The bridge exposed `skill_view`/`skills_list` but not `skill_manage`, so an ACP
agent could read the skill library and not maintain it.

### `7d92ffd32d2` — test ACP tool-call approval routing

+225 lines of test driving the real `_handle_server_message` with a fake
process. Written after the routing shipped broken once (see
[`wire-contracts.md`](wire-contracts.md) — permission RPCs carry no `toolName`).

### `9d1cd12d0cb` — plumb per-turn token usage from ACP prompt results

`_acp_usage_chunk()` reads the usage block off the `session/prompt` result so
per-turn token counts stop reading as zero.

### `bf910f5c497` — auto-approve non-shell ACP tools via `approvals.tool_allowlist`

Adds `tools/approval.py::is_tool_allowlisted()` and `_match_tool_allowlist()`.
`command_allowlist` only ever reached shell commands; non-shell tools (`Edit`,
`Write`, `skill_manage`, …) had no auto-approval path at all and gated on exact
match. Grant-only: the list can approve, never deny.

### `d48c5b9f6f8` — recover ACP permission `toolName` from the streamed `tool_call`

A real `session/request_permission` carries `kind`, not `toolName` — that rides
the `session/update` notifications. `_remember_tool_name()` / `_recall_tool_name()`
keep a call-id → name map from the stream so the permission card can show what
tool is actually asking.

### `7bc5cff06fc` — build MoA reference advisors tool-less over ACP

`agent/auxiliary_client.py`, +14 / −2. `CopilotACPClient(advisory=True)` opens
the session with `tools: []` and no MCP servers, so a MoA reference advisor
holds zero tools. Also keys the client cache by task for `copilot-acp`, or an
advisory client could be handed to a `moa_aggregator` call.

### `18d0d4db077` — log the background-review fork lifecycle at INFO

`agent/background_review.py`, +41. The fork had three `logger.warning` and zero
`logger.info`, so "never fired" and "fired and wrote nothing" looked identical.
Three INFO lines now: requested / fork starting / finished with an action count.

### `abe35b6e10e` — offer ACP agent models in the setup wizard, not the GitHub catalog

`_model_flow_copilot_acp` branches on `_copilot_acp_is_rerouted()`. Rerouted, it
offers `provider_model_ids("copilot-acp")` and skips `fetch_github_model_catalog`
+ `normalize_copilot_model_id` — GitHub-id mappers with no Claude counterpart.
The `/model` picker already took this route; only the wizard showed GitHub ids.

### `a8cfe70a10a` — per-session ACP permission mode, a desktop pill, and config MCP forwarding

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

### `3b99488b985` — track the Claude ACP launcher in-repo

`claude-acp/claude-acp-run.js` +126 (new), `claude-acp/claude-acp-run.sh` +102
(new), plus doc updates in `upstreaming.md` and `verification.md`.

The launcher had lived untracked at `~/.hermes-acp/` since the fork started,
purely because it sat beside the probe harnesses. The probes are excluded for a
real reason — they hardcode machine paths — but the launcher carries none, so
that reason never applied to it. Untracked meant it was in **no backup**: the
update script bundles refs and diffs tracked files, and neither reaches a file
outside the repo.

Zero rebase risk, permanently: upstream has no file at this path, so the commit
is additive forever.

Two constraints the file encodes, both decoded out of the compiled `claude.exe`
rather than guessed — see [`surfaces.md`](surfaces.md):

- `ENABLE_TOOL_SEARCH=false` puts the SDK in `standard` mode so MCP tool
  *schemas* load up front. Left at the default, `mcp__hermes-tools__memory` and
  `skill_manage` arrive as bare names the model can't call, which is half of why
  Claude never wrote memories.
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` kills Claude Code's own auto-memory store,
  which under Hermes is a second memory the user never sees. The env var is read
  *before* the `autoMemoryEnabled` setting, so it scopes to ACP only — a plain
  `claude` CLI session is unaffected.

The directory must never be renamed to anything containing "copilot":
`_resolve_tool_mode()` substring-tests the whole argv and would silently flip the
provider back to bridge mode.

Upstream-bound: no. This is fork infrastructure.

### `721a1d3cd33` — memory/skill standing instructions, and the Bridge pill

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

### `5c1d1aafe2e` — context window for bare Claude Code aliases

`agent/model_metadata.py` +68, `tests/agent/test_acp_claude_alias_context.py`
+146 (new).

The `copilot-acp` picker advertises claude-agent-acp's short aliases — `opus`,
`sonnet`, `haiku` — which have no vendor prefix. `get_model_context_length()`
fuzzy-matches its catalog as substrings, so none of them matched anything and
all three fell to `DEFAULT_FALLBACK_CONTEXT` (256K) against a real 1M window.
Observable as a 4x under-report on the desktop context gauge, and a compressor
summarizing at roughly a quarter of the window. The two prefixed entries in the
same picker (`claude-fable-5[1m]`, `claude-opus-4-8`) already resolved to 1M,
which is why the bug read as "only some Claude models are wrong."

`_ACP_CLAUDE_ALIAS_CONTEXT` is a separate exact-match table consulted at step
5a0, before the GitHub Copilot `/models` branch. Rationale for keeping it out of
`DEFAULT_CONTEXT_LENGTHS`, why `haiku` is 200K, and why the values are never
cached to disk are in [`wire-contracts.md`](wire-contracts.md) §Model selection.

The test file asserts the contract, not the numbers: aliases must not equal
`DEFAULT_FALLBACK_CONTEXT`, frontier aliases must equal their concrete catalog
entries, and every bare alias in the picker must have a table entry — so adding
one to `_PROVIDER_MODELS["copilot-acp"]` without a context entry fails a test
rather than silently reintroducing the fallback.

Upstream-bound: the aliases and the resolver are both upstream code.

### `205cef42f92` — run the ACP child in the session's selected project

`agent/copilot_acp_client.py` +50 / −2, `tests/agent/test_copilot_acp_client.py`
+83.

`acp_cwd` was only ever honoured when a caller passed it explicitly (tests, CLI
overrides). The normal gateway path never did, so every Claude-over-ACP session
started in `os.getcwd()` — wherever the Hermes backend process happened to be
launched — instead of the project the user picked in the desktop.

`_resolve_acp_cwd()` honours an explicit `acp_cwd` first, then falls back to the
session's recorded cwd via the bound agent's `_gateway_session_key`
(`tools.terminal_tool.get_session_cwd`), and only then to `os.getcwd()`.

Two ordering constraints, both load-bearing:

- **Resolution is lazy and re-run in `_spawn_process`**, not computed once at
  construction. `bind_agent()` runs *after* the client is built, so at
  construction there is no agent to read a session key off. Re-running at every
  spawn also means a mid-session project switch is picked up.
- **`_agent()` reads `self._agent_ref` via `getattr` with a default**, because
  `_resolve_acp_cwd` can now run before `bind_agent` has ever been called.

Upstream-bound: yes. Any ACP provider wants the child in the session's project.

### `8e4e132dad0` — pass `single_query_deny_message` to the approval gate

`agent/copilot_acp_client.py` +6.

Upstream `1596148ff22 fix(approval): deterministic approvals.single_query_mode
for -q sessions` (2026-08-15) added a **required** keyword-only
`single_query_deny_message: str` to `_run_approval_gate` (`tools/approval.py`).
Our non-shell tool call site did not pass it, so the branch raised
`TypeError: _run_approval_gate() missing 1 required keyword-only argument`
instead of presenting an approval card.

This is the rebase failure mode that no textual check catches: upstream changed
the *signature* in `approval.py`, the fork's call site lives in
`copilot_acp_client.py`, and git had nothing to conflict on. The merge-base
overlap scout, the `merge-tree` rehearsal and the CRLF pass were all clean.

Blast radius while broken: non-shell ACP tool approvals only. Shell commands
route through `check_dangerous_command` on a separate branch, and anything
matching `approvals.tool_allowlist` short-circuits before the gate — which is
why the break stayed invisible in normal use.

Caught by `test_always_on_one_path_does_not_bless_another`, which spies on the
**real** `_run_approval_gate` rather than substituting a permissive fake. Keep
that shape for every fork call site into upstream code; a stub would have
swallowed the signature change.

Upstream has the identical omission at its own `tools/file_tools.py:1005`
(`ssh_config_write`) — untouched by the fork, so that one is upstream's to fix.

### `d6559b01d4f` — move ACP session modes out of `tui_gateway/server.py`

4 files, +492 / −366 (this commit's own diff). Measured against the merge base
instead, `tui_gateway/server.py` goes from +371 to **+42**; its content lands in
a new fork-only `tui_gateway/acp_session_modes.py` (+434).

The first deliberate footprint reduction rather than a feature. `server.py` is
the hottest file the fork touches by a wide margin — **485 upstream commits in
90 days**, against 296 for `conversation_loop.py` and 14 for
`copilot_acp_client.py`. Biggest patch is not the same as biggest risk, and
`server.py` was carrying 371 fork lines into the file most likely to be rewritten
underneath them.

What moved: seven `_acp_*` helpers, three constants, and the two `config.set`
arms. All of it purely additive — code upstream has no concept of. What stayed:
four call sites (the import, a `**`-unpack in the `session.info` payload, one
`apply_session_acp_modes()` at turn start, one delegation out of the `config.set`
chain).

**The import re-exports `_acp_permission_mode_info` and
`_acp_system_prompt_mode_info` even though `server.py` no longer calls either.**
That is load-bearing: `methods_config.py` handler bodies are rebound onto
`server.py`'s globals at install time (`method_ctx.py`) and resolve those two
names from there at call time, so dropping either import breaks `config.get`
with a `NameError`. The new module imports nothing from `server.py` in return —
`handle_acp_config_set()` takes `_ok` / `_err` / `_emit` / `_session_info` as
injected arguments, because the reverse import would be a cycle.

**The rule this follows, for the next extraction:** move code that is *additive*
to upstream; leave code that *modifies* upstream's own logic where it is.
Isolating a modification means overriding an upstream function, which trades a
loud failure (a conflict marker you must resolve) for a silent one (your override
shadows a future upstream bugfix with no signal). That is why
`agent/conversation_loop.py` (+14 / −9, and those 9 deletions are upstream's own
`elif`) and `agent/copilot_acp_client.py` (+2,306 / −157) stay put despite being
the two largest remaining surfaces.

Verified by `probe_mode_rpc.py` and `probe_session_mode_override.py` (both ALL
PASS after being repointed at the new module), `tests/test_tui_gateway_server.py`
(585 passed under `run_tests.sh`), and the gating set (811 passed, 1 pre-existing
`test_ping_suppression` failure).

## File map

Where the fork touches upstream code, and what to check after a rebase.

### Python — core

| File | Δ | Role |
|---|---|---|
| `agent/copilot_acp_client.py` | +2306 / −157 | The fork. Native tool mode, sessions, streaming, permission gate, thinking, modes, MCP wiring, project cwd |
| `agent/transports/hermes_tools_mcp_server.py` | +126 / −11 | `memory`, `session_search`, `skill_manage` added to the exposed tool surface |
| `agent/auxiliary_client.py` | +15 / −3 | Advisory (tool-less) client for `moa_reference`; task-keyed client cache |
| `agent/model_metadata.py` | +68 | `_ACP_CLAUDE_ALIAS_CONTEXT` — context window for bare `opus`/`sonnet`/`haiku` aliases |
| `agent/background_review.py` | +40 | INFO lifecycle logging; stamps `_acp_restrict_to_hermes_tools` |
| `agent/agent_runtime_helpers.py` | +4 | `client.bind_agent(agent)` |
| `agent/conversation_loop.py` | +14 / −9 | Stops excluding `copilot-acp` from streaming |
| `agent/display.py` | +13 / −1 | `build_tool_preview` fallback keys |
| `agent/turn_context.py` | +12 | MemoryStore staleness reload |
| `tools/memory_tool.py` | +71 | Cross-process MemoryStore sync |
| `tools/approval.py` | +48 | `approvals.tool_allowlist` for non-shell tools |

### Python — CLI, config, gateway

| File | Δ | Role |
|---|---|---|
| `hermes_cli/config_defaults.py` | +46 | The `copilot_acp:` config block |
| `hermes_cli/model_setup_flows.py` | +55 / −33 | Wizard offers agent models when rerouted |
| `hermes_cli/model_switch.py` | +22 | `/model` picker routing |
| `hermes_cli/models.py` | +48 / −2 | `_copilot_acp_is_rerouted()`, Opus 4.8 entry |
| `hermes_cli/providers.py`, `auth.py` | +1 / −1 each | Provider label `Claude Sub ACP` |
| `plugins/model-providers/copilot-acp/__init__.py` | +2 / −2 | Plugin metadata |
| `tui_gateway/server.py` | +42 | Call sites only — import (re-exports the two `_info` fns for `methods_config.py`), `session.info` unpack, turn-start apply, `config.set` delegation |
| `tui_gateway/methods_config.py` | +21 | `config.get permission_mode` |

### Desktop (TypeScript — needs a rebuild to take effect)

| File | Δ | Role |
|---|---|---|
| `app/chat/composer/permission-mode-pill.tsx` | +190 | The pill |
| `app/chat/composer/bridge-mode-pill.tsx` | +154 | The Bridge/Native toggle. Editable on a draft, locked once the ACP session opens |
| `lib/acp-permission.ts` | +95 | State shape, normalizer, `setSessionPermissionMode()` |
| `lib/acp-system-prompt-mode.ts` | +93 | Same for bridge/native; `setSessionSystemPromptMode()` |
| `app/session/hooks/use-session-actions/index.ts` | +24 | Replays the sticky draft pick onto the new session, before the first turn |
| `app/types.ts` | +58 | `AcpPermissionState` |
| `i18n/en.ts`, `zh.ts`, `types.ts` | +72 | Pill strings |
| `store/session.ts` | +49 / −1 | Per-view permission atom |
| `app/session/hooks/use-message-stream/{gateway-event,utils}.ts` | +41 / −1 | `acp_permission` off `session.info` |
| `app/chat/{session-view,session-tile}.tsx`, `composer/controls.tsx` | +32 / −1 | Mounting and view scoping |
| `lib/{chat-messages,chat-runtime,icons}.ts` | +14 | Plumbing and the shield icon |
| `lib/markdown-preprocess.ts` | +66 / −7 | Bare-fence streaming fix |
| `lib/model-status-label.ts` | +28 | `claude-opus-4-8` → "Opus 4.8" |

### Tests

| File | Δ |
|---|---|
| `tests/agent/test_copilot_acp_approval_routing.py` | +530 |
| `tests/agent/test_copilot_acp_client.py` | +353 / −1 |
| `tests/agent/test_copilot_acp_system_prompt_mode.py` | +303 (bridge/native wire shape, exclusions, lock) |
| `tests/agent/transports/test_hermes_tools_mcp_server.py` | +258 |
| `tests/agent/test_acp_claude_alias_context.py` | +146 |
| `tests/tools/test_memory_disk_sync.py` | +128 |
| `tests/tools/test_approval_tool_allowlist.py` | +85 |
| `tests/agent/test_copilot_acp_usage.py` | +79 |
| `tests/run_agent/test_streaming.py` | +53 / −47 |
| `tests/hermes_cli/test_setup_model_provider.py` | +52 / −1 |
| `tests/agent/test_empty_tool_name_loop_dampening.py` | +17 / −2 (restores `sys.modules` — upstream bug, see verification) |
| `tests/hermes_cli/test_{api_key_providers,model_validation}.py` | +1 / −1 each (label) |
| Desktop `*.test.tsx` / `*.test.ts` | +570 across 7 files |

### Fork-only files (additive — no rebase risk)

Upstream has no file at these paths, so they can never conflict.

| File | Δ | Role |
|---|---|---|
| `tui_gateway/acp_session_modes.py` | +434 | Permission-mode and bridge/native session modes: 7 helpers, 2 `config.set` arms, injected server helpers |
| `claude-acp/claude-acp-run.js` | +126 | Windows launcher. Scrubbed-env allowlist; `ENABLE_TOOL_SEARCH=false`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` |
| `claude-acp/claude-acp-run.sh` | +102 | POSIX variant, held at parity |
| `docs/fork/*.md` | — | This knowledge base |
