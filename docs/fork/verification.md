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
  tests/agent/transports/test_hermes_tools_mcp_server.py \
  tests/tools/test_memory_disk_sync.py \
  tests/tools/test_approval_tool_allowlist.py \
  tests/agent/test_empty_tool_name_loop_dampening.py \
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
