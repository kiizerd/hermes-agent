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

## Approval routing

`session/request_permission` splits two ways in `_handle_server_message`
(`:2342`):

- **Shell** — when `shell_command` is non-empty *and* (`toolName == "Bash"` or
  `kind == "execute"`). Goes to `check_dangerous_command(cmd, env_type="local")`:
  safe reads auto-approve, the hardline floor still applies.
- **Everything else** — `_run_approval_gate(pattern_key=f"copilot-acp:{tool}:{sha256(target)[:12]}",
  fail_closed_when_no_human=True)`. Per-target grain, so `[a]lways` on one path
  does not bless another; only the target string is hashed, so content churn
  keeps the key stable.

`_acp_shell_command()` (`:381`) reads only `command`/`cmd` — deliberately
narrower than `_raw_input_detail()` (`:194`), which also returns paths and
queries that must never reach a shell-pattern matcher.

## Model selection

`_meta.claudeCode.options` beats `session/set_config_option`.

`set_config_option` only accepts model aliases the agent advertises — `opus`,
`sonnet`, `haiku`, `claude-fable-5[1m]`. Raw ids like `claude-opus-4-8` only pass
through `_meta`. The fork sends both; `_select_acp_model()` (`:445`) skips
silently when a value is not advertised.

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
