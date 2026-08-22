# ACP wire contracts

Exact shapes the fork sends and receives. Every entry here was read off
`claude-agent-acp`'s bundled `dist/acp-agent.js` or proven with a probe against
the live agent. **Do not guess these and do not "clean them up" from intuition** —
several are counter-intuitive and cost a shipped bug each.

Line numbers are into `acp-agent.js` as of Aug 2026; re-check after an agent
upgrade.

## Native Claude options

| Option | Wire location | `acp-agent.js` |
|---|---|---|
| system prompt append | `_meta.systemPrompt = {"append": ...}` — **top-level `_meta`, object form only** | 4155–4172 |
| allowedTools | `_meta.claudeCode.options.allowedTools` | 4231 |
| disallowedTools | same path; concatenated with the agent's own list | 4278 |
| additionalDirectories | same path; concatenated with the official ACP field | 4347–4351 |
| mcpServers | `{...userProvidedOptions.mcpServers, ...params.mcpServers}` | 4248 |
| tools | `userProvidedOptions?.tools ?? <claude_code preset>` | 4198 |

**`systemPrompt` as a plain string REPLACES the `claude_code` preset** and strips
the agent's built-ins. Only the `{"append": ...}` object form is additive.

**An empty `tools` array is a present value**, so `?? <preset>` never fires. That
is exactly what makes the tool-less advisory mode possible: `tools: []` really
does mean zero tools, not "fall back to defaults".

## MCP servers merge, they do not replace

Passing `mcpServers` at `session/new` **adds to** whatever the user's own
settings (`.mcp.json`, `settings.local.json`) load. It does not suppress them.

Proven by `~/.hermes-acp/probe_mcp_merge.py`: a tripwire MCP server logs a line
on spawn (no tool call, so no approval prompt). Run A passes `mcpServers=[]` and
the `.mcp.json` server still spawns — that arm is the control, and without it
run B proves nothing. Run B passes a server over ACP and **both** spawn.

Settled. Do not re-litigate.

### stdio entries need an `env` key even when empty

The TypeScript type marks `env` optional. It is not, in practice: omit it and the
stdio server **never spawns** — silently, with no error anywhere on the wire.
Reproduced 4/4, so not a startup timing flake.

The fork always emits `"env": []` for stdio and `"headers": []` for http/sse.

```jsonc
// stdio — no "type" field at all
{"name": "x", "command": "node", "args": ["s.js"], "env": [{"name":"K","value":"v"}]}

// http / sse
{"name": "y", "type": "http", "url": "https://…", "headers": [{"name":"X-Key","value":"…"}]}
```

`env` and `headers` are **arrays of `{name, value}`**, not maps. Converting back:
`Object.fromEntries(list.map(e => [e.name, e.value]))`.

## Permission requests carry no `toolName`

A real `session/request_permission` from `claude-agent-acp` carries `kind`
(e.g. `"execute"`) and **no `toolName` on the toolCall**. `toolName` rides the
`session/update` notifications instead.

This shipped a bug once: a probe fed `_handle_server_message` a synthetic
toolCall with `_meta.claudeCode.toolName="Bash"`, the test passed, and production
prompted for every `ls`. Route shell on `kind == "execute"`, not on `toolName`.

The fork recovers the name for display by keeping a call-id → name map off the
stream: `_remember_tool_name()` (`:2298`) / `_recall_tool_name()` (`:2336`).

**A synthetic probe must match the real wire shape or it proves nothing.**
Capture a real permission RPC from `agent.log` (`ACP permission requested: … to
execute`) and shape the probe from that, not from what the display helpers emit.

### `tool_call` announces; `tool_call_update` only refines

One `sessionUpdate: "tool_call"` per call the sub-agent makes. Every later
`tool_call_update` for that same `toolCallId` is a status refinement
(`pending` → `in_progress` → `completed`) and announces nothing new. How many
refinements arrive varies per tool and per run, so anything counting both is
measuring adapter chatter, not work.

This is the only signal Hermes has for how much the sub-agent actually did:
in native mode `tool_calls` is forced empty, so none of it surfaces as an
OpenAI tool call. `_last_turn_tool_calls` tallies it (reset per
`session/prompt`, alongside `_last_turn_usage`) and
`_credit_native_tool_iterations()` spends it on the skill-review nudge — see
"Counters the loop owns" in `surfaces.md`.

## Approval routing

`session/request_permission` splits two ways in `_handle_server_message`
(`:2342`):

- **Shell** — when `shell_command` is non-empty *and* (`toolName == "Bash"` or
  `kind == "execute"`). Goes to `check_all_command_guards(cmd, env_type="local")`:
  safe reads auto-approve, the hardline floor still applies.
- **Everything else** — `smart_tool_verdict(...)` first (returns `None` unless
  `approvals.mode` is `smart`), then
  `_run_approval_gate(pattern_key=f"copilot-acp:{tool}:{sha256(target)[:12]}",
  fail_closed_when_no_human=True)`. Per-target grain, so `[a]lways` on one path
  does not bless another; only the target string is hashed, so content churn
  keeps the key stable.

**The shell branch called `check_dangerous_command` until 2026-08-21.** That is
the narrower of `approval.py`'s two public shell entry points, and its caller set
had drifted to Hermes' own `terminal` tool alone. Four guards live only in the
`check_all_command_guards` wrapper — tirith content scanning, the sudo-stdin
guard, `approvals.mode: off`, and the smart-approval aux-LLM pass — so an ACP
`Bash` call was held to a weaker standard than the identical command typed at
`terminal`, and an operator who set `approvals.mode` was ignored on this path
entirely. Both functions return the same result dict, so the swap is caller-local.

Smart approval for **non-shell** tools has no such wrapper to inherit from:
Phase 2.5 lives inside `check_all_command_guards`, which takes a command string
and cannot be handed a tool call. `smart_tool_verdict` (`tools/approval.py`) is
the tool-shaped entry point, wrapping a guardian (`_smart_approve_tool`) with its
own prompt keyed on the tool+target pair. It is deliberately **not** a reuse of
`_smart_approve`, whose prompt is entirely shell semantics ("recursive delete",
"fork bombs"); a file path judged against that grammar degrades to surface-token
matching.

Two contracts worth holding onto:

- **`None` and `"escalate"` are different answers.** `None` means the guardian
  never ran — mode is not `smart`, or no human was present to spare — and the
  caller must fall through to its normal gate. A guardian that *errors* returns
  `escalate`, never `None`, so a dead aux model degrades to asking rather than
  to silently skipping the check.
- **The guardian only runs when a human could otherwise have been prompted.**
  The shell path enforces this structurally, by placing Phase 2.5 after
  `check_all_command_guards` has already returned on the non-interactive branch;
  `smart_tool_verdict` mirrors it explicitly. The ACP tool gate passes
  `fail_closed_when_no_human=True`, so letting the guardian answer in a headless
  cron or gateway session would quietly convert a hard denial into "an LLM
  decides". Unattended policy stays where the operator set it:
  `approvals.cron_mode` / `single_query_mode`. Single-query (`-q`) exports
  `HERMES_INTERACTIVE=1` but has nobody to answer, so it is demoted first.

A smart `approve` is applied to **that call only** and is never persisted under
the pattern key — one benign write to a scratch file must not bless every later
call that hashes to the same tool. A smart `deny` is hard: unlike the shell path
there is no interactive-owner override, because the ACP handler answers a
protocol request with a single allow/deny outcome and has no channel to raise a
one-shot override card mid-request.

`_acp_shell_command()` (`:381`) reads only `command`/`cmd` — deliberately
narrower than `_raw_input_detail()` (`:194`), which also returns paths and
queries that must never reach a shell-pattern matcher.

## Model selection

`_meta.claudeCode.options` beats `session/set_config_option`.

`set_config_option` only accepts model aliases the agent advertises — `opus`,
`sonnet`, `haiku`, `claude-fable-5[1m]`. Raw ids like `claude-opus-4-8` only pass
through `_meta`. The fork sends both; `_select_acp_model()` (`:445`) skips
silently when a value is not advertised.

**Those aliases carry no vendor prefix, and that breaks context resolution.**
`get_model_context_length()` fuzzy-matches `DEFAULT_CONTEXT_LENGTHS` keys as
substrings; `opus`/`sonnet`/`haiku` contain no `claude`, so every step of the
chain missed and they landed on the 256K hard fallback while the real window is
1M — a ~4x under-report on the context gauge, and a compressor that summarizes
about three quarters of a conversation early. `claude-fable-5[1m]` and
`claude-opus-4-8` were unaffected: they match by substring.

Resolved at **step 5a0** of `get_model_context_length()`
(`agent/model_metadata.py`), ahead of the GitHub Copilot `/models` branch —
those aliases are claude-agent-acp's, not Copilot's, so that lookup can only
miss on them. Step 5 is also before step 4's `api.anthropic.com` call, which
keeps the alias path free of network I/O.

The table (`ACP_CLAUDE_ALIAS_CONTEXT`, in the fork-only
`agent/acp_alias_context.py`) is deliberately **separate** from
`DEFAULT_CONTEXT_LENGTHS`, matched exactly and only for ACP providers. A bare
`sonnet`/`haiku` key in the global dict would tie with the `claude` catch-all at
6 characters, and the longest-key-first sort breaks that tie arbitrarily — a win
for `sonnet` would promote every older Claude on *every* provider to 1M.

An alias is a moving pointer at whatever the CLI resolves it to today, so the
values are family floors and are never written to the on-disk context cache.
`haiku` maps to the 200K catch-all rather than 1M on purpose: over-reporting
lets a conversation grow past the real window and the API rejects the turn,
whereas under-reporting only compresses early.

Both directions of that separation are tested. Forward: every bare alias the
picker advertises must have a table entry. Reverse: no alias may appear as a
`DEFAULT_CONTEXT_LENGTHS` key. The reverse guard is two assertions, not one —
a derived disjointness check cannot see an alias *migrated* out of the fork
table into the fuzzy dict, since the two sets stay disjoint through exactly
that change. A frozen literal tuple of the four names covers it.

## Thinking

Set via `_meta.claudeCode.options.thinking = {"type": "adaptive", "display": "summarized"}`.

**`MAX_THINKING_TOKENS` is a trap.** `resolveThinkingConfig` maps a positive
value to `{type:"enabled", budgetTokens:N}`, which is **removed on Opus 5 and
returns 400**. The ACP wrapper's env allowlist scrubs it — leave it scrubbed.

Probing thinking with a riddle returns zero thought chunks and looks like a
broken option. Use a genuinely hard prompt or the A/B is meaningless.

## Permission mode

Negotiated at `session/new`, but sessions span turns — so a config change would
sit inert until the session happened to rebuild. `_sync_acp_mode()` (`:2012`)
re-applies a **changed** mode to a live session with `session/set_mode`.

Only a change is sent, and the applied value is recorded **even when the agent
rejects it**, so a bad mode id does not warn every turn. `self._applied_mode`
guards the no-op re-send — which is why a wire trace of set-A-then-back-to-A
shows one `session/set_mode`, not two.

Verified on the live wire: changing the mode on turn 2 emits `session/set_mode`
and the session is **not** rebuilt.

## System prompt: `append` object vs bare string

`_meta.systemPrompt` rides **top-level** `_meta`, not `_meta.claudeCode.options`,
and takes two shapes that are not variants of each other:

| Sent | `claude-agent-acp` does | Result |
|---|---|---|
| `{"append": "…"}` | locks `type`/`preset` to the `claude_code` preset, forwards the rest | Claude Code's identity, tool-schema guidance, auto `CLAUDE.md` and env context all stay; Hermes' text rides on top |
| `"…"` (plain string) | passes it through as the whole `systemPrompt` | preset **never loads**. The string IS the entire system prompt |

**The SDK accepts both and errors on neither.** There is no 400, no log line,
no validation failure. A session built with the wrong shape boots clean and then
runs an agent with no identity, no tool-call format guidance and no repo
context — visible only as degraded behaviour (prose instead of tool calls, a
generic assistant voice, no awareness of cwd or git state).

That silence is the whole reason this is a wire contract rather than a code
detail. Bridge mode must use the object form. Native mode (the Bridge pill)
uses the string form **on purpose** — replacing the preset is the feature — and
carries its own replacements for what the preset provided:

- Hermes' full system prompt from `build_system_prompt_parts` supplies identity
  and operating guidance.
- `_NATIVE_TOOL_NAME_MAPPING` (`copilot_acp_client.py`) supplies the tool
  reconciliation, because Hermes' prompt names tools off its OWN registry. Only
  the `EXPOSED_TOOLS` allowlist in
  `agent/transports/hermes_tools_mcp_server.py` crosses the MCP boundary, and
  it arrives prefixed `mcp__hermes-tools__*`; `terminal`, `read_file` and
  `write_file` do not cross at all and the agent's own `Bash`/`Read`/`Write`
  cover that ground.
- `_hermes_system_prompt_append()` is **concatenated** rather than dropped —
  native mode has no `append` channel, and losing it would take the memory/skill
  block and the operator's `system_prompt_append` with it.

There is **no RPC to resend `systemPrompt`.** Unlike permission mode (which has
`session/set_mode`) this is negotiated once at `session/new` and is fixed for the
life of that session. Any UI for it has to be a pre-session control; see the
`locked` contract in [`surfaces.md`](surfaces.md).

Pinned by `~/.hermes-acp/probe_system_prompt_mode.py` (17 checks offline, plus a
`--live` arm that opens a real session in each mode).

Proven on the live wire — same question, one session each:

```
bridge -> Claude Code, Sonnet 5 model, run in this repo terminal.
native -> Hermes Agent, Nous Research make.
```

The identity flip is the evidence: it is not reachable by an `append`, only by
actually replacing the preset. Note also that Claude Code's `SessionStart` hooks
still fire in native mode — they are wired into the CLI, not into the preset —
so hook-injected text survives a replacement that removes everything else.

## Tool suppression: MCP survives `tools: []`

Handing the agent `tools: []` removes its built-ins (Bash, Read, Edit, Write) but
**MCP tools still work**. That is what makes the background-review restriction
possible: `tools=[]` with `mcpServers` still carrying `hermes-tools`.

Proven by `~/.hermes-acp/probe_toolless_mcp.py` against a real minimal stdio MCP
server. Control arm (no tools override) called `mcp__probe__probe_record` after a
`ToolSearch`; test arm (`tools=[]`) called it **directly with no ToolSearch
available**. Both reached `tools/call`.

### Residual surface — know this before claiming "locked down"

A restricted fork still reaches everything in `EXPOSED_TOOLS`
(`agent/transports/hermes_tools_mcp_server.py:129`): `web_search`, `web_extract`,
the eight `browser_*` tools, `vision_analyze`, `image_generate`,
`text_to_speech`, plus the intended `memory` / `skill_manage` / `skill_view` /
`skills_list` / `session_search`. That tuple is hardcoded — **no env or arg
filter**. No shell, no file write, no delegate.

Tradeoff: the restricted fork also cannot `Read` repo files, so it must reason
from the inherited conversation snapshot alone.

## contextvars across threads

Approval routing (session key, gateway notify callback) lives in contextvars. A
bare `threading.Thread` starts with an **empty** context, so every permission
request resolves to session key `"default"`, finds no callback, and is denied
instantly with no card shown.

Any new worker thread must `ctx = contextvars.copy_context()` then
`ctx.run(fn, ...)`. This bit once already, in `_stream_native_turn`.
