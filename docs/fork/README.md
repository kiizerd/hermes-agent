# The Claude-over-ACP fork

This directory is the knowledge base for everything `kiizerd/hermes-agent` carries
on top of `NousResearch/hermes-agent`. It lives in the repo so it rebases with the
code and can be read by anyone (or any agent) who opens the checkout cold.

## What the fork is for

Hermes ships a `copilot-acp` provider that speaks ACP to a subprocess agent. On
this machine that subprocess is rerouted to `claude-agent-acp` — Claude Code
itself — via `HERMES_COPILOT_ACP_COMMAND`. The stock provider treated the
subprocess as a dumb completion endpoint: it injected a tool catalog into the
prompt and scraped `<tool_call>` blocks back out of the text.

That is the wrong shape for a target that is already an agent with its own tools,
its own permission model, and its own session lifecycle. The fork makes
`copilot-acp` a **native ACP client**: real tool calls over the wire, real
permission RPCs routed to Hermes' approval gate, persistent sessions, streaming,
and Hermes' own tool surface handed back to the agent over MCP.

## Read these in order

| Doc | What it answers |
|---|---|
| [`changes.md`](changes.md) | What we changed, commit by commit and file by file |
| [`surfaces.md`](surfaces.md) | Config keys, env vars, gateway RPC, desktop UI — the contracts a user or another surface touches |
| [`wire-contracts.md`](wire-contracts.md) | Exact ACP/SDK wire shapes, read off `acp-agent.js` or proven on the wire. Do not guess these |
| [`verification.md`](verification.md) | The probe harnesses and the test set that gate the fork |
| [`upstreaming.md`](upstreaming.md) | What should go to Nous, what stays local, and why |

The operational runbook — pull, rebase, rebuild, swap the locked binary — is the
`hermes-fork-maintenance` skill at `~/.claude/skills/hermes-fork-maintenance/`.
This directory documents *what the code is*; the skill documents *how to work on
it on this machine*. Keep them from duplicating: machine-specific paths and
shell traps belong in the skill, code contracts belong here.

## Invariants

Things that are true of the fork as a whole. Breaking one of these is a bug, not
a design choice.

1. **The backend decides what is Claude; the renderer obeys.** `copilot-acp` is a
   generic provider — it is Claude only because of a reroute. The truth test is
   `hermes_cli/models.py::_copilot_acp_is_rerouted()`. It is surfaced to the
   desktop as a flag (`session.info.acp_permission.available`). Never re-derive
   the reroute heuristic in TypeScript.

2. **Nothing widens permissions silently.** Every default that could loosen what
   runs without a human is empty/off: `copilot_acp.permission_mode` defaults to
   `""` (leave the agent alone), `bypassPermissions` is behind a confirm in the
   UI, and the approval gate fails closed when there is no human to ask.

3. **Env beats config beats session-default.** An operator who pins
   `HERMES_ACP_PERMISSION_MODE` at launch cannot be overridden by a user's
   config or a click in the UI. The UI is told about it (`locked: true`) rather
   than being allowed to fail silently.

4. **Tool suppression is enforced at the transport, not the prompt.** The
   advisory (`moa_reference`) and background-review paths hand the agent an
   empty `tools` array on the wire. A prompt asking a model not to use tools is
   not a control.

5. **Verify out-of-process.** The Python module loads once at process start and
   the desktop UI is a prebuilt bundle. "I edited the file" is not evidence that
   anything changed in the running app. Drive the real `claude-agent-acp` from a
   probe script and read the wire; for the desktop, prove the string reached
   `resources/app.asar`.

6. **Every positive test needs a negative control.** A probe that only runs the
   arm you expect to pass proves nothing about the arm you disabled.

## Keeping this alive

When you land a change on the fork:

- Add a row to the ledger in [`changes.md`](changes.md) — commit subject, files,
  and the one-sentence reason.
- If it adds a config key, env var, gateway method, or UI control, add it to
  [`surfaces.md`](surfaces.md). That file is the contract list; an undocumented
  surface is one nobody can find later.
- If you learned an SDK wire shape the hard way, put it in
  [`wire-contracts.md`](wire-contracts.md) with the `acp-agent.js` line or the
  probe that proved it.
- If you wrote a probe, register it in [`verification.md`](verification.md).

After an upstream rebase, re-check the line numbers cited here. They are
convenience anchors, not contracts — symbol names are the stable reference.
