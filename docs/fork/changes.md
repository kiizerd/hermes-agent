# Change ledger

Every commit the fork carries on top of `upstream/main`, oldest first. Net diff
against the merge base: **98 files, +15,580 / −481**.

Last verified against `upstream/main` at `ac6c8028e00` (2026-08-28). When you
rebase, re-run the numbers below and re-check the `file.py:line` refs in
[surfaces.md](surfaces.md) and [wire-contracts.md](wire-contracts.md) — they are
the first thing an upstream merge invalidates.

`ref_drift.py` is not sufficient to earn this line. It gates on `### <sha>`
headings and on whether a `path:line` resolves inside its file — it says so
itself on exit. It cannot see an inline prose ref, a bare-basename ref, or a
ref that resolves cleanly onto the *wrong* function. The 2026-08-28 walk read
all 45 refs across all six docs against the source and repointed **23** of
them; only **8** had been moved by the rebase, so 15 were already lying while
`ref_drift` reported clean. Two traps worth naming: a ref's sentence often
begins on the *previous* doc line, so matching the nearest plausible symbol
produces a confidently-wrong number (`todo.md:37` names `HERMES_SESSION_ID`,
not the `PYTHONIOENCODING` entry two lines above it); and a bare basename like
`en.ts:5` can match several files, so disambiguate from the prose before
trusting it.

A **rebase** invalidates a second thing, and the numbers do not show it: it
rewrites every fork commit, so each `### <sha>` heading below names an object
that is unreachable from the new HEAD and absent from a fresh clone. Re-key them
by matching subject lines against `git log --format='%h %s' <merge-base>..HEAD`,
and make the tool verify both subjects are byte-identical before it writes —
a mis-paired heading re-keys an entry onto the wrong commit and reads as
correct forever. A merge never had this problem, which is why it first bit on
2026-08-26.

Regenerate the raw numbers with:

```bash
MB=$(git merge-base HEAD upstream/main)
git diff --numstat "$MB"..main
for c in $(git rev-list --reverse "$MB"..main); do
  echo "--- $c $(git log -1 --format=%s "$c")"
  git show --pretty="" --numstat "$c"
done
```

Computing the header total is self-referential — this file postdates the merge
base, so every line already here counts, and editing it moves the number you are
trying to report. Do all structural edits first, then compute, then write the
total by replacing text *within* an existing line so the line count cannot shift,
then recompute to confirm.

Compute it **from the index**, not the working tree:

```bash
git add docs/fork/changes.md
git diff --cached --numstat "$MB" | awk '{a+=$1;d+=$2;n++} END {print n,a,d}'
```

`git diff "$MB"` (no `--cached`) reads the working tree, which is only correct
when nothing else is in flight. On 2026-08-16 a second session was editing the
ACP files during this very update; the working-tree form read **+10,345** and
the index form **+10,200**, and the 145-line difference belonged to someone
else's uncommitted work.

## Rebase notes — 2026-09-02 (1,428 upstream commits)

The largest pull the fork has taken. Eight conflicts; five were upstream
arriving at our design independently, so the resolution SHRANK the fork rather
than re-applying it. Recorded here because a future rebase that "restores" any
of these would be undoing an upstream absorption.

- **`agent/agent_runtime_helpers.py` — upstream shipped the registration seam.**
  Our hardcoded `if agent.provider == "copilot-acp": CopilotACPClient(...)`
  branch is gone; upstream's `_provider_supplied_client()` consults the
  `ProviderProfile.create_client()` of any registered profile before the
  built-in ladder, and its comment names an out-of-tree ACP provider as the
  motivating case. Their seam does **not** bind the agent, so the fork is now
  one duck-typed line: `if hasattr(provider_client, "bind_agent")`. Keyed on
  the capability, not on the provider name, so any profile shipping a client
  that wants the parent agent gets it.

- **`agent/background_review.py` — upstream extracted our fork block.**
  `build_cache_parity_fork()` sets every attribute our inline block set
  (`_persist_disabled`, `_skip_mcp_refresh`, `_memory_store`, the
  `_cached_system_prompt` parity pin, the ACP `acp_command`/`acp_args`
  passthrough) — everything except our two markers. 277 fork lines collapsed to
  the helper call plus the INFO log line and
  `review_agent._acp_restrict_to_hermes_tools = True`.

- **`agent/auxiliary_client.py` — advisory now rides the profile seam.** Same
  absorption: upstream replaced our direct `CopilotACPClient(...)` construction
  with `_extproc_profile.create_client(**kwargs)`. `advisory=True` for
  `task == "moa_reference"` is passed through that seam so an out-of-tree
  external-process provider can honour the tool-less contract too. A profile
  whose client rejects the kwarg raises `TypeError`; we log and retry without
  it rather than losing the provider outright, because dropping the client
  entirely would break every non-ACP external-process provider.

- **`agent/copilot_acp_client.py` — upstream's model-hint fix kept, not
  reverted.** Our `_render_prompt` refactor (native mode, `include_preamble`
  for persistent sessions) is structural and survives, but upstream's
  `a94b68ad40 fix(copilot-acp): stop substituted models impersonating the
  requested one` deleted the `Hermes requested model hint: {model}` line from
  the prompt. Replaying our commit would have silently reverted that fix. No
  fork test asserted the line; upstream's reasoning was carried forward as the
  comment now sitting in `_render_prompt`. Upstream's new
  `_model_selection_request()` is kept (its tests are in the gate set) but is
  **not** on the fork's session path — `_select_acp_model()` still owns model
  selection, and unlike `_model_selection_request` it has no legacy
  `session/set_model` fallback. Wiring the two together is open work.

- **`hermes_cli/inventory.py` — the `configured` gate is deliberate; do not
  "restore" `auth_verified`.** Upstream's `_external_process_signed_in()` gates
  explicit-only pickers on
  `get_external_process_provider_status()["auth_verified"]`. That field comes
  from `_external_process_auth_evidence()`, which fingerprints **GitHub Copilot
  credentials only** (`COPILOT_ENV_VARS`, `~/.copilot/config.json`, the GH
  credential stores). An ACP endpoint pointed at Claude reports False and would
  be dropped from every explicit-only picker — the exact bug our fix closed.
  The helper's own docstring says False means "not verifiable from here", NOT
  "signed out", and that callers must never treat it as proof of absence; an
  `auth_verified` picker gate does precisely that. We gate on `configured` (a
  resolvable launch command) instead.

- **`scripts/install.sh` and `tests/agent/test_copilot_acp_client.py`** were
  additive on both sides and kept whole. One adaptation:
  `test_run_prompt_receives_picker_model` stubs `_run_prompt` with a fake that
  predates the fork's `emit` streaming callback, so the stub gained
  `emit=None`. The claim under test (model passthrough) is unchanged.

`signature_drift.py --against upstream/main` reported **20 false BREAKs** on
this pull, all `agent.anthropic_adapter.*`. Upstream split that module into
`anthropic_credentials.py` / `anthropic_endpoints.py` and re-exports every
symbol back through the adapter with `# noqa: F401`; the tool only parses `def`
statements in the named module, so a re-export reads as a deleted target.
Validate mode on the rebased tree is clean. Teaching the tool to follow
`from X import` re-export chains is open work — until then, treat an
`--against` BREAK whose reason is "target no longer exists" as unproven until
you have grepped for a re-export.

## Ledger

### `a78cd8b1c9` — Claude Code as a first-class ACP provider

The foundation. 25 files, +2,397 / −239; `copilot_acp_client.py` alone is
+1,423 / −150.

Turns the provider from prompt-scraping into a native ACP client:

- **Native tool mode.** When the target is a real agent, stop injecting a tool
  catalog into the prompt and stop parsing `<tool_call>` blocks out of the reply.
  `_resolve_tool_mode()` picks the mode; `HERMES_ACP_TOOL_MODE` forces it.
- **Persistent sessions.** One ACP session spans turns instead of one per
  completion. `_ensure_session()` builds or reuses; turn 2+ logs
  `ACP session REUSED: sending N new message(s)`.
- **Streaming.** Upstream excludes every `acp://` base URL from the streaming
  path; `agent/conversation_loop.py:3182` carves `copilot-acp` back out **by
  provider**, because this client really does yield OpenAI-style chunks.
  `_acp_stream_chunk()` shapes ACP updates into the chunk form the rest of
  Hermes expects. Not cosmetic: non-streaming suppresses `reasoning_callback`
  whenever a stream consumer is registered, so excluding this provider leaves
  the desktop Thought pane empty for every ACP turn. Pinned from the other side
  by `tests/agent/test_acp_subprocess_streaming.py` — see `b3ca9d55b4`.
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

### `b666f092df` — per-target approval keys for ACP tool calls

`copilot_acp_client.py` only, +30 / −12. The approval pattern key becomes
`copilot-acp:{tool}:{sha256(target)[:12]}`. Choosing `[a]lways` on one path no
longer blesses every other path through the same tool, and content churn does
not invalidate the key because only the target string is hashed.

### `0f382e2138` — expose `skill_manage` over the Hermes tools MCP bridge

The bridge exposed `skill_view`/`skills_list` but not `skill_manage`, so an ACP
agent could read the skill library and not maintain it.

### `4141de6baf` — test ACP tool-call approval routing

+225 lines of test driving the real `_handle_server_message` with a fake
process. Written after the routing shipped broken once (see
[`wire-contracts.md`](wire-contracts.md) — permission RPCs carry no `toolName`).

### `ce4a4e0c50` — plumb per-turn token usage from ACP prompt results

`_acp_usage_chunk()` reads the usage block off the `session/prompt` result so
per-turn token counts stop reading as zero.

### `041c08dee5` — auto-approve non-shell ACP tools via `approvals.tool_allowlist`

Adds `tools/approval.py::is_tool_allowlisted()` and `_match_tool_allowlist()`.
`command_allowlist` only ever reached shell commands; non-shell tools (`Edit`,
`Write`, `skill_manage`, …) had no auto-approval path at all and gated on exact
match. Grant-only: the list can approve, never deny.

### `ec56c0e057` — recover ACP permission `toolName` from the streamed `tool_call`

A real `session/request_permission` carries `kind`, not `toolName` — that rides
the `session/update` notifications. `_remember_tool_name()` / `_recall_tool_name()`
keep a call-id → name map from the stream so the permission card can show what
tool is actually asking.

### `f27f93d126` — build MoA reference advisors tool-less over ACP

`agent/auxiliary_client.py`, +14 / −2. `CopilotACPClient(advisory=True)` opens
the session with `tools: []` and no MCP servers, so a MoA reference advisor
holds zero tools. Also keys the client cache by task for `copilot-acp`, or an
advisory client could be handed to a `moa_aggregator` call.

### `5886cf32e1` — log the background-review fork lifecycle at INFO

`agent/background_review.py`, +41. The fork had three `logger.warning` and zero
`logger.info`, so "never fired" and "fired and wrote nothing" looked identical.
Three INFO lines now: requested / fork starting / finished with an action count.

### `1969e8f7af` — offer ACP agent models in the setup wizard, not the GitHub catalog

`_model_flow_copilot_acp` branches on `_copilot_acp_is_rerouted()`. Rerouted, it
offers `provider_model_ids("copilot-acp")` and skips `fetch_github_model_catalog`
+ `normalize_copilot_model_id` — GitHub-id mappers with no Claude counterpart.
The `/model` picker already took this route; only the wizard showed GitHub ids.

### `cc0caf4a4b` — per-session ACP permission mode, a desktop pill, and config MCP forwarding

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

### `85262f0d6c` — track the Claude ACP launcher in-repo

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

### `809279ccb5` — memory/skill standing instructions, and the Bridge pill

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

### `d310bda50e` — context window for bare Claude Code aliases

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

### `189225b6b6` — run the ACP child in the session's selected project

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

**Shipped dead — completed by `c5a428e901`.** Nothing ever passed the
`gateway_session_key` kwarg this reads, so the session lookup was skipped on
every real session and the fallback to `os.getcwd()` still ran. Tests green,
`git status` clean, and the only tell was the child's actual working directory.

### `a64fd1282e` — pass `single_query_deny_message` to the approval gate

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

Upstream has the identical omission at its own `tools/file_tools.py:1010`
(`ssh_config_write`) — untouched by the fork, so that one is upstream's to fix.

### `7bfe384075` — move ACP session modes out of `tui_gateway/server.py`

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

### `a136f2990b` — move the ACP alias context table into a fork-only module

5 files, +169 / −78 (this commit's own diff). Against the merge base,
`agent/model_metadata.py` goes from +68 to **+23**; the table lands in a new
fork-only `agent/acp_alias_context.py` (+73).

M3, and the second footprint reduction after `server.py`. `model_metadata.py`
took **105 upstream commits in 90 days**, most of them appending to
`DEFAULT_CONTEXT_LENGTHS` — the exact dict the fork's lines sat beside.

What moved: the alias table, the ACP provider frozenset, the resolver, and the
seven-line rationale comment that lived inside `DEFAULT_CONTEXT_LENGTHS`. What
stayed is +23: a four-line import, a five-line pointer where the rationale was,
and the fourteen-line step 5a0 branch. **The branch does not move, by rule** —
it modifies an upstream function body, and isolating it would mean overriding
`get_model_context_length()`, trading a conflict marker for an override that
silently shadows a future upstream fix.

Shortening the comment opened a gap worth naming, because it is the failure mode
extraction creates. Two invariants govern this table; only the forward one was
tested (every bare alias in the picker has an entry). The reverse — no alias may
be added to `DEFAULT_CONTEXT_LENGTHS` — was held *by the prose that got cut*.
That dict is substring-matched across every provider, and `sonnet`/`haiku` tie
the `claude` catch-all at six characters, so one key there promotes every older
Claude on Bedrock, Vertex and OpenRouter until the API starts rejecting turns.

The guard is **two** assertions on purpose. Derived set-disjointness has no drift
and covers aliases added later, but it is blind to a *migration*: moving an alias
out of the fork table and into the fuzzy dict leaves the sets disjoint, so that
check stays green while the bug returns. A frozen literal tuple of the four names
catches it. Both were **mutation-tested, not merely run** — a guard that passes
on unmutated code proves nothing. Injecting the naive add fails both; injecting
the migration fails only the literal one, which is the evidence that they are two
guards rather than one written twice.

A blanket "no bare keys" rule was not available: `DEFAULT_CONTEXT_LENGTHS`
legitimately carries 13 vendor-less catch-alls (`claude`, `grok`, `qwen`,
`gemini`, …), so the guard distinguishes provenance, not spelling.

No external probe reads these symbols — `grep` over `~/.hermes-acp/probe_*.py`
returns nothing for all three — so unlike M1 there was no probe to repoint.
Verified by the gating set (447 passed, 1 pre-existing `test_ping_suppression`)
and `run_tests.sh` on the three touched files (148 passed, 0 failed).

Also corrects a stale pointer: the old in-dict comment said step 5a2, the branch
is step 5a0.

**M2 was assessed and dropped, not deferred.** The i18n overlay (+72 across
`apps/desktop/src/i18n/{en,zh,types}.ts`) cannot be done by declaration merging.
`Translations` *is* an `interface`, so merging is legal, but the fork's keys are
nested inside `composer` and merging a second `composer` member is `TS2717` —
declaration merging does not deep-merge. Hoisting to a new top-level member
instead fails `TS2741`, because `en.ts:5` and `zh.ts:5` are explicitly annotated
`const … : Translations`, so any required merged key reads as a missing property.
Both errors were reproduced with `tsc`, not reasoned about. The structural point
outlives the TypeScript detail: **55 of the 72 lines are values, not types**
(`types.ts` is +17; the rest is string literals), and declaration merging is a
type-level tool with no mechanism for injecting runtime values.

### `a992fae9de` — add a post-rebase signature-drift check

4 files, +1,591 / −1. Two new fork-only files
(`scripts/fork/signature_drift.py` +882, `tests/scripts/test_fork_signature_drift.py`
+622) plus the `verification.md` section that documents them.

Closes the gap the `single_query_deny_message` break exposed on 2026-08-16
(see the entry above it in this ledger, and `verification.md`). Upstream changed
a helper's signature in `tools/approval.py`; our caller lives in
`agent/copilot_acp_client.py`. Different file, so the rebase was clean, the
overlap scout saw nothing, the `merge-tree` rehearsal was clean, the CRLF pass
was clean, and the branch shipped raising `TypeError`. **No textual check can
see that class of break** — it is a property of the call, not of the text.

The tool walks the fork's own files, resolves every call into a first-party
helper, rebuilds that helper's signature from source at a chosen revision, and
asks CPython's own `Signature.bind` whether the call still fits. Validate mode
(`python scripts/fork/signature_drift.py`) answers "is the fork broken now";
`--against upstream/main` answers "will pulling break it", which is the form to
run **before** a pull.

Three decisions carry it:

- **It walks the whole AST, not `tree.body`.** The real call site imports
  `_run_approval_gate` inside a function. A module-level-only import index finds
  zero calls in that file and reports it clean — the tool would have missed the
  very break it was written for.
- **It applies git's three-way merge rule, not a two-way diff.** "Does this
  exist upstream" is the wrong question; "will this bind after I rebase onto
  upstream" is the right one, and a rebase carries our hunks across. Presence at
  the **merge base** is what separates *upstream deleted this* from *we added
  this*. Without that arm the first real run against `upstream/main` produced 10
  BREAKs, every one a fork-only symbol. The same rule at signature level
  silences `CopilotACPClient(advisory=…)`, absent upstream only because we added
  it. When both sides changed one signature, it says so rather than picking.
- **It reconstructs a real `inspect.Signature` and calls `.bind()`.**
  Positional-only `/`, keyword-only `*`, defaults and varargs then behave
  exactly as the interpreter does, instead of as a hand-rolled approximation
  that drifts from CPython.

It also catches a reorder of parameters passed positionally — which binds
cleanly and quietly means something else, the one drift `bind()` alone is blind
to.

Evidence is a reproduction, not a green run: "no breaks" is also what a tool
that checks nothing prints. The pre-fix caller
(`git show <fix>^:agent/copilot_acp_client.py`) bound against today's
`tools/approval.py` yields exactly one finding, `missing a required argument:
'single_query_deny_message'`. Test fixtures are synthetic on purpose — a rebase
rewrites every fork SHA, so a test pinned to the real commits rots the way this
ledger's own SHAs did.

Two bugs the tests caught, both worth recording because both were invisible in
the mode being exercised. `subprocess.run(text=True)` decodes with the **locale**
codec (cp1252 here); `tools/approval.py` has `→` in a docstring, so `git show`
raised `UnicodeDecodeError` in the reader thread and the resolver reported the
file as *nonexistent at that revision* — only on the git path, while validate
mode read the working tree with an explicit encoding and looked healthy. And
signatures were cached under `SourceTree.label` (`rev or "working tree"`), so
two trees differing only by root collided and every diff came back empty; a
display string is not a cache identity.

Current state: `--against upstream/main` is clean across 1,511 first-party call
sites in 22 fork files, so the 12 unpulled upstream commits break no call site.

### `af1e35e6a8` — route native ACP approvals through the full guard stack

7 files, +936 / −32. Two unrelated gaps that share a root: native mode routes
around the machinery Hermes normally runs, and both times the bypass was silent.

**The shell branch called the wrong guard.** `session/request_permission` went to
`check_dangerous_command`, the narrower of `approval.py`'s two public shell entry
points, whose caller set had drifted to Hermes' own `terminal` tool alone. Four
guards live only in the `check_all_command_guards` wrapper — tirith content
scanning, the sudo-stdin guard, `approvals.mode: off`, and the smart-approval
aux-LLM pass. An ACP `Bash` call was therefore held to a weaker standard than the
identical command typed at `terminal`, and an operator who set `approvals.mode`
was ignored on this path entirely. Both functions return the same result dict, so
the swap is caller-local — one line, no branch change below it.

**Non-shell tools had no wrapper to inherit from.** Phase 2.5 lives *inside*
`check_all_command_guards`, which takes a command string and cannot be handed a
tool call. `smart_tool_verdict` is the tool-shaped entry point, wrapping a
guardian (`_smart_approve_tool`) with its own prompt keyed on the tool+target
pair. Deliberately not a reuse of `_smart_approve`, whose prompt is entirely
shell semantics ("recursive delete", "fork bombs") — a file path judged against
that grammar has no grammar to reason with and degrades to surface-token
matching.

Three contracts hold that gate together:

- **`None` and `escalate` are different answers.** `None` means the guardian
  never ran and the caller must fall through to its normal gate. A guardian that
  *errors* returns `escalate`, never `None`, so a dead aux model degrades to
  asking rather than to silently skipping the check.
- **It only runs when a human could otherwise have been prompted.** The shell
  path enforces this structurally, by placing Phase 2.5 after the
  non-interactive branch has already returned; the tool path mirrors it
  explicitly. The ACP gate passes `fail_closed_when_no_human=True`, so answering
  in a headless cron or gateway session would quietly convert a hard denial into
  "an LLM decides". Unattended policy stays where the operator set it.
  Single-query (`-q`) exports `HERMES_INTERACTIVE=1` but has nobody to answer, so
  it is demoted first.
- **A smart approve is not persisted.** It applies to that call only, never under
  the pattern key — one benign write to a scratch file must not bless every later
  call hashing to the same tool.

**Native tool work now reaches the skill nudge.** `tool_calls` is forced empty
for the mode, so the conversation loop exits after a single pass however much the
sub-agent did behind the wire: a turn where Claude ran twenty tools ticked
`_iters_since_skill` exactly once, so the nudge needed ~10 *user turns* instead
of ~10 tool iterations. Memory review is turn-counted rather than
iteration-counted and kept working throughout — that asymmetry is why this read
as "ACP never updates skills" rather than as a counter problem. Tally the
`tool_call` notifications and supply the difference, the same compensation
`codex_runtime.py:895` applies for the codex app-server path.

The credit copies **both** conditions the loop puts on its own increment, not
just the interval. An agent without `skill_manage` in `valid_tool_names` is never
counted by the loop, so crediting it would under-count by one *and* stack a tally
onto an agent that can never act on the nudge. Codex needs no such guard at its
credit site because it bypasses the loop entirely and applies the test at the
nudge check instead — same invariant, two correct placements, and copying codex
verbatim here would have been wrong.

Verified against a pristine worktree rather than assumed: the naked-batch gate
set is 494 passed / 4 failed, and all four reproduce identically on untouched
HEAD (`test_ping_suppression` asyncio teardown, three `symlink_to` calls needing
a Windows privilege this box does not hold). Both are now recorded in
`verification.md` so the next run does not chase them.

### `e6cd95c5cc` — make the heavy CI lanes resolve on a fork

6 files, +15 / −15 (one `runs-on` and one `timeout-minutes` per lane, plus the
Python worker count).

Upstream `10f99bc15e` (2026-08-22) moved every heavy lane onto **GitHub larger
runners**. Those labels are provisioned per-org: `NousResearch` has them, a
personal fork does not. GitHub does not fall back to a standard runner — an
unresolvable label queues until the 24h ceiling, so every lane read as a hang,
not an error:

```
The job has exceeded the maximum execution time while awaiting a runner for 24h0m0s
```

The fix keeps upstream's label and adds the fork's, selected by repository:

```yaml
runs-on: ${{ github.repository == 'NousResearch/hermes-agent' && 'ubuntu-latest-96-core' || 'ubuntu-latest' }}
```

| File | Lane | Upstream runner | Fork runner |
|---|---|---|---|
| `tests.yml:24` | Python tests | `ubuntu-latest-96-core` | `ubuntu-latest` |
| `js-tests.yml:17` | JS & TS checks | `ubuntu-latest-32-core` | `ubuntu-latest` |
| `rust-tests.yml:33` | Rust tests | `ubuntu-latest-32-core` | `ubuntu-latest` |
| `tests-os.yml:51` | Windows-only tests | `windows-latest-32-core` | `windows-latest` |
| `nix.yml:57` | nix flake check | `ubuntu-latest-32-core` | `ubuntu-latest` |
| `e2e-desktop.yml:23` | Desktop E2E | `ubuntu-latest-32-core` | `ubuntu-latest` |

Two values move with the label, both in `tests.yml`. Neither is cosmetic:

- **`HERMES_TEST_WORKERS` (`:122`), 96 → 4.** Upstream's comment block measures
  one worker per core as the win. 96 subprocesses on a 4-core / 16 GB runner is
  not a slower version of that — it is an OOM.
- **`timeout-minutes` (`:25`), 30 → 90.** Upstream measured 11,645s of test
  work in series. 96 cores land it inside 30 minutes; 4 cores do not.

Other lanes get a timeout raise on the same reasoning: js/rust/os 30 → 60, nix
60 → 120, desktop E2E 20 → 45.

`docker.yml` needs no edit. Its three jobs are already gated
`if: github.repository == 'NousResearch/hermes-agent'` (`:73`, `:185`, `:266`),
so its four `-32-core` / `-32-arm-core` labels are never requested here.

**Rebase note.** These are one-line edits inside files upstream actively
maintains, so each is a standing conflict. The ternary form is deliberate: it
preserves upstream's value verbatim on the left, which makes the resolution
"keep the fork's line" obvious in a conflict hunk instead of looking like the
fork deleted upstream's runner. It is also upstreamable as-is — it fixes CI for
every fork, not just this one. If upstream takes it, this entry retires.

### `b3ca9d55b4` — pin that a subprocess ACP turn still streams

Test-only. 1 file, +110.

Upstream's `613164dadc fix(acp): key the ACP runtime exclusions on the scheme,
not on one vendor` widened the streaming exclusion in
`agent/conversation_loop.py` from this one provider to every `acp://` base URL,
and shipped `test_an_acp_provider_turn_never_asks_for_a_stream`
(`tests/agent/test_acp_provider_rails.py:80`) to hold it there. That is a
head-on collision with `a78cd8b1c9`, which exists in part to make this provider
stream.

Resolved by keying the carve-out on the **provider** rather than the scheme
(`conversation_loop.py:3182`). Upstream's test passes unchanged — its fake
client reports `provider=unknown` — and the split matches how the client is
actually routed: `create_openai_client` sends only the `copilot-acp` provider
and `acp://copilot` base URLs to `CopilotACPClient`
(`agent/agent_runtime_helpers.py:2721`), so a generic `acp://` vendor never
reaches a client that could stream anyway. Upstream's scheme-wide rule stays
correct for every case it was written for.

`test_a_copilot_acp_turn_asks_for_a_stream` pins the other direction, which
nothing did before: upstream forbids `acp://` streaming, no test asserted
`copilot-acp` still streams, so a future widening would blank the Thought pane
with a green suite. Mutation-verified — restoring the scheme-only condition
fails it.

**Rebase note.** Fork-only file, not an edit to upstream's
`test_acp_provider_rails.py`, so the test itself carries zero conflict surface.
The `conversation_loop.py` condition is the standing conflict; this test is what
tells you the resolution got lost.

### `e489bb1eee` — keep configured `external_process` providers in explicit-only pickers

2 files, +130. `hermes_cli/inventory.py` is +26 of it.

Every Claude model vanished from the desktop model selectors. Not the ACP
client and not a stale build — a filter.

The picker always sends `explicit_only: true`
(`apps/desktop/src/lib/model-options.ts:155`), which runs
`_filter_explicit_provider_rows` (`hermes_cli/inventory.py:717`). The provider
clears the credential check and dies on the gate after it:
`is_provider_explicitly_configured()` reads auth.json `active_provider`,
config.yaml `model.provider`, MoA slots, and API-key env vars.
`external_process` providers touch none of them — `copilot-acp` declares
`api_key_env_vars = ()` and is configured by pointing Hermes at a launch
command, which that function never consults.

Only the `slug == current_slug` hatch kept the row alive, which makes the picker
chicken-and-egg: it offers Claude solely when you are already on Claude. Point
`model.provider` anywhere else and every Claude model disappears.

The new hatch (`inventory.py:755`) sits directly beside the anthropic-OAuth one,
which exists for the identical shape — a deliberate user setup that leaves no
trace the strict gate can see. Reachability is answered by
`_external_process_provider_configured()` (`:770`) delegating to
`get_external_process_provider_status()` (`hermes_cli/auth.py:7263`), which
self-gates on `auth_type` and resolves the launch command with `shutil.which`.
Reusing that rather than hardcoding one vendor means a second `external_process`
provider inherits the fix instead of re-reporting the bug.

Not a blanket opt-out: the filter still cuts 12 rows to 6, and an
`external_process` provider whose command does not resolve is still dropped
(`test_an_unconfigured_external_process_provider_is_still_dropped`).

**Rebase note.** `inventory.py` was untouched by all 485 upstream commits in the
2026-08-26 window, so this applied clean. Upstreamable as-is — the gap is
upstream's own auth-type blind spot, not a fork artifact.

### `aba81a2d21` — add a post-rebase reference-drift check

2 files, +391. Fork-only, additive.

`docs/fork/*.md` carries two kinds of pointer a rebase breaks without touching
a character of the docs: `<file>:<line>` refs into upstream code, and
`### <sha>` ledger headings. The 2026-08-26 rebase broke both at once —
`_acp_config` slid 506 → 726, `_config_mcp_servers` 1799 → 2155,
`_requested_acp_mode` 820 → 969, and all 22 fork SHAs became unreachable from
the new HEAD. Every one of those line refs still *resolved*, because the files
are thousands of lines long, so no diff, test run, or lint pass could see it.

Three checks gate, each a fact rather than a guess: a ref names a file that is
missing; a ref names a line past the end of that file; a `### <sha>` names a
commit unreachable from HEAD or no commit at all. The range check is what
caught three live bare `:NNNN` continuation refs in `todo.md` that had
inherited the wrong filename — invisible to a reader who already knew which
file was meant.

`--anchors` adds a heuristic pass over the symbols each paragraph names in
backticks. Off by default: on the rebase that motivated the tool it produced
27 false positives against 3 real finds, because a paragraph routinely names
the Hermes-side function while the ref points at its codex-side counterpart.
A gate that cries wolf gets ignored.

**A clean run is not verification** and the tool says so on exit. It proves no
ref is broken, not that a ref still points at the right function. Advancing the
`Last verified` line above still means reading them.

### `2bcf018654` — bridge the active memory provider's tools into native sessions

3 files, +533 / −47. Closes the `todo.md` entry opened the same day.

A memory provider declares its tools on the `MemoryProvider` ABC
(`agent/memory_provider.py:232`) and the live agent routes them through a
`MemoryManager` built at init (`agent/agent_init.py:1905`). None of that touches
`tools/registry.py`, so the tools never enter `get_tool_definitions()` —
measured on this box: 44 definitions, zero `hindsight_*`.

That also corrects the fix sketched in the todo entry. Folding provider tool
*names* into `EXPOSED_TOOLS` could not have worked: the registration loop looks
each name up in the `get_tool_definitions()` dict and `continue`s on a miss, so
the names would have been silently skipped. The bridge has to supply the schemas
too, not just the names.

What hid it is the asymmetry between a provider's automatic and explicit halves.
`prefetch()` (`plugins/memory/hindsight/__init__.py:1935`) and `sync_turn()`
(`:2080`) run server-side in the parent Hermes process, so recall arrives
pre-injected and every turn is still ingested. Only the explicit tools were
missing — and only for native ACP/codex sessions, which are precisely the
sessions whose system-prompt block says *"use hindsight_recall to search,
hindsight_retain to store facts"*. Live instruction, absent tools.

`_memory_provider_bridge()` rebuilds the same two pieces `agent_init` builds —
`load_memory_provider()` plus a `MemoryManager` — rather than calling the
provider directly, so the reserved core-tool-name rule and the schema
normalisation in `MemoryManager.add_provider()` apply identically.

Three things the implementation cannot skip:

- **`initialize()` is mandatory.** Provider tool handlers read state only it
  assigns — hindsight resolves its `bank_id` template there
  (`plugins/memory/hindsight/__init__.py:1673`) and sets `_observation_scopes`
  (`:1701`) and `_recall_tags` (`:1705`). An uninitialised provider looks wired
  and raises `AttributeError` on the first retain.
- **Identity has to match the parent.** `HERMES_SESSION_ID` / `HERMES_HOME` /
  `HERMES_PROFILE` already crossed into the subprocess, so session tags and the
  profile `agent_identity` derives from resolve to the parent's values.
  `HERMES_PLATFORM` is added to that forward list because `bank_id_template` can
  interpolate `{platform}` — a mismatch would write to a *different bank*.
  Absent, the bridge takes `agent_init`'s own `"cli"` default.
- **Order matters.** Provider tools register after the curated list because
  `MCPServer.add_tool` accepts a duplicate name silently, last writer wins
  (probed directly). An unguarded collision would *replace* the Hermes tool
  rather than raise. `MemoryManager.add_provider()` already rejects provider
  tools named after a `_HERMES_CORE_TOOLS` entry, and every current
  `EXPOSED_TOOLS` name is one, so today that layer catches it first; the
  bridge's own check covers what it cannot — an exposed tool that is not a core
  tool.

Fails soft: no provider configured, provider unavailable, or load raising all
yield `(None, [])` and the rest of the surface still comes up. Shutdown drains
the provider so retains queued on its daemon writer thread get a chance to land.

Verified live against the configured hindsight provider — 3 tools on a real
`MCPServer`, `hindsight_recall` returning stored facts, `hindsight_retain`
storing, an unknown tool routed to a clean error. Disabling the bridge takes the
server 13 → 10 tools, exactly the 3. All four guards in
`tests/agent/transports/test_memory_provider_mcp_bridge.py` are mutation-checked:
dropping the registration block, skipping `initialize()`, removing the shadow
guard, and dropping the shutdown drain each turn the suite red.

### `5d516c258a` — deflake the measure-at-unmount offset cache test

1 file, +6 / −4. Upstream-owned test; upstreamable as-is.

`ui-tui/src/__tests__/virtualHistoryOffsetCache.test.ts` "corrects and
compensates a same-layout row measured at unmount" failed twice on fork CI
(2026-08-26, `9cb4997` and `1a30582`) with `adjustScrollTop` at 0 calls
instead of 1, passing on the run between — a scheduler flake, not a
regression. The test raced blind `delay(20)`/`delay(40)` waits against React
commits: measure-at-unmount only fires when the reconciler calls `ref(null)`
on a row that actually mounted, and on a loaded runner the `scrollTo(0)`
commit that mounts item-0 can land after the fixed 20 ms wait. The
`scrollTo(5)` that follows shares snapshot bin 0 (`QUANTUM = 10`), so no
second commit ever rescues the mount — the rerender then unmounts nothing and
the spy stays at zero.

Fix per upstream's own flake policy (event-based sync, no fixed timing):
three `vi.waitFor` polls on facts — scroll handle present, item-0 mounted
(`virtualHistory.start === 0`), spy fired — replacing the fixed delays.
10/10 local stress runs green.

### `7ceb679e9e` — on-disk known-good config generations

2 files, +777 / −20. The fork's first commit in `hermes_cli/config.py`.

Upstream already keeps a last-known-good config, but only in a module global
(`_LAST_EXPANDED_CONFIG_BY_PATH`). That covers a running process whose user
mid-edits `config.yaml` into broken YAML; it dies with the process. On
2026-08-27 a `config.yaml` truncated during a desktop build swap loaded as bare
`DEFAULT_CONFIG` in the next process, and the in-memory tier had gone with the
old one.

Adds a disk tier behind the same restore path. `_snapshot_known_good` rotates
three byte copies (`config.yaml.lkg.1..3`) after any parse that provably
succeeded; `_load_known_good` walks them newest-first when the live file fails
to parse. A generation that no longer parses is skipped, never deleted.

Three details carry the correctness:

- **A restored generation re-enters the normal pipeline** instead of
  short-circuiting it. `user_config` and the `_deep_merge` are hoisted out of
  the `try:` block so restored and live loads walk the same merge → normalize →
  expand → managed-overlay chain. Short-circuiting would pass an "is my value
  there?" test while silently dropping the `max_turns` migration and `${VAR}`
  expansion.
- **A restored config is never snapshotted back**, guarded by `parsed_live_file`
  — the flag exists only because that hoist made the success path reachable from
  the failure path. Without it every load against a still-broken file rotates
  the restored copy into slot 1 and walks real history off the 3-slot ring.
- **A missing `config.yaml` must not register in the in-process tier.** Upstream
  wrote that dict on every successful load, including loads where the file did
  not exist and the "loaded config" is just `DEFAULT_CONFIG`; the read side only
  checks presence, so that defaults snapshot outranked the disk tier.

A restore is never silent: `_warn_config_parse_failure` gained a
`known-good-file` wording that names the generation and its save time on
stderr. `describe_known_good()` lists what is on disk, and `hermes config edit`
validates on editor exit so a broken save is caught then rather than at the
next process start.

Shaped for rebases: helpers are one contiguous block near the top of the file,
and the loader changes are three small edits at the tail of `_load_config_impl`
rather than a restructure. Surfaces and invariants in
[surfaces.md](surfaces.md); 24 tests in
`tests/hermes_cli/test_config_known_good.py`, all gated on a live
`_snapshot_known_good` body so a rebase that reverts the helper block turns the
suite red instead of quietly skipping. 154 further tests green across the
existing config suites cover the `_load_config_impl` restructure.

### `896c9f5c7c` — forward `SYSTEMDRIVE` to the hermetic test environment

1 file, +10 / −5. Upstream-owned, upstreamable, and the fork's first commit in
`scripts/run_tests.sh`.

The runner drops the environment with `env -i` and forwards an explicit
allowlist of Windows location variables, on the stated rationale that keeping
the list short is what keeps the "no credential can leak" property auditable at
a glance. `SYSTEMDRIVE` was missing from it.

Windows stores the ProgramData path in the registry as a `REG_EXPAND_SZ` of the
literal `%SystemDrive%\ProgramData`. With `SYSTEMDRIVE` unset,
`ExpandEnvironmentStringsW` leaves it unexpanded, and the resulting drive-less
string resolves *relative to the current directory* — so a test run silently
creates a `%SystemDrive%/ProgramData/Microsoft/Windows/Caches/` tree in the repo
root. It reads like a stray artifact from a misbehaving test; nothing in the
suite is doing it, the shell is.

Verified as an A/B against the two environments rather than asserted:

```
without SYSTEMDRIVE: CSIDL_COMMON_APPDATA -> '%SystemDrive%\ProgramData'
with    SYSTEMDRIVE: CSIDL_COMMON_APPDATA -> 'C:\ProgramData'
```

Narrow on purpose. `PROGRAMDATA`, `PUBLIC`, `PATHEXT` and `COMSPEC` are the
obvious next candidates and were left out — each is speculative until a run
actually fails without it, and the file's whole design is that the list stays
short enough to read. Same class as the gap the allowlist comment already cites
(#67385, #70813); the list was incomplete, not wrong. `SYSTEMDRIVE` is a
location variable, not a credential, and is forwarded only when set, so POSIX
runs are byte-for-byte unchanged.

### `9e687da049` — treat an empty `HERMES_COPILOT_ACP_ARGS` as "no arguments"

3 files, +47 / −3. Upstream-owned, upstreamable.

`_resolve_args()` (`agent/copilot_acp_client.py:84`) read the variable as
`os.getenv(name, "").strip()` and returned Copilot's `["--acp", "--stdio"]` for
any falsy result. That collapses *unset* (no preference — Copilot's flags are
the right default) with *set-but-empty* (an explicit "pass no arguments"). The
second was unexpressible.

Set-but-empty is what `claude-agent-acp` needs on POSIX, where the launcher
script is itself the command rather than an argument to a node binary. Windows
never hit this: its `.env` sets `ARGS` to the `.js` path, which is non-empty and
contains no `--acp`, so `_acp_supported()` short-circuits `True` at line 123 and
never probes.

The failure mode is not a clean "unknown option". `_acp_supported()` sees
`--acp` among the args it is about to pass, probes the command with `--help`,
and `claude-acp-run.sh --help` exits **0 with empty stdout**. `rc == 0` reads as
a trustworthy answer and the absent `--acp` reads as definitively unsupported,
so the probe returns `False` and the spawn hard-fails before a child exists. A
*crashing* probe would have returned `None` and fallen through to the real spawn
path — clean success with no output is what makes it fatal.

Found on paladin, where both profiles carried the latent bug; only steward
exercised it, because the default profile's cron jobs are `no-agent` and never
call a model. Three regression tests pin unset, set-but-empty, and that the
empty case never reaches the probe.

Unset still yields `["--acp", "--stdio"]`, so nothing relying on the default
changes.

## File map

Where the fork touches upstream code, and what to check after a rebase.

### Python — core

| File | Δ | Role |
|---|---|---|
| `agent/copilot_acp_client.py` | +2808 / −155 | The fork. Native tool mode, sessions, streaming, permission gate, thinking, modes, MCP wiring (incl. `HERMES_PLATFORM` forward for memory-bank identity), project cwd, native tool-iteration credit |
| `agent/transports/hermes_tools_mcp_server.py` | +345 / −51 | `memory`, `session_search`, `skill_manage` added to the exposed tool surface; `_memory_provider_bridge()` registers the active memory provider's own tools, which `EXPOSED_TOOLS` cannot express |
| `agent/auxiliary_client.py` | +15 / −3 | Advisory (tool-less) client for `moa_reference`; task-keyed client cache |
| `agent/model_metadata.py` | +23 | Import + step 5a0 branch delegating to `agent/acp_alias_context.py` |
| `agent/background_review.py` | +40 | INFO lifecycle logging; stamps `_acp_restrict_to_hermes_tools` |
| `agent/agent_runtime_helpers.py` | +4 | `client.bind_agent(agent)` |
| `agent/conversation_loop.py` | +23 / −10 | Carves `copilot-acp` back out of upstream's scheme-wide streaming exclusion |
| `agent/display.py` | +20 / −2 | `build_tool_preview` fallback keys |
| `agent/turn_context.py` | +12 | MemoryStore staleness reload |
| `tools/memory_tool.py` | +71 | Cross-process MemoryStore sync |
| `tools/approval.py` | +259 / −18 | `approvals.tool_allowlist` for non-shell tools; `smart_tool_verdict` + `_smart_approve_tool`, the tool-shaped entry to smart approval |

### Python — CLI, config, gateway

| File | Δ | Role |
|---|---|---|
| `hermes_cli/config.py` | +325 / −20 | On-disk known-good generations: `_lkg_path`, `_snapshot_known_good`, `_load_known_good`, `describe_known_good`, the `known-good-file` warn arm, three edits at the tail of `_load_config_impl`, and `config edit` validation |
| `hermes_cli/config_defaults.py` | +46 | The `copilot_acp:` config block |
| `hermes_cli/model_setup_flows.py` | +55 / −33 | Wizard offers agent models when rerouted |
| `hermes_cli/model_switch.py` | +22 | `/model` picker routing |
| `hermes_cli/models.py` | +48 / −2 | `_copilot_acp_is_rerouted()`, Opus 4.8 entry |
| `hermes_cli/inventory.py` | +26 | `external_process` hatch in `_filter_explicit_provider_rows` |
| `hermes_cli/providers.py`, `auth.py` | +1 / −1 each | Provider label `Claude Sub ACP` |
| `plugins/model-providers/copilot-acp/__init__.py` | +2 / −2 | Plugin metadata |
| `tui_gateway/server.py` | +58 | Call sites only — import (re-exports the two `_info` fns for `methods_config.py`), `session.info` unpack, turn-start apply, `config.set` delegation |
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
| `tests/scripts/test_fork_signature_drift.py` | +622 (fork-only; synthetic fixtures, plus a live guard over the real fork) |
| `tests/scripts/test_fork_ref_drift.py` | +145 (fork-only; synthetic fixtures, plus a live guard over the real docs) |
| `tests/hermes_cli/test_config_known_good.py` | +452 (fork-only; every write-half test gated on a live `_snapshot_known_good` body) |
| `tests/agent/test_copilot_acp_approval_routing.py` | +895 |
| `tests/agent/test_copilot_acp_skill_iterations.py` | +245 (fork-only; native tool-iteration credit) |
| `tests/agent/test_copilot_acp_client.py` | +353 / −1 |
| `tests/agent/test_copilot_acp_system_prompt_mode.py` | +303 (bridge/native wire shape, exclusions, lock) |
| `tests/agent/transports/test_hermes_tools_mcp_server.py` | +258 |
| `tests/agent/test_copilot_acp_edit_preview.py` | +236 (inline diff previews for native ACP edits) |
| `tests/agent/test_acp_claude_alias_context.py` | +200 |
| `tests/tui_gateway/test_acp_system_prompt_mode_latch.py` | +192 |
| `tests/tools/test_memory_disk_sync.py` | +128 |
| `tests/agent/test_copilot_acp_permission_mode_state.py` | +113 |
| `tests/agent/test_acp_subprocess_streaming.py` | +110 (fork-only; pins that copilot-acp still streams) |
| `tests/hermes_cli/test_inventory_external_process.py` | +104 (fork-only; the explicit-only picker hatch) |
| `tests/agent/transports/test_memory_provider_mcp_bridge.py` | +293 (fork-only; memory-provider tools over the MCP bridge, 4 mutation-checked guards) |
| `tests/tui_gateway/test_acp_session_provider.py` | +63 |
| `tests/tools/test_approval_tool_allowlist.py` | +85 |
| `tests/agent/test_copilot_acp_usage.py` | +79 |
| `tests/run_agent/test_streaming.py` | +53 / −47 |
| `tests/hermes_cli/test_setup_model_provider.py` | +52 / −1 |
| `tests/agent/test_empty_tool_name_loop_dampening.py` | +17 / −2 (restores `sys.modules` — upstream bug, see verification) |
| `tests/hermes_cli/test_{api_key_providers,model_validation}.py` | +1 / −1 each (label) |
| Desktop `*.test.tsx` / `*.test.ts` | +590 / −1 across 8 files |
| `ui-tui/src/__tests__/virtualHistoryOffsetCache.test.ts` | +6 / −4 (deflake; upstream-owned, upstreamable) |
| `scripts/run_tests.sh` | +10 / −5 (`SYSTEMDRIVE` in the `env -i` allowlist; upstream-owned, upstreamable) |

### CI (one line each — upstream owns these files)

| File | Δ | Role |
|---|---|---|
| `.github/workflows/tests.yml` | +3 / −3 | Runner, timeout, `HERMES_TEST_WORKERS` |
| `.github/workflows/js-tests.yml` | +2 / −2 | Runner, timeout |
| `.github/workflows/rust-tests.yml` | +2 / −2 | Runner, timeout |
| `.github/workflows/tests-os.yml` | +2 / −2 | Windows matrix runner, timeout |
| `.github/workflows/nix.yml` | +2 / −2 | Runner, timeout |
| `.github/workflows/e2e-desktop.yml` | +2 / −2 | Runner, timeout |

### Fork-only files (additive — no rebase risk)

Upstream has no file at these paths, so they can never conflict.

| File | Δ | Role |
|---|---|---|
| `scripts/fork/signature_drift.py` | +882 | Post-rebase signature-drift check: AST call-site index, three-way signature resolution, `Signature.bind` verdict |
| `scripts/fork/ref_drift.py` | +246 | Post-rebase reference-drift check: `file.py:line` refs and ledger SHAs in `docs/fork/*.md` |
| `tui_gateway/acp_session_modes.py` | +528 | Permission-mode and bridge/native session modes: 7 helpers, 2 `config.set` arms, injected server helpers |
| `agent/acp_alias_context.py` | +73 | Context windows for bare Claude Code aliases: table, ACP provider set, exact-match resolver |
| `claude-acp/claude-acp-run.js` | +126 | Windows launcher. Scrubbed-env allowlist; `ENABLE_TOOL_SEARCH=false`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` |
| `claude-acp/claude-acp-run.sh` | +102 | POSIX variant, held at parity |
| `docs/fork/*.md` | — | This knowledge base |
