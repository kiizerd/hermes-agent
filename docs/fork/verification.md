# Verification

How claims about this fork get proven. The rule is invariant 5 in the
[README](README.md): **verify out-of-process, never restart-and-hope.** The
Python module loads once at process start, and the desktop UI is a prebuilt
bundle — an edit is not evidence.

## Probe harnesses

They live at `~/.hermes-acp/` (outside the repo, because they hardcode machine
paths and spawn tripwire servers). Each one runs standalone with
`venv/Scripts/python.exe`.

The **launcher** that used to sit beside them is a different thing and is now
tracked at [`claude-acp/claude-acp-run.js`](../../claude-acp/claude-acp-run.js).
It carries no machine paths, so the reason the probes stay out never applied to
it.

Two families:

- **Wire probes** drive the real `claude-agent-acp` over stdio via
  `claude-acp-run.js` — the same wrapper Hermes uses — and read the raw JSON-RPC.
  They answer "what does the agent actually do".
- **Module probes** import the *edited* source fresh and drive the real
  functions with only the subprocess and RPC transport stubbed. They answer
  "what does the patched code actually send", without needing a restart.

| Probe | Kind | Answers |
|---|---|---|
| `probe_toolless.py` | wire | Does `_meta.claudeCode.options.tools=[]` disarm the agent? A/B: advisory arm vs full-preset control |
| `probe_toolless_mcp.py` | wire | Does `tools=[]` also suppress **MCP** tools? (No — this is what makes the background-review fix possible) |
| `probe_mcp_merge.py` | wire | Does `session/new` `mcpServers` merge with the user's `.mcp.json` or replace it? (Merge) |
| `probe_config_mcp.py` | wire | Do `config.yaml` `mcp_servers:` entries reach the agent, and do HTTP headers survive? Own tripwire HTTP server; control arm with `HERMES_ACP_CONFIG_MCP=off` |
| `probe_permission_join_key.py` | wire | Does the streamed `tool_call` share an id with the later `session/request_permission`, and arrive first? |
| `probe_bridge.py` | wire | Does the agent emit `<tool_call>` blocks the old bridge scraper can parse? Uses Hermes' own renderer and extractor |
| `probe_bridge_roundtrip.py` | wire | The bridge *loop*, not just first emission — re-render the whole transcript and ask again |
| `probe_models.py`, `probe_prompt.py`, `probe_setmodel.py`, `probe_switch.py` | wire | Model advertisement, prompt round-trip, `set_config_option` vs `_meta` model passthrough |
| `probe_native_options.py` | module | The full config passthrough at `session/new` — 19 checks, a negative control per key, plus proof the no-tools contract still holds |
| `probe_advisory_session.py` | module | The patched `_ensure_session` emits `tools:[]` and empty `mcpServers` for an advisory client |
| `probe_review_restriction.py` | module | Restricted / advisory / normal / unbound-client arms — 14 checks, normal arm as the negative control |
| `probe_session_mode_override.py` | module | The session-scoped mode ladder and the params `_ensure_session` builds from it |
| `probe_mode_rpc.py` | module | The real registered `config.get` / `config.set permission_mode` handlers out of `tui_gateway.server._methods` — the exact calls the pill makes |

Both mode probes address the helpers as `acpmodes.*` since those moved to
`tui_gateway/acp_session_modes.py`, but deliberately keep reading
`_acp_permission_mode_info` / `_acp_system_prompt_mode_info` off
`tui_gateway.server`. That is not an oversight: `methods_config.py` resolves
those two names from `server.py`'s globals at call time, so a probe that reads
them there doubles as a regression test for the re-export. If the import is ever
dropped from `server.py`, these probes fail instead of `config.get` failing in
production.

| `probe_approval_routing.py` | module | Replays the `session/request_permission` branch through the real `tools.approval` gates; verifies per-target keying |
| `probe_toolname_enrichment.py` | module | Replays frames captured from the live wire; does the toolName cache produce a narrow per-tool key? |
| `probe_system_prompt_append.py` | module | The memory/skill standing-instructions block: normal, operator-override, opted-out and advisory arms |
| `probe_system_prompt_mode.py` | both | Bridge vs native. 17 module checks (wire shape, native-keeps-the-append regression, advisory + restricted negative controls, lock semantics); `--live` opens a real session in each mode and asks the agent what it is |

`probe_system_prompt_mode.py` is the one probe with both kinds in one file, and
the split is the point. The module arm proves what Hermes **sends**; only
`--live` proves what the agent **does** with it. That matters more here than
elsewhere because the SDK accepts both `systemPrompt` shapes and errors on
neither — see [`wire-contracts.md`](wire-contracts.md). `--live` costs tokens,
so it is opt-in. It has been run and passes; the transcript is quoted in
`wire-contracts.md`.

Gotcha, and the reason the live arm nearly passed while testing nothing: a
standalone probe client has no bound agent, so `_build_native_system_prompt()`
returns `""` and native mode **falls back to bridge**. The live arm injects a
stub prompt to force the string form onto the wire. Separately, the live arm
must spawn `node.exe` with `claude-acp-run.js` as an argument — handing Popen
the bare `.js` raises `WinError 193`.

### Writing a new probe

- **Include a negative control.** An arm you expect to fail, that does. Without
  it "it worked" may just mean "nothing was checked". A bogus model id must
  error `model_not_found`.
- **Shape synthetic frames from a real capture**, not from what the display
  helpers emit. See [`wire-contracts.md`](wire-contracts.md) — the permission
  RPC that carries no `toolName` shipped a bug precisely because the probe was
  shaped from intuition.
- **Put workspaces under a Windows-namespace path** (`C:/tmp/...`). An MSYS
  `/tmp/...` path is invisible to Python and the probe silently finds nothing.
- A tripwire MCP server listed in `.mcp.json` must also appear in
  `.claude/settings.local.json`'s `enabledMcpjsonServers`, or it is never
  approved and the probe reads as a false "replace".

## Test set

Do not run the whole `tests/agent/` suite — 363 files with heavy fixtures (real
HTTP servers, a temp `HERMES_HOME` per test) and no completed run has ever been
observed. The set that gates this fork runs in minutes:

```bash
python -m pytest tests/acp/ \
  tests/agent/test_copilot_acp_client.py \
  tests/agent/test_copilot_acp_approval_routing.py \
  tests/agent/test_copilot_acp_usage.py \
  tests/agent/test_copilot_acp_permission_mode_state.py \
  tests/agent/test_acp_claude_alias_context.py \
  tests/agent/transports/test_hermes_tools_mcp_server.py \
  tests/scripts/test_fork_signature_drift.py \
  tests/tools/test_memory_disk_sync.py \
  tests/tools/test_approval_tool_allowlist.py \
  tests/agent/test_empty_tool_name_loop_dampening.py \
  tests/tui_gateway/test_acp_session_provider.py \
  tests/tui_gateway/test_acp_system_prompt_mode_latch.py \
  tests/hermes_cli/test_api_key_providers.py \
  tests/hermes_cli/test_model_validation.py \
  tests/hermes_cli/test_setup_model_provider.py -q

cd apps/desktop && npx vitest run && npx tsc --noEmit
```

`python`, not `python3` — there is no `python3` on this box.

**Run `scripts/run_tests.sh` *and* a naked batch `pytest`.** They measure
different things: the canonical runner isolates per file, so it structurally
cannot see cross-test `sys.modules` pollution. The same file set has been green
under `run_tests.sh` and shown 11 failures under one-process pytest.

## Signature drift — the break textual tooling cannot see

`scripts/fork/signature_drift.py`. Fork-only, additive, no upstream file at
that path.

The problem it solves: on 2026-08-16 upstream `1596148ff22` added a required
keyword-only `single_query_deny_message` to `_run_approval_gate`
(`tools/approval.py`). Our caller is in `agent/copilot_acp_client.py` — a
*different file* — so the rebase produced **zero conflict markers**, the
merge-base overlap scout saw nothing, the `merge-tree` rehearsal was clean, the
CRLF pass was clean, and the non-shell ACP approval branch shipped raising
`TypeError`. No textual check can see that class of break.

The tool walks the fork's own files, resolves every call into a first-party
helper, rebuilds that helper's signature from source at a chosen revision, and
asks CPython's own `Signature.bind` whether the call still fits.

```bash
python scripts/fork/signature_drift.py                        # is the fork broken NOW
python scripts/fork/signature_drift.py --against upstream/main  # will pulling break it
python scripts/fork/signature_drift.py --against upstream/main --show-info
```

Exit 1 on any BREAK. **Run the `--against` form before every pull** — that is
the whole point; validate mode only tells you about damage already done.

`tests/scripts/test_fork_signature_drift.py::test_the_real_fork_has_no_signature_drift_today`
runs validate mode over the real fork files, so the gating set catches drift
automatically once a rebase has landed.

### What it checks

| Shape | Verdict |
|---|---|
| upstream added a required param | BREAK — `missing a required argument` |
| upstream removed a param we pass | BREAK — `unexpected keyword argument` |
| upstream deleted the helper (present at merge base) | BREAK |
| both sides changed one signature | BREAK — needs a human |
| positional params reordered under a positional call | BREAK — *binds fine, means something else* |
| upstream added an optional param | INFO (hidden unless `--show-info`) |
| we added the helper / we changed it | silent — our hunk wins the rebase |
| call splats `**kwargs` | silent for arity, still loud for a named unknown |

### Three things that make it correct rather than plausible

**It walks the whole AST, not `tree.body`.** The real call site imports
`_run_approval_gate` *inside a function*. A module-level-only import index finds
zero calls in that file and reports it clean.

**It applies git's three-way rule, not a two-way diff.** "Does this exist at
upstream" is the wrong question; "will this bind after I rebase onto upstream"
is the right one, and a rebase carries our hunks across. Presence at the
**merge base** is what separates *upstream deleted this* (break) from *we added
this* (fine). Skipping that arm produced 10 BREAKs on the first real run, all
false — every one a fork-only symbol. The same rule at signature level silences
`CopilotACPClient(advisory=...)`, which upstream lacks because we added it.

**It reconstructs a real `inspect.Signature` and calls `.bind()`.** Positional-only
`/`, keyword-only `*`, defaults and varargs then behave exactly as the
interpreter would, instead of as a hand-rolled approximation that drifts from
CPython.

### Traps hit while building it

**`subprocess.run(text=True)` decodes with the *locale* codec.** cp1252 on this
box. `tools/approval.py` has `→` in a docstring, so `git show` raised
`UnicodeDecodeError` in the reader thread, the call reported failure, and the
resolver concluded *"the file does not exist at that revision"* — confident,
wrong output. Only the git path was affected; validate mode reads the working
tree with an explicit `encoding="utf-8"` and looked perfectly healthy
throughout. Read bytes, decode UTF-8 explicitly.

**A display label is not a cache identity.** Signatures were cached under
`SourceTree.label`, which is `rev or "working tree"` — so two trees differing
only by root collided and every diff came back empty. Caught only because the
tests compare two `tmp_path` roots, where both revs are `None`.

**Proof it works is a reproduction, not a green run.** "No breaks" is also what
a tool that checks nothing prints. The evidence is the pre-fix caller
(`git show <fix>^:agent/copilot_acp_client.py`) bound against today's
`tools/approval.py`, yielding exactly one finding:
`missing a required argument: 'single_query_deny_message'`. Synthetic fixtures
in the test file carry that shape forward; the historical SHAs are deliberately
**not** pinned in a test, because a rebase rewrites every fork SHA.

### The other invisible break: right hunks, wrong order

`tui_gateway/server.py` turn start — the one conflict pending against
`upstream/main` as of 2026-08-17. Upstream `ea4310e76c2` and our ACP mode
re-push both insert directly after `_sync_agent_model_with_config(sid, session)`
in `_run_prompt_submit`, so git raises a single conflict between two pure
insertions. Both sides are kept; the trap is which one goes first.

`_sync_bot_capabilities()` ends in `session["agent"] = new_agent` — a full
`_make_agent` rebuild — and upstream re-reads `agent = session["agent"]`
immediately after it for exactly that reason. `apply_session_acp_modes(session)`
resolves the agent through the session dict, so it must run **after** that
rebuild or a Bot Chat turn runs on a fresh `agent.client` carrying no pinned
permission or system-prompt mode. That is the hazard
`acp_session_modes.py:330` already documents for a model switch, arriving from a
second direction.

Marker order — upstream block, then ours — is the correct resolution. Keep it,
and do not reorder these by any other rule.

### Known pre-existing failures — do not chase

Confirmed identical on a clean `upstream/main` worktree:

- `test_ping_suppression` — `OSError: [WinError 6]` / `ValueError: I/O operation
  on closed file`. asyncio proactor teardown on Windows.
- `test_acp_images` — builds a WSL-style `\mnt\c\...` path on native Windows.
- Desktop vitest: ~19 failures across `electron/ssh-*`, `desktop-installation`,
  `git-worktree-ops`, `windows-hermes-path`, `stage-native-deps`,
  `markdown-text`. All Windows-path shaped, e.g. a test asserting
  `/\/[0-9a-f]{16}\.sock$/` against `\tmp\d\7d8fab2028b2913f.sock`.

Before blaming a new failure on a local change, reproduce it clean:
`git worktree add "$TEMP/hermes_base" upstream/main`.

### An upstream test polluter the fork patches

`tests/agent/test_empty_tool_name_loop_dampening.py` pops every `run_agent` /
`agent.*` / `tools.*` / `hermes_*` module out of `sys.modules` into a local
`evicted` dict and never restores it — the `finally` only cleans up
`HERMES_HOME`. Later tests in that worker re-import fresh module objects, so
`monkeypatch.setattr("agent.foo.bar", …)` patches a different object than the
code under test holds.

Symptom: a test file passes alone and fails in a batch. The fork's one-line fix
is `sys.modules.update(evicted)` in the `finally`. Upstream-bound.

## Runtime proof checklist

Reading code is not evidence. Each feature has a specific observable:

| Feature | Proof |
|---|---|
| Permission gate | `agent.log`: `ACP permission APPROVED/DENIED`, with a multi-second gap = a real human click |
| Approval routing | A probe is necessary but **not sufficient** — it only proves the branch you feed it. Real proof is a live card: `ls` runs silent, a Write card shows the actual path (not `<execute> (plugin approval rule)`), and `agent.log` shows a same-millisecond APPROVED on a per-path `[a]lways` repeat vs a fresh card on a new path |
| Native tool mode | Permission requests carry real shell text as the target; bridge mode never hits that RPC |
| Persistent session | `ACP session REUSED: sending N new message(s)` on turn 2+ |
| Thinking | A/B probe: chunks with the option, zero without, same hard prompt |
| Streaming order | First reasoning chunk index < first content chunk index |
| Permission mode switch | `session/set_mode` on the wire at turn 2 **and** no session rebuild |
| Config MCP forwarding | Tripwire server logs a spawn in the test arm, nothing in the `HERMES_ACP_CONFIG_MCP=off` control |
| ACP alias context window | `get_model_context_length("opus", base_url="acp://copilot", provider="copilot-acp")` returns 1M, not 256K. The 256K value is `DEFAULT_FALLBACK_CONTEXT`, so "is it the fallback?" is the assertion — not the literal number |
| Alias namespace guard | Mutation, not a green run — a guard that passes on unmutated code proves nothing. Insert `"sonnet"` into `DEFAULT_CONTEXT_LENGTHS` in memory: both reverse-invariant tests must fail. Then also pop `"sonnet"` from `ACP_CLAUDE_ALIAS_CONTEXT` (the migration case): the literal guard must still fail, the derived one passes — that asymmetry is why both exist |
| Tool cards | User screenshot — there is no log for render |
| Desktop fence fix / the pill | User screenshot |
| Shipped bundle | md5 of the file extracted from `app.asar` vs `dist/assets/` |

Anything ending in "user screenshot" **cannot be self-verified**. Say so rather
than claiming it works.

### Proving a desktop change actually shipped

`dist/` is not what runs — `resources/app.asar` is. Grepping the archive directly
gives a **false negative**: it is binary, so `grep -c` stops at the first NUL and
returns 0 even for strings that are present. Extract instead:

```bash
cd apps/desktop
npx asar list release/win-unpacked/resources/app.asar | grep "assets.index-"
npx asar extract-file release/win-unpacked/resources/app.asar "dist\\assets\\index-XXXX.js"
md5sum index-XXXX.js dist/assets/index-XXXX.js   # identical == the swap shipped it
```

Vite filenames are content-hashed, so a matching name already implies matching
content; the md5 is the cheap proof. Delete the extracted probe file afterwards.
