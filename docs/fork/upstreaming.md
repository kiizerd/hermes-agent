# Upstreaming

Shrinking the fork is the real fix. Most of what it carries is not personal
config — it is provider support anyone pointing `copilot-acp` at a real ACP agent
needs. Merged upstream, the fork drops to roughly five lines.

## Send upstream

Ranked roughly by how clearly they are everyone's bug, not ours.

| Change | Why it is general |
|---|---|
| Native tool mode | No catalog injection / no `<tool_call>` scraping when the target is already an agent. The current behaviour is wrong for every ACP agent, not just Claude |
| `session/request_permission` → the real approval gate | Otherwise a native agent's permission model is bypassed entirely |
| Per-target approval keys | `[a]lways` on one path blessing every path through the same tool is a security bug |
| Persistent sessions | One session per completion throws away the agent's context every turn |
| Streaming | `copilot-acp` was excluded from the streaming path for no reason that survives native mode |
| `tool_call` / `tool_call_update` rendering | Native agents' tools currently render blank cards |
| `build_tool_preview` fallback keys | Same |
| Thinking option | `_meta.claudeCode.options.thinking`, and the `MAX_THINKING_TOKENS` scrub that avoids a 400 on Opus 5 |
| `session/set_mode` + `_sync_acp_mode` | A mode change sitting inert until a session happens to rebuild is a bug |
| Per-turn token usage from the prompt result | Counts currently read as zero |
| Advisory tool-less mode | Enforces the reference advisor's no-tools contract at the transport instead of the prompt, and drops the per-tool approval round-trips that timed advisors out |
| `approvals.tool_allowlist` | Non-shell tools had no auto-approval path at all; `command_allowlist` only ever reached shell |
| Config MCP forwarding | A server the user configured in `config.yaml` being invisible to a native agent is a gap, not a policy |
| Markdown bare-fence streaming fixes | Pure renderer bug, nothing ACP about it |
| `sys.modules` restore in `test_empty_tool_name_loop_dampening.py` | One line; fixes cross-test pollution that makes unrelated tests flaky |

The permission-mode desktop pill is upstreamable in shape but depends on the
`acp_permission` gating flag; send the backend flag with it or it has nothing to
key off.

## Keep local

| Thing | Why |
|---|---|
| `Claude Sub ACP` provider label | Ours. Upstream's generic label is correct for upstream |
| Opus 4.8 catalog entry + `model-status-label` map entry | Machine-specific model set |
| `claude-acp/claude-acp-run.js` | Tracked in-repo, but ours: it exists only because `copilot-acp` is rerouted at a machine, and upstream has no such reroute |
| The probe harnesses under `~/.hermes-acp/` | Machine paths and tripwire servers; deliberately untracked |

**Do not** write a general dash-to-dot rule in `displayModelName` to avoid the
explicit map entry. It cannot distinguish a version tail from a name
(`claude-fable-5`) and would rewrite every other provider's labels.

## Deferred, not rejected

**Track B — a real named `claude-acp` provider.** Generalizing `copilot-acp` into
a properly named provider instead of relying on a reroute env var. Deferred by
the user. Keep edits clean enough to upstream later, but do not gate work on
Nous accepting anything.

## Why the fork exists at all

Beyond the features: pointing `origin` at the fork **disarms `hermes update`**.

Before, HEAD was not an ancestor of `origin/main`, so the
`git merge --ff-only origin/<branch>` path in `update_cmd.py` (~`:4248`) failed
and the fallback steered to `git reset --hard origin/<branch>` (~`:4275`) —
silently dropping every local commit.

With `origin` on the fork, `_is_fork(origin_url)` (`update_cmd.py:1495`) returns
True, and `_sync_with_upstream_if_needed` (`:1583`) early-returns at `:1656`:

```python
if origin_ahead > 0:
    print("  Skipping upstream sync to preserve your changes.")
    return
```

`commit_count` for `HEAD..origin/main` is 0, so the reset path is never entered.
Both hazards are inert. Verify after any remote change:

```bash
python -c "from hermes_cli.update_cmd import _is_fork; print(_is_fork('https://github.com/kiizerd/hermes-agent.git'))"  # True
git rev-list --count upstream/main..origin/main   # >0 => sync refuses to trample
git rev-list --count HEAD..origin/main            # 0  => no reset path
```

**The tradeoff, stated plainly:** in fork mode `hermes update` will never pull
upstream code again. `origin_ahead > 0` is permanent while we carry patches, so
the sync always returns early. Updates are fully manual — that is the price of a
branch that cannot be clobbered, and it is the reason the rebase runbook in the
`hermes-fork-maintenance` skill exists.
