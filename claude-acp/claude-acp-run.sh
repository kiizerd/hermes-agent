#!/usr/bin/env bash
# POSIX variant (Linux / macOS / WSL) of claude-acp-run.js.
#
# Why: if CLAUDE_CODE_OAUTH_TOKEN (or ANTHROPIC_API_KEY) is visible in Hermes'
# general environment, the SDK can silently fall back to the metered API. The
# token is read from disk here and injected ONLY into the child process.
#
# Point Hermes at it with:
#   HERMES_COPILOT_ACP_COMMAND=/path/to/claude-acp/claude-acp-run.sh
#
# PARITY: the env allowlist below must match claude-acp-run.js. The reasoning
# for ENABLE_TOOL_SEARCH and CLAUDE_CODE_DISABLE_AUTO_MEMORY -- both decoded out
# of the compiled claude.exe -- is documented in full in the .js; it is not
# repeated here. Change one file, change the other.
set -euo pipefail

TOKEN_FILE="${CLAUDE_ACP_TOKEN_FILE:-$HOME/.claude-acp-token}"
MODEL_FILE="${CLAUDE_ACP_MODEL_FILE:-$HOME/.claude-acp-model}"

# Strip a UTF-8 BOM, NULs (UTF-16LE redirection artefacts), CRs, surrounding
# whitespace, and a wrapping pair of quotes. Pure bash -- no sed dialect
# differences between GNU and BSD.
clean() {
  local v
  v="$(tr -d '\000\r' < "$1")"
  v="${v#$'\xEF\xBB\xBF'}"
  v="${v%%$'\n'*}"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  case "$v" in
    \"*\") v="${v#\"}"; v="${v%\"}" ;;
    \'*\') v="${v#\'}"; v="${v%\'}" ;;
  esac
  printf '%s' "$v"
}

if [ ! -s "$TOKEN_FILE" ]; then
  echo "[claude-acp-run] Missing or empty token file: $TOKEN_FILE" >&2
  echo "Generate one with:  claude setup-token" >&2
  echo "then save the value into that file (chmod 600)." >&2
  exit 1
fi

TOKEN="$(clean "$TOKEN_FILE")"
if [ -z "$TOKEN" ]; then
  echo "[claude-acp-run] Token file $TOKEN_FILE is empty." >&2
  exit 1
fi

# Model selection is normally negotiated over ACP by Hermes itself
# (session/set_config_option with configId="model"), so no ANTHROPIC_MODEL is
# set by default. Left as an escape hatch for non-Hermes ACP clients that do not
# negotiate: CLAUDE_ACP_MODEL=<alias> or a value in ~/.claude-acp-model.
# Valid aliases: default | sonnet | opus | haiku | claude-fable-5[1m]
MODEL="${CLAUDE_ACP_MODEL:-}"
if [ -n "${CLAUDE_ACP_NO_MODEL:-}" ]; then
  MODEL=""                        # test hook: force the ACP set_config_option path
elif [ -z "$MODEL" ] && [ -s "$MODEL_FILE" ]; then
  MODEL="$(clean "$MODEL_FILE")"
fi

# Scrubbed env: allowlist only. Nothing from Hermes leaks in, and no stray
# ANTHROPIC_API_KEY can hijack billing onto the metered API.
ENV_ARGS=(
  PATH="$PATH"
  HOME="$HOME"
  LANG="${LANG:-en_US.UTF-8}"
  CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
  ENABLE_TOOL_SEARCH="${CLAUDE_ACP_TOOL_SEARCH:-false}"
)
if [ -n "${CLAUDE_ACP_AUTO_MEMORY:-}" ]; then
  ENV_ARGS+=(CLAUDE_CODE_DISABLE_AUTO_MEMORY=0)
else
  ENV_ARGS+=(CLAUDE_CODE_DISABLE_AUTO_MEMORY=1)
fi
if [ -n "${TMPDIR:-}" ]; then
  ENV_ARGS+=(TMPDIR="$TMPDIR")
fi
if [ -n "$MODEL" ]; then
  ENV_ARGS+=(ANTHROPIC_MODEL="$MODEL")
fi

# Hermes' copilot-acp provider appends Copilot-specific flags (`--acp --stdio`)
# when HERMES_COPILOT_ACP_ARGS is unset. claude-agent-acp takes no flags and
# already speaks ACP over stdio, so drop them defensively.
PASSTHROUGH=()
for a in "$@"; do
  case "$a" in
    --acp|--stdio) ;;
    *) PASSTHROUGH+=("$a") ;;
  esac
done

# CLAUDE_ACP_ROOT overrides the module root, matching the .js. Used by the wire
# probes to point at a stub index.js and read the env the child actually gets.
if [ -n "${CLAUDE_ACP_ROOT:-}" ]; then
  ENTRY=(node "$CLAUDE_ACP_ROOT/@agentclientprotocol/claude-agent-acp/dist/index.js")
else
  ENTRY=(claude-agent-acp)
fi

exec env -i "${ENV_ARGS[@]}" "${ENTRY[@]}" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
