#!/usr/bin/env node
// Wrapper: run claude-agent-acp under a scrubbed environment.
// Windows equivalent of `env -i PATH=... CLAUDE_CODE_OAUTH_TOKEN=$(cat ~/.token) claude-agent-acp "$@"`
//
// Why: if CLAUDE_CODE_OAUTH_TOKEN (or ANTHROPIC_API_KEY) is visible in Hermes'
// general environment, the SDK can silently fall back to the metered API.
// The token is read from disk here and injected ONLY into the child process.

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const TOKEN_FILE = path.join(os.homedir(), ".claude-acp-token");
const MODEL_FILE = path.join(os.homedir(), ".claude-acp-model");
const ENTRY = path.join(
  process.env.CLAUDE_ACP_ROOT ||
    path.join(path.dirname(process.execPath), "node_modules"),
  "@agentclientprotocol",
  "claude-agent-acp",
  "dist",
  "index.js",
);

function cleanToken(raw) {
  let t = raw;
  if (t.charCodeAt(0) === 0xfeff) t = t.slice(1); // strip UTF-8/UTF-16 BOM
  t = t.replace(/\0/g, "").trim(); // strip UTF-16LE NULs from PS redirection
  // strip a wrapping pair of quotes if the value was saved with them
  if (
    (t.startsWith('"') && t.endsWith('"')) ||
    (t.startsWith("'") && t.endsWith("'"))
  ) {
    t = t.slice(1, -1).trim();
  }
  return t;
}

let token;
try {
  token = cleanToken(fs.readFileSync(TOKEN_FILE, "utf8"));
} catch {
  console.error(`[claude-acp-run] Missing token file: ${TOKEN_FILE}
Generate one with:  claude setup-token
then save the value into that file (chmod 600 on POSIX).`);
  process.exit(1);
}
if (!token) {
  console.error(`[claude-acp-run] Token file ${TOKEN_FILE} is empty.`);
  process.exit(1);
}

// Scrubbed env: allowlist only. Nothing from Hermes leaks in, and no stray
// ANTHROPIC_API_KEY can hijack billing onto the metered API.
const env = {
  PATH: process.env.PATH,
  HOME: os.homedir(),
  USERPROFILE: os.homedir(),
  TEMP: process.env.TEMP,
  TMP: process.env.TMP,
  SYSTEMROOT: process.env.SYSTEMROOT,
  APPDATA: process.env.APPDATA,
  LOCALAPPDATA: process.env.LOCALAPPDATA,
  LANG: process.env.LANG || "en_US.UTF-8",
  CLAUDE_CODE_OAUTH_TOKEN: token,
  // Tool-search deferral. Read off claude.exe: ZYr() maps a falsy
  // ENABLE_TOOL_SEARCH through su() to mode "standard", and c4() returns
  // false for "standard" -- i.e. no tool search, every tool schema loaded up
  // front. Left on (the default for a first-party host), MCP tool schemas are
  // withheld until a ToolSearch call fetches them, which is why Hermes'
  // memory/skill_manage arrive as bare names the model never reaches for.
  // Cost of turning it off: every MCP server's schemas ride in the prompt
  // each turn. Set CLAUDE_ACP_TOOL_SEARCH=true to restore the SDK default.
  ENABLE_TOOL_SEARCH: process.env.CLAUDE_ACP_TOOL_SEARCH || "false",
  // Claude Code's OWN auto-memory store (~/.claude/projects/<cwd>/memory/).
  // Under Hermes it is a second, competing memory the user never sees: Hermes
  // injects its own MEMORY.md/USER.md every turn and reads only
  // %LOCALAPPDATA%\hermes\memories, so a fact saved to the Claude store is a
  // fact silently lost. Read off claude.exe -- Cm() checks this env var BEFORE
  // the `autoMemoryEnabled` setting, so this wins without touching
  // ~/.claude/settings.json and a plain `claude` CLI session (where the var is
  // unset) keeps its own memory untouched:
  //   function Cm(){ ... let e=process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY;
  //     if(Yt(e))return!1; if(su(e))return!0; ...
  //     let t=eo(); if(t.autoMemoryEnabled!==void 0)return t.autoMemoryEnabled;
  //     return!0 }
  // Yt(x) = ["1","true","yes","on"], so "1" disables BOTH reads and writes.
  // Set CLAUDE_ACP_AUTO_MEMORY=1 to hand the ACP session its own store back.
  CLAUDE_CODE_DISABLE_AUTO_MEMORY: process.env.CLAUDE_ACP_AUTO_MEMORY ? "0" : "1",
};

// Model selection is negotiated over ACP by Hermes itself
// (session/set_config_option with configId="model"), so no ANTHROPIC_MODEL is
// set here. Left as an escape hatch for non-Hermes ACP clients that don't
// negotiate: CLAUDE_ACP_MODEL=<alias> or a value in ~/.claude-acp-model.
// Valid aliases: default | sonnet | opus | haiku | claude-fable-5[1m]
let model = process.env.CLAUDE_ACP_MODEL;
if (process.env.CLAUDE_ACP_NO_MODEL) {
  model = undefined; // test hook: force the ACP set_config_option path
} else if (!model) {
  try {
    model = cleanToken(fs.readFileSync(MODEL_FILE, "utf8"));
  } catch {
    /* no override file: fall through to the SDK default (Sonnet 5) */
  }
}
if (model) env.ANTHROPIC_MODEL = model;
for (const k of Object.keys(env)) if (env[k] === undefined) delete env[k];

// Hermes' copilot-acp provider appends Copilot-specific flags (`--acp --stdio`)
// when HERMES_COPILOT_ACP_ARGS is unset. claude-agent-acp takes no flags and
// already speaks ACP over stdio, so drop them defensively.
const passthrough = process.argv
  .slice(2)
  .filter((a) => a !== "--acp" && a !== "--stdio");

const child = spawn(process.execPath, [ENTRY, ...passthrough], {
  env,
  stdio: "inherit",
  cwd: process.cwd(),
});
child.on("exit", (code, sig) => process.exit(sig ? 1 : (code ?? 0)));
child.on("error", (err) => {
  console.error(`[claude-acp-run] Failed to start ${ENTRY}: ${err.message}`);
  process.exit(1);
});
