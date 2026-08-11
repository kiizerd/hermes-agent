"""OpenAI-compatible shim that forwards Hermes requests to `copilot --acp`.

This adapter lets Hermes treat the GitHub Copilot ACP server as a chat-style
backend. Each request starts a short-lived ACP session, sends the formatted
conversation as a single prompt, collects text chunks, and converts the result
back into the minimal shape Hermes expects from an OpenAI client.
"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from agent.file_safety import get_read_block_error, get_write_denied_error, is_write_approval_required
from agent.redact import redact_sensitive_text
from tools.environments.local import hermes_subprocess_env

ACP_MARKER_BASE_URL = "acp://copilot"
logger = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 900.0
# ACP config option id for model selection (stable across ACP agents).
_ACP_MODEL_CONFIG_ID = "model"

# Opt-outs for the two behaviours that depend on which agent is on the far end.
_TOOL_MODE_ENV = "HERMES_ACP_TOOL_MODE"
_PERSISTENT_SESSION_ENV = "HERMES_ACP_PERSISTENT_SESSION"
# Tool-activity lines are surfaced as reasoning, not as answer text, so keep
# any single one short enough that a long bash command can't drown the trace.
_TOOL_ACTIVITY_MAX_CHARS = 300

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_JSON_RE = re.compile(r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,\s*\"function\"\s*:\s*\{.*?\}\s*\}", re.DOTALL)

# Stderr fingerprint of the deprecated `gh copilot` CLI extension
# (https://github.blog/changelog/2025-09-25-upcoming-deprecation-of-gh-copilot-cli-extension).
# We require BOTH the literal product name ("gh-copilot") AND a deprecation
# marker, so generic stderr from the NEW `@github/copilot` CLI — whose repo
# is github.com/github/copilot-cli and which legitimately mentions "copilot-cli"
# in its own banners and error messages — doesn't get misclassified as the
# deprecated extension.
_DEPRECATION_REQUIRED = ("gh-copilot",)
_DEPRECATION_MARKERS = (
    "has been deprecated",
    "no commands will be executed",
)


def _is_gh_copilot_deprecation_message(stderr_text: str) -> bool:
    """True iff stderr looks like the deprecated gh-copilot extension's banner."""

    lower = stderr_text.lower()
    if not any(req in lower for req in _DEPRECATION_REQUIRED):
        return False
    return any(marker in lower for marker in _DEPRECATION_MARKERS)


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_COPILOT_ACP_ARGS", "").strip()
    if not raw:
        return ["--acp", "--stdio"]
    return shlex.split(raw)


# Probe verdicts cached per binary path so repeated prompts against a
# CLI that supports --acp pay the ~50ms --help cost exactly once per
# process. Only definitive verdicts (True/False) are cached; an
# inconclusive probe (binary missing, --help crashed or timed out) is
# not cached so a CLI installed mid-session is picked up.
_ACP_PROBE_CACHE: dict[str, bool] = {}


def _acp_supported(command: str, args: list[str]) -> bool | None:
    """Tri-state probe: does ``command`` accept the ACP args we'd pass?

    Different CLI versions support different transports. The GitHub
    Copilot CLI (`@github/copilot`, late 2025+) ships with ``--acp``;
    older releases (and Claude Code v2.x as of Aug 2026) do not.
    Spawning a CLI that doesn't recognize the flag silently exits
    with code 1 and ``error: unknown option '--acp'`` on stderr,
    after which every delegate_task call hangs the parent for
    ``child_timeout_seconds`` (default 600s) waiting for stdout
    that never arrives.

    Returns:
      - ``True``  — help text advertises ``--acp``; safe to spawn.
      - ``False`` — help ran cleanly but ``--acp`` is absent; spawning
        would hang, so the caller should fast-fail with a clear error.
      - ``None``  — inconclusive (binary missing, --help failed or
        timed out). The caller must fall through to the normal spawn
        path, which surfaces the existing "Could not start Copilot ACP
        command" error with full context.

    Only probes when ``--acp`` is actually among ``args``: a custom
    HERMES_COPILOT_ACP_ARGS transport is the operator's business.
    """
    if "--acp" not in args:
        return True
    cached = _ACP_PROBE_CACHE.get(command)
    if cached is not None:
        return cached
    try:
        probe = subprocess.run(
            [command, "--help"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        # --help itself failed; can't tell anything about --acp.
        return None
    # Match ``--acp`` as a flag in the help text; tolerate spacing and
    # variants like ``[--acp]``.
    verdict = bool(re.search(r"(?:^|[\s\[])--acp(?:[\s=\],]|$)", probe.stdout, re.MULTILINE))
    _ACP_PROBE_CACHE[command] = verdict
    return verdict


def _resolve_tool_mode(command: str, args: list[str] | None) -> str:
    """Which tool protocol the agent on the far end of the ACP wire speaks.

    ``bridge`` -- the agent has no tools of its own (GitHub Copilot CLI under
    ``--acp``, which this shim was written against). Hermes injects its own
    tool catalog into the prompt and scrapes ``<tool_call>`` blocks back out
    of the reply.

    ``native`` -- the agent brings and executes its own tools (Claude Code,
    Codex, Gemini CLI...). Stacking Hermes' catalog on top of an agent that is
    already a full agent makes it oscillate between emitting ``<tool_call>``
    JSON for Hermes tools it cannot see the results of and quietly using its
    real ones, which Hermes then never observes. So in this mode Hermes
    injects no catalog, gives no ``<tool_call>`` instruction, and relays text.
    """
    override = os.getenv(_TOOL_MODE_ENV, "").strip().lower()
    if override in {"bridge", "native"}:
        return override
    # `copilot` may be invoked indirectly (`node .../copilot.js`), so match on
    # the whole argv rather than the executable's basename.
    blob = " ".join([command or ""] + [str(a) for a in (args or [])]).lower()
    return "bridge" if "copilot" in blob else "native"


def _persistent_sessions_enabled(tool_mode: str) -> bool:
    """Whether one ACP session should span multiple Hermes turns.

    Native-tool agents hold per-session state Hermes cannot rebuild by
    re-pasting the transcript (todo lists, subagent handles, read caches,
    approval grants), so tearing the session down every turn discards real
    work. Bridge agents are stateless text completers, where a fresh session
    per request stays the safer default.
    """
    raw = os.getenv(_PERSISTENT_SESSION_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return tool_mode == "native"


def _resolve_home_dir() -> str:
    """Return a stable HOME for child ACP processes."""
    home = os.environ.get("HOME", "").strip()
    if home:
        return home

    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        return expanded

    try:
        import pwd

        resolved = pwd.getpwuid(os.getuid()).pw_dir.strip()  # windows-footgun: ok — POSIX fallback inside try/except (pwd import fails on Windows)
        if resolved:
            return resolved
    except Exception:
        pass

    # Last resort: /tmp (writable on any POSIX system). Avoids crashing the
    # subprocess with no HOME; callers can set HERMES_HOME explicitly if they
    # need a different writable dir.
    return "/tmp"


def _build_subprocess_env() -> dict[str, str]:
    # Copilot ACP is a model-driving CLI executor: it legitimately needs LLM
    # provider credentials. Route through the central helper so Tier-1 secrets
    # (gateway bot tokens, GitHub auth, infra) are still stripped (#29157).
    env = hermes_subprocess_env(inherit_credentials=True)
    home = _resolve_home_dir()
    env["HOME"] = home
    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(env)
    return env


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _permission_denied(message_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "outcome": {
                "outcome": "cancelled",
            }
        },
    }


def _raw_input_detail(raw_input: Any) -> str:
    """Best-effort "what is this tool call actually touching" string.

    ``rawInput`` is agent-defined, so probe the argument names real agents use
    for their primary target rather than assuming one schema.
    """
    if not isinstance(raw_input, dict):
        return ""
    for key in (
        "command",
        "cmd",
        "path",
        "file_path",
        "filePath",
        "pattern",
        "query",
        "url",
        "prompt",
    ):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _merge_tool_call_fields(
    remembered: dict[str, Any], update: dict[str, Any]
) -> dict[str, Any]:
    """Fold one ``session/update`` into what is already known about a call.

    ACP updates are partial deltas: the opening ``tool_call`` fires before the
    agent has finished streaming its arguments (so ``rawInput`` is typically
    ``{}`` and the title is a bare tool name), and later refinements may carry
    only the fields that changed -- a status-only update has no ``title`` or
    ``kind`` at all. Overwriting blindly would therefore blank out the very
    fields the refinement was supposed to fill in, so only non-empty values
    win and everything else carries forward.
    """
    merged = dict(remembered)
    for key in ("title", "kind", "status"):
        value = update.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value
    raw_input = update.get("rawInput")
    if isinstance(raw_input, dict) and raw_input:
        # Streamed refinements grow toward the complete argument set, so the
        # latest non-empty one is always the most informative.
        merged["rawInput"] = raw_input
    meta = update.get("_meta")
    if isinstance(meta, dict) and meta:
        merged["_meta"] = {**(remembered.get("_meta") or {}), **meta}
    # Output only ever arrives on the consolidated terminal update.
    for key in ("content", "rawOutput"):
        value = update.get(key)
        if value:
            merged[key] = value
    return merged


def _format_tool_activity(update: dict[str, Any]) -> str:
    """One-line trace of a native agent's own tool call.

    Native-tool agents run their tools themselves and report them over
    ``session/update``. Hermes cannot execute or intercept those, but dropping
    them entirely leaves the user watching an agent that appears to do nothing
    between prose chunks, so render each as a compact reasoning line.
    """
    title = str(update.get("title") or "").strip()
    kind = str(update.get("kind") or "").strip()
    detail = _raw_input_detail(update.get("rawInput"))
    label = title or kind or "tool"
    if detail and detail != label:
        label = f"{label}: {detail}"
    label = " ".join(label.split())
    if len(label) > _TOOL_ACTIVITY_MAX_CHARS:
        label = label[: _TOOL_ACTIVITY_MAX_CHARS - 1] + "…"
    status = str(update.get("status") or "").strip().lower()
    if status in {"failed", "error"}:
        return f"[tool failed] {label}"
    return f"[tool] {label}"


#: Cap on the tool result forwarded to the UI. Matches the codex bridge's own
#: limit -- a full Read result is megabytes and the card only shows a summary.
_TOOL_RESULT_MAX_CHARS = 4000

#: ACP tool-call statuses that end a call. ``cancelled`` covers a denied
#: permission request, which the agent reports as a terminal state rather than
#: a failure.
_TERMINAL_TOOL_STATUSES = frozenset(
    {"completed", "failed", "error", "cancelled", "canceled"}
)


def _acp_tool_display(fields: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a merged ACP tool call to the ``(name, args)`` Hermes' UI expects.

    ``_meta.claudeCode.toolName`` is the agent's own tool name (``Read``,
    ``Bash``, ``Edit``, ...) and is carried on every ``tool_call`` and
    ``tool_call_update``; ``title`` is a humanised phrase and ``kind`` is the
    coarse ACP enum, so neither substitutes for it. Other ACP agents omit the
    ``claudeCode`` sidecar entirely, hence the ``kind`` fallback.

    The name is deliberately *not* translated to a Hermes tool name: the
    gateway's edit-snapshot and inline-diff paths key off names like ``patch``
    and ``write_file`` with Hermes' own argument and result shapes, and feeding
    them ACP shapes would render a diff from data that never described one.
    Unknown names fall through to the generic tool card instead.
    """
    meta = fields.get("_meta")
    claude_meta = meta.get("claudeCode") if isinstance(meta, dict) else None
    name = ""
    if isinstance(claude_meta, dict):
        name = str(claude_meta.get("toolName") or "").strip()
    if not name:
        name = str(fields.get("kind") or "").strip() or "tool"
    raw_input = fields.get("rawInput")
    args = dict(raw_input) if isinstance(raw_input, dict) else {}
    return name, args


def _acp_tool_result_text(fields: dict[str, Any]) -> str:
    """Best-effort result string for a finished ACP tool call.

    Never returns empty: the gateway treats a falsy result as "no output to
    show" and the card would render as though the tool did nothing.
    """
    raw_output = fields.get("rawOutput")
    if raw_output:
        try:
            return json.dumps(raw_output, ensure_ascii=False)[:_TOOL_RESULT_MAX_CHARS]
        except Exception:
            return str(raw_output)[:_TOOL_RESULT_MAX_CHARS]
    texts: list[str] = []
    content = fields.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            if isinstance(inner, dict):
                text = inner.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
    if texts:
        return "\n".join(texts)[:_TOOL_RESULT_MAX_CHARS]
    return f"status={fields.get('status') or 'completed'}"


def _describe_permission_request(tool_call: dict[str, Any]) -> tuple[str, str]:
    """Build (command, description) display strings from an ACP ToolCallUpdate.

    ``rawInput`` and ``title`` are the only fields agents reliably populate;
    fall back through them so the approval prompt never shows a blank target.
    """
    title = str(tool_call.get("title") or "").strip()
    kind = str(tool_call.get("kind") or "").strip()
    command = _raw_input_detail(tool_call.get("rawInput"))
    if not command:
        command = title or "(unspecified action)"
    description = f"Copilot requests to {kind}" if kind else "Copilot requests permission"
    if title and title != command:
        description += f": {title}"
    return command, description


def _acp_tool_name(tool_call: dict[str, Any]) -> str:
    """The agent's own tool name (``Bash``, ``Edit``, ``Read``, ...).

    Same ``_meta.claudeCode.toolName`` field the tool cards read. Agents
    without the ``claudeCode`` sidecar return "", and the caller then treats
    the request as non-shell, which is the safe direction: it gates rather
    than assuming a command it cannot see is harmless.
    """
    meta = tool_call.get("_meta")
    claude_meta = meta.get("claudeCode") if isinstance(meta, dict) else None
    if isinstance(claude_meta, dict):
        return str(claude_meta.get("toolName") or "").strip()
    return ""


def _acp_shell_command(tool_call: dict[str, Any]) -> str:
    """The literal shell string a Bash tool call will run.

    Only ``command``/``cmd`` count. ``_raw_input_detail`` also falls back to
    paths and queries, which must never be fed to the shell-pattern matcher:
    scanning a filename for ``rm -rf`` is meaningless and would let a real
    command through under a key derived from the wrong text.
    """
    raw_input = tool_call.get("rawInput")
    if not isinstance(raw_input, dict):
        return ""
    for key in ("command", "cmd"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _select_permission_option(options: list[Any]) -> str | None:
    """Pick the option_id to accept with, matching by ``kind`` rather than
    assuming a specific id -- the remote agent assigns its own optionId
    strings. Prefers a single-use grant; falls back to a durable one only
    if that is all the agent offered."""
    by_kind: dict[str, str] = {}
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind") or "")
        option_id = opt.get("optionId")
        if kind and isinstance(option_id, str) and kind not in by_kind:
            by_kind[kind] = option_id
    return by_kind.get("allow_once") or by_kind.get("allow_always")


def _current_hermes_session_id() -> str:
    """Hermes' current session id, as exported by agent init.

    Read live rather than cached: one client instance can outlive a chat, and
    the value is what the hermes-tools MCP subprocess was given for
    "which session is the current one" in cross-session recall.
    """
    return (os.environ.get("HERMES_SESSION_ID") or "").strip()


def _acp_model_option_values(session: Any) -> list[str]:
    """Model values advertised by an ACP agent's session/new response."""
    if not isinstance(session, dict):
        return []
    options = session.get("configOptions")
    if not isinstance(options, list):
        return []
    for opt in options:
        if not isinstance(opt, dict) or opt.get("id") != _ACP_MODEL_CONFIG_ID:
            continue
        values: list[str] = []
        for choice in opt.get("options") or []:
            if isinstance(choice, dict):
                value = choice.get("value")
                if isinstance(value, str) and value:
                    values.append(value)
        return values
    return []


def _select_acp_model(request: Any, session: Any, session_id: str, model: str) -> None:
    """Ask the ACP agent to switch models, if it advertises the requested one.

    Best-effort by design: the ACP model config option is optional, agents are
    free to reject a value, and a failure here must never break an otherwise
    working prompt. Falls back silently to the agent's current model.
    """
    supported = _acp_model_option_values(session)
    if not supported:
        return
    match = next((v for v in supported if v == model), None)
    if match is None:
        match = next((v for v in supported if v.lower() == model.lower()), None)
    if match is None:
        logger.debug(
            "ACP agent does not advertise model %r (has: %s); leaving default.",
            model,
            ", ".join(supported),
        )
        return
    try:
        request(
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": _ACP_MODEL_CONFIG_ID,
                "value": match,
            },
        )
    except Exception as exc:  # pragma: no cover - agent-dependent
        logger.debug("ACP session/set_config_option for model %r failed: %s", match, exc)


def _acp_mode_ids(session: Any) -> list[str]:
    """Permission-mode ids advertised by an ACP agent's session/new response."""
    if not isinstance(session, dict):
        return []
    modes = session.get("modes")
    if not isinstance(modes, dict):
        return []
    ids: list[str] = []
    for entry in modes.get("availableModes") or []:
        if isinstance(entry, dict):
            mode_id = entry.get("id")
            if isinstance(mode_id, str) and mode_id:
                ids.append(mode_id)
    return ids


def _requested_acp_mode() -> str:
    """Permission mode to request, from HERMES_ACP_PERMISSION_MODE.

    Empty by default: an unset variable leaves the agent on whatever mode it
    chose for itself, so this never silently widens what runs without a human
    approving it. Opting in is a deliberate act by the operator.
    """
    return (os.environ.get("HERMES_ACP_PERMISSION_MODE") or "").strip()


def _select_acp_mode(request: Any, session: Any, session_id: str, mode: str) -> None:
    """Ask the ACP agent to switch permission modes, if it advertises the id.

    The agent decides its own starting mode; a settings.json ``defaultMode`` is
    only consulted in code paths that read the user's real HOME, which a
    sanitising launcher wrapper can hide. Setting it explicitly over ACP is the
    supported route and works regardless of how the child was launched.

    Best-effort like the model selector: modes are optional in ACP, agents may
    reject a value, and a failure here must never break a working prompt.
    """
    supported = _acp_mode_ids(session)
    if not supported:
        return
    match = next((v for v in supported if v == mode), None)
    if match is None:
        match = next((v for v in supported if v.lower() == mode.lower()), None)
    if match is None:
        logger.warning(
            "ACP agent does not advertise permission mode %r (has: %s); "
            "leaving the agent's own default.",
            mode,
            ", ".join(supported),
        )
        return
    try:
        request(
            "session/set_mode",
            {"sessionId": session_id, "modeId": match},
        )
        logger.info("ACP permission mode set to %r", match)
    except Exception as exc:  # pragma: no cover - agent-dependent
        logger.warning("ACP session/set_mode for %r failed: %s", match, exc)


def _iter_renderable(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Normalise a Hermes message list to ``(role, rendered_text)`` pairs.

    Shared by the prompt renderer and the transcript fingerprint so the two
    agree on exactly which messages exist -- an index computed against one and
    applied to the other would silently mis-slice the delta.
    """
    pairs: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "context"
        rendered = _render_message_content(message.get("content"))
        if not rendered:
            continue
        pairs.append((role, rendered))
    return pairs


def _fingerprint_pairs(pairs: list[tuple[str, str]]) -> list[str]:
    """Per-message ids used to test whether the caller's transcript is an
    append-only extension of what the live ACP session already received."""
    return [
        f"{role}:{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}"
        for role, text in pairs
    ]


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    *,
    tool_mode: str = "bridge",
    include_preamble: bool = True,
) -> str:
    return _render_prompt(
        _iter_renderable(messages),
        model=model,
        tools=tools,
        tool_choice=tool_choice,
        tool_mode=tool_mode,
        include_preamble=include_preamble,
    )


def _render_prompt(
    pairs: list[tuple[str, str]],
    *,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    tool_mode: str = "bridge",
    include_preamble: bool = True,
) -> str:
    """Render an ACP prompt.

    ``include_preamble=False`` is the follow-up turn of a persistent session:
    the agent already holds the standing instructions and everything sent
    before, so only the new transcript slice goes over the wire.
    """
    native = tool_mode == "native"
    sections: list[str] = []

    if not include_preamble:
        return _render_transcript_sections(
            sections, pairs, "New messages since your last reply:"
        )

    sections.append("You are being used as the active ACP agent backend for Hermes.")
    if native:
        sections.append(
            "You have your own tools and execute them yourself. Use them directly. "
            "Do NOT emit <tool_call> blocks and do not narrate a tool call in prose "
            "instead of making it: Hermes relays only your text back to the user and "
            "runs nothing on your behalf."
        )
    else:
        sections.append("Use ACP capabilities to complete tasks.")
        sections.append(
            "IMPORTANT: If you take an action with a tool, you MUST output tool calls using <tool_call>{...}</tool_call> blocks with JSON exactly in OpenAI function-call shape."
        )
        sections.append("If no tool is needed, answer normally.")
    if model:
        sections.append(f"Hermes requested model hint: {model}")

    # A native agent's own tools are the ones that will actually run; listing
    # Hermes' catalog next to them only invites calls Hermes cannot fulfil.
    if native:
        tools = None
        tool_choice = None

    if isinstance(tools, list) and tools:
        tool_specs: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            tool_specs.append(
                {
                    "name": name.strip(),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        if tool_specs:
            sections.append(
                "Available tools (OpenAI function schema). "
                "When using a tool, emit ONLY <tool_call>{...}</tool_call> with one JSON object "
                "containing id/type/function{name,arguments}. arguments must be a JSON string.\n"
                + json.dumps(tool_specs, ensure_ascii=False)
            )

    if tool_choice is not None:
        sections.append(f"Tool choice hint: {json.dumps(tool_choice, ensure_ascii=False)}")

    return _render_transcript_sections(sections, pairs, "Conversation transcript:")


_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "context": "Context",
}


def _render_transcript_sections(
    sections: list[str],
    pairs: list[tuple[str, str]],
    header: str,
) -> str:
    transcript = [
        f"{_ROLE_LABELS.get(role, role.title())}:\n{text}" for role, text in pairs
    ]
    if transcript:
        sections.append(f"{header}\n\n" + "\n\n".join(transcript))
    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if "content" in content and isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _build_openai_tool_call(
    *,
    call_id: str,
    name: str,
    arguments: str,
) -> ChatCompletionMessageToolCall:
    """Build an OpenAI-compatible tool-call object for downstream handling."""
    return ChatCompletionMessageToolCall(
        id=call_id,
        call_id=call_id,
        response_item_id=None,
        type="function",
        function=Function(name=name, arguments=arguments),
    )


def _completion_to_stream_chunks(completion: SimpleNamespace) -> list[SimpleNamespace]:
    """Convert a one-shot ACP response into OpenAI-style stream chunks."""
    choice = completion.choices[0]
    message = choice.message
    tool_call_deltas = None
    if message.tool_calls:
        tool_call_deltas = []
        for index, tool_call in enumerate(message.tool_calls):
            tool_call_deltas.append(
                SimpleNamespace(
                    index=index,
                    id=getattr(tool_call, "id", None),
                    type=getattr(tool_call, "type", "function"),
                    function=SimpleNamespace(
                        name=getattr(tool_call.function, "name", None),
                        arguments=getattr(tool_call.function, "arguments", None),
                    ),
                )
            )

    delta = SimpleNamespace(
        role="assistant",
        content=message.content or None,
        tool_calls=tool_call_deltas,
        reasoning_content=message.reasoning_content,
        reasoning=message.reasoning,
    )
    data_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=delta,
                finish_reason=choice.finish_reason,
            )
        ],
        model=completion.model,
        usage=None,
    )
    usage_chunk = SimpleNamespace(
        choices=[],
        model=completion.model,
        usage=completion.usage,
    )
    return [data_chunk, usage_chunk]


def _stream_tool_line(
    call_id: str,
    tool_lines: dict[str, dict[str, Any]],
    emit: Any,
) -> None:
    """Stream a tool call's fallback reasoning line, exactly once.

    The accumulating path rewrites a call's line in place as refinements
    arrive; a stream cannot retract what it already sent. So this waits for the
    same settled point the tool card waits for -- arguments known, or a
    terminal status reached without them -- and sends a single line.
    """
    entry = tool_lines.get(call_id)
    if entry is None or entry.get("stream_line_sent"):
        return
    fields = entry.get("fields") or {}
    status = str(fields.get("status") or "").strip().lower()
    raw_input = fields.get("rawInput")
    has_args = isinstance(raw_input, dict) and bool(raw_input)
    if not (has_args or status in _TERMINAL_TOOL_STATUSES):
        return
    entry["stream_line_sent"] = True
    emit("reasoning", f"\n{_format_tool_activity(fields)}\n")


def _acp_stream_chunk(
    model: str,
    *,
    content: str | None = None,
    reasoning: str | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    """One OpenAI-style streaming chunk carrying a single delta.

    Both ``reasoning_content`` and ``reasoning`` are set because the consumer
    reads whichever it finds first; carrying only one of them makes the chunk
    depend on which provider convention that lookup happens to prefer.
    """
    delta = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=None,
        reasoning_content=reasoning,
        reasoning=reasoning,
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
        ],
        model=model,
        usage=None,
    )


def _acp_usage_chunk(model: str) -> SimpleNamespace:
    """Final choices-less chunk. ACP reports no token counts, so it is zeros."""
    return SimpleNamespace(
        choices=[],
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


def _extract_tool_calls_from_text(text: str) -> tuple[list[ChatCompletionMessageToolCall], str]:
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[ChatCompletionMessageToolCall] = []
    consumed_spans: list[tuple[int, int]] = []

    def _try_add_tool_call(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"acp_call_{len(extracted)+1}"

        extracted.append(
            _build_openai_tool_call(
                call_id=call_id,
                name=fn_name.strip(),
                arguments=fn_args,
            )
        )

    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        raw = m.group(1)
        _try_add_tool_call(raw)
        consumed_spans.append((m.start(), m.end()))

    # Only try bare-JSON fallback when no XML blocks were found.
    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            raw = m.group(0)
            _try_add_tool_call(raw)
            consumed_spans.append((m.start(), m.end()))

    if not consumed_spans:
        return extracted, text.strip()

    consumed_spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned



def _ensure_path_within_cwd(path_text: str, cwd: str) -> Path:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved = candidate.resolve()
    root = Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside the session cwd '{root}'.") from exc
    return resolved


# A persistent session outlives the request that opened it, so nothing else
# guarantees the child agent gets reaped -- an abandoned one would keep running
# after Hermes exits. Track clients weakly and terminate any survivors.
_LIVE_CLIENTS: "weakref.WeakSet[CopilotACPClient]" = weakref.WeakSet()


def _reap_live_clients() -> None:
    for client in list(_LIVE_CLIENTS):
        try:
            client.close()
        except Exception:
            pass


atexit.register(_reap_live_clients)


class _ACPChatCompletions:
    def __init__(self, client: "CopilotACPClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ACPChatNamespace:
    def __init__(self, client: "CopilotACPClient"):
        self.completions = _ACPChatCompletions(client)


class CopilotACPClient:
    """Minimal OpenAI-client-compatible facade for Copilot ACP."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "copilot-acp"
        self.base_url = base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command()
        self._acp_args = list(acp_args or args or _resolve_args())
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = _ACPChatNamespace(self)
        self.is_closed = False
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_lock = threading.Lock()
        # Persistent-session state. Guarded by _session_lock, which serialises
        # whole turns: one ACP session cannot interleave two prompts.
        self._session_lock = threading.RLock()
        self._inbox: queue.Queue[dict[str, Any]] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._next_request_id = 0
        self._session_id: str | None = None
        self._session_model: str | None = None
        # Hermes session id baked into the hermes-tools MCP subprocess env at
        # session/new. A live ACP session outlives a Hermes chat, so this is
        # tracked to force a rebuild rather than letting cross-session recall
        # keep filtering against a session the user already left.
        self._session_hermes_id: str | None = None
        self._sent_fingerprint: list[str] = []
        # Owning agent, for surfacing the remote agent's own tool calls to the
        # UI. Weak on purpose: the agent holds this client as ``agent.client``,
        # so a strong ref back would make both immortal.
        self._agent_ref: "weakref.ref[Any] | None" = None
        _LIVE_CLIENTS.add(self)

    def bind_agent(self, agent: Any) -> None:
        """Attach the agent whose display callbacks tool activity routes to."""
        try:
            self._agent_ref = weakref.ref(agent)
        except TypeError:
            # Not weak-referenceable (test doubles, __slots__ objects). Tool
            # cards are display-only, so degrade rather than fail the turn.
            self._agent_ref = None

    def _agent(self) -> Any:
        ref = self._agent_ref
        return ref() if ref is not None else None

    def close(self) -> None:
        self._shutdown_process()
        self.is_closed = True

    def _shutdown_process(self) -> None:
        """Tear down the child process and forget the session bound to it."""
        proc: subprocess.Popen[str] | None
        with self._active_process_lock:
            proc = self._active_process
            self._active_process = None
        self._session_id = None
        self._session_model = None
        self._session_hermes_id = None
        self._sent_fingerprint = []
        self._inbox = None
        self._next_request_id = 0
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _session_is_live(self) -> bool:
        proc = self._active_process
        return (
            proc is not None
            and proc.poll() is None
            and proc.stdin is not None
            and self._inbox is not None
            and bool(self._session_id)
        )

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        tool_mode = _resolve_tool_mode(self._acp_command, self._acp_args)
        pairs = _iter_renderable(messages or [])
        # Normalise timeout: run_agent.py may pass an httpx.Timeout object
        # (used natively by the OpenAI SDK) rather than a plain float.
        if timeout is None:
            _effective_timeout = _DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, (int, float)):
            _effective_timeout = float(timeout)
        else:
            # httpx.Timeout or similar — pick the largest component so the
            # subprocess has enough wall-clock time for the full response.
            _candidates = [
                getattr(timeout, attr, None)
                for attr in ("read", "write", "connect", "pool", "timeout")
            ]
            _numeric = [float(v) for v in _candidates if isinstance(v, (int, float))]
            _effective_timeout = max(_numeric) if _numeric else _DEFAULT_TIMEOUT_SECONDS

        render = lambda slice_, preamble: _render_prompt(  # noqa: E731
            slice_,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            tool_mode=tool_mode,
            include_preamble=preamble,
        )

        def _run_turn(emit: Any = None) -> tuple[str, str]:
            if _persistent_sessions_enabled(tool_mode):
                return self._run_turn_persistent(
                    pairs,
                    render=render,
                    timeout_seconds=_effective_timeout,
                    model=model,
                    emit=emit,
                )
            return self._run_prompt(
                render(pairs, True),
                timeout_seconds=_effective_timeout,
                model=model,
                emit=emit,
            )

        if stream and tool_mode == "native":
            # Only native mode can stream incrementally. Bridge mode has to see
            # the whole reply before it can hand anything over, because
            # <tool_call> blocks are scraped out of the finished text and the
            # surrounding prose is only correct once they have been removed.
            return self._stream_native_turn(
                _run_turn, model=model, timeout_seconds=_effective_timeout
            )

        response_text, reasoning_text = _run_turn()

        if tool_mode == "native":
            # Nothing instructed the agent to emit <tool_call> blocks, so any
            # that appear are echoes of the transcript, not real intent.
            # Executing them would run tools the agent never asked for.
            tool_calls, cleaned_text = [], response_text.strip()
        else:
            tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)

        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        completion = SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or "copilot-acp",
        )
        if stream:
            return _completion_to_stream_chunks(completion)
        return completion

    def _stream_native_turn(
        self,
        run_turn: Any,
        *,
        model: str | None,
        timeout_seconds: float,
    ) -> Any:
        """Run one native-mode turn on a worker thread, yielding chunks live.

        The one-shot path packs a whole turn into a single delta, so the
        display receives reasoning and reply text in the same flush and orders
        them by its own fixed rule rather than by arrival -- which is why the
        Thought pane stayed empty even though the reasoning text was there.
        Emitting each ``agent_thought_chunk`` as its own delta, at the moment it
        arrives and therefore before any reply text exists, is what puts
        reasoning in an earlier flush. Reply text streaming live rather than
        landing as one block at the end falls out of the same change.

        The turn has to run on a worker because the ACP read loop is blocking:
        the generator drains what the worker publishes.
        """
        model_name = model or "copilot-acp"
        outbox: queue.Queue[tuple[str, str] | None] = queue.Queue()
        state: dict[str, Any] = {}

        def emit(kind: str, text: str) -> None:
            if text:
                outbox.put((kind, text))

        # Approval routing (gateway notify callback, session key) lives in
        # contextvars. A bare Thread starts with an EMPTY context, so every
        # session/request_permission resolved to the wrong session key, found
        # no notify callback registered under it, and was denied instantly
        # without an approval card ever rendering.
        ctx = contextvars.copy_context()

        def worker() -> None:
            try:
                state["result"] = ctx.run(run_turn, emit)
            except BaseException as exc:  # surfaced to the consumer below
                state["error"] = exc
            finally:
                outbox.put(None)

        thread = threading.Thread(
            target=worker, name="copilot-acp-stream", daemon=True
        )
        thread.start()

        first_content = True
        # The worker enforces the real budget inside _rpc; this only guards
        # against the sentinel never arriving at all.
        deadline = time.monotonic() + timeout_seconds + 30.0
        while True:
            try:
                item = outbox.get(timeout=0.25)
            except queue.Empty:
                if not thread.is_alive():
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "Timed out waiting for the Copilot ACP stream to finish."
                    )
                continue
            if item is None:
                break
            kind, text = item
            if kind == "content":
                if first_content:
                    # The one-shot path returns response_text.strip(); match
                    # its leading edge so both paths build the same message.
                    text = text.lstrip()
                    if not text:
                        continue
                    first_content = False
                yield _acp_stream_chunk(model_name, content=text)
            else:
                yield _acp_stream_chunk(model_name, reasoning=text)

        error = state.get("error")
        if error is not None:
            raise error

        # A stream that ends without a finish_reason is treated downstream as a
        # truncated response, so send one even when the turn produced no text.
        yield _acp_stream_chunk(model_name, finish_reason="stop")
        yield _acp_usage_chunk(model_name)

    def _spawn_process(self) -> subprocess.Popen[str]:
        """Start the ACP child and attach its stdout/stderr reader threads."""
        # Fast-fail when the CLI doesn't support the ACP args we'd pass.
        # Without this guard, a CLI like Claude Code v2.x exits with
        # ``error: unknown option '--acp'`` immediately, then the parent
        # ACP loop waits the full ``child_timeout_seconds`` (default 600s)
        # for stdout that never arrives. The probe costs ~50ms and turns
        # a 600s silent hang into a 280ms clear error.
        # ``None`` (inconclusive probe — e.g. binary missing) falls
        # through to the spawn below, which raises the established
        # "Could not start Copilot ACP command" error.
        # Upstream guarded ``_run_prompt``; that was its only spawn point.
        # Ours spawns here and holds the session across turns, so the probe
        # belongs here — in _run_prompt it would re-run every turn.
        if _acp_supported(self._acp_command, self._acp_args) is False:
            preview = " ".join(self._acp_args[:3]) if self._acp_args else "(none)"
            raise RuntimeError(
                f"ACP transport not supported by '{self._acp_command}': "
                f"`{preview}` is rejected as an unknown option. "
                f"This usually means the CLI is an older release (e.g. "
                f"Claude Code v2.x) or a different tool than expected. "
                f"Either install a CLI that ships with --acp support "
                f"(e.g. `@github/copilot` late 2025+), or set "
                f"HERMES_COPILOT_ACP_COMMAND / HERMES_COPILOT_ACP_ARGS "
                f"to a working pair."
            )

        try:
            # Hide the console the CLI child would otherwise flash on Windows
            # (#56747). Hide-only — stdio pipes stay intact for the ACP wire.
            from hermes_cli._subprocess_compat import windows_hide_flags

            proc = subprocess.Popen(
                [self._acp_command] + self._acp_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace',
                bufsize=1,
                cwd=self._acp_cwd,
                env=_build_subprocess_env(),
                creationflags=windows_hide_flags(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start Copilot ACP command '{self._acp_command}'. "
                "Install GitHub Copilot CLI or set HERMES_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH."
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("Copilot ACP process did not expose stdin/stdout pipes.")

        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=40)

        def _stdout_reader() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                try:
                    inbox.put(json.loads(line))
                except Exception:
                    inbox.put({"raw": line.rstrip("\n")})

        def _stderr_reader() -> None:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                stderr_tail.append(line.rstrip("\n"))

        out_thread = threading.Thread(target=_stdout_reader, daemon=True)
        err_thread = threading.Thread(target=_stderr_reader, daemon=True)
        out_thread.start()
        err_thread.start()

        # Publish the process only once its readers exist, so no other thread
        # can observe a "live" client whose inbox is still unset.
        self._inbox = inbox
        self._stderr_tail = stderr_tail
        self._next_request_id = 0
        self.is_closed = False
        with self._active_process_lock:
            self._active_process = proc
        return proc

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        text_parts: list[str] | None = None,
        reasoning_parts: list[str] | None = None,
        tool_lines: dict[str, dict[str, Any]] | None = None,
        emit: Any = None,
    ) -> Any:
        proc = self._active_process
        inbox = self._inbox
        if proc is None or proc.stdin is None or inbox is None:
            raise RuntimeError("Copilot ACP process is not running.")
        stderr_tail = self._stderr_tail

        self._next_request_id += 1
        request_id = self._next_request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                msg = inbox.get(timeout=0.1)
            except queue.Empty:
                continue

            if self._handle_server_message(
                msg,
                process=proc,
                cwd=self._acp_cwd,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                tool_lines=tool_lines,
                emit=emit,
            ):
                continue

            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                err = msg.get("error") or {}
                raise RuntimeError(
                    f"Copilot ACP {method} failed: {err.get('message') or err}"
                )
            return msg.get("result")

        stderr_text = "\n".join(stderr_tail).strip()
        if proc.poll() is not None and stderr_text:
            if _is_gh_copilot_deprecation_message(stderr_text):
                raise RuntimeError(
                    "Hermes ACP mode requires the NEW GitHub Copilot CLI "
                    "(github.com/github/copilot-cli), but the binary it just "
                    "spawned is the deprecated `gh copilot` extension.\n\n"
                    "Install the new CLI:\n"
                    "  npm install -g @github/copilot\n"
                    "  # then verify with: copilot --help\n\n"
                    "If `copilot` already resolves to the new CLI but you still see this,\n"
                    "point Hermes at it explicitly:\n"
                    "  export HERMES_COPILOT_ACP_COMMAND=/path/to/new/copilot\n\n"
                    "Alternative: use the `copilot` provider (no ACP, hits the Copilot API\n"
                    "directly with a Copilot subscription token) via `hermes setup`.\n\n"
                    f"Original error:\n{stderr_text}"
                )
            raise RuntimeError(f"Copilot ACP process exited early: {stderr_text}")
        raise TimeoutError(f"Timed out waiting for Copilot ACP response to {method}.")

    def _acp_thinking_option(self, model: str | None) -> dict[str, Any] | None:
        """Thinking config to pass through ``_meta.claudeCode.options``.

        claude-agent-acp only sets ``thinking`` when ``MAX_THINKING_TOKENS`` is
        present in its environment, so normally the option is left unset and the
        API applies its own default of ``display: "omitted"``. That default
        streams signature-only thinking blocks whose text is empty, and the
        agent drops those instead of emitting ``agent_thought_chunk`` -- which
        is why the Thought pane stays blank. Asking for ``summarized`` is what
        puts real reasoning text on the wire.

        This is the supported override point rather than a patch to the agent:
        it builds its options as ``...(thinking !== undefined && { thinking })``
        followed by ``...userProvidedOptions``, so a caller-supplied value wins
        over the env-derived one.
        """
        if _resolve_tool_mode(self._acp_command, self._acp_args) != "native":
            return None
        raw = (os.environ.get("HERMES_ACP_THINKING_DISPLAY") or "").strip().lower()
        if raw in {"off", "none", "0", "false"}:
            return None
        # Adaptive thinking is Opus/Sonnet 4.6+. Haiku still takes a fixed token
        # budget and rejects the adaptive shape, and a bad thinking config fails
        # the whole turn -- so leave those on the agent's default.
        if "haiku" in (model or "").lower():
            return None
        display = raw if raw in {"summarized", "omitted"} else "summarized"
        return {"type": "adaptive", "display": display}

    def _hermes_tools_mcp_servers(self) -> list[dict[str, Any]]:
        """ACP ``mcpServers`` entries exposing Hermes' own tool surface.

        A native agent builds its own tool list, so without this Hermes'
        richer tools (web search, browser automation, vision, image
        generation, skills, persistent memory, cross-session recall) are
        unreachable from inside the turn. Same approach the codex app-server
        runtime takes; see agent/transports/hermes_tools_mcp_server.py.

        Bridge mode is skipped deliberately: there Hermes already injects its
        catalog into the prompt and executes the calls itself, so a second
        copy over MCP would just be duplicate tools with different names.

        Opt out with HERMES_ACP_HERMES_TOOLS=off.
        """
        if _resolve_tool_mode(self._acp_command, self._acp_args) != "native":
            return []
        raw = (os.environ.get("HERMES_ACP_HERMES_TOOLS") or "").strip().lower()
        if raw in {"off", "none", "0", "false"}:
            return []

        # The launcher (~/.hermes-acp wrapper, systemd unit, gateway) may run
        # the ACP child under a scrubbed allowlist environment, so nothing can
        # be assumed to be inherited -- every variable the server needs is
        # passed explicitly.
        hermes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env: list[dict[str, str]] = [
            # `-m agent.transports...` resolves against PYTHONPATH, not cwd:
            # cwd is the user's working directory, not the Hermes source root.
            {"name": "PYTHONPATH", "value": hermes_root},
            # stdio IS the MCP wire. Without this, Windows defaults to cp1252
            # and any non-ASCII tool result raises mid-protocol.
            {"name": "PYTHONIOENCODING", "value": "utf-8"},
        ]
        for var in ("HERMES_HOME", "HERMES_PROFILE", "HERMES_SESSION_ID"):
            value = os.environ.get(var)
            if value:
                env.append({"name": var, "value": value})

        # No "type" key: acp-agent.js treats an entry as stdio only when the
        # field is absent (`else if (!("type" in server))`).
        return [
            {
                "name": "hermes-tools",
                "command": sys.executable,
                "args": ["-m", "agent.transports.hermes_tools_mcp_server"],
                "env": env,
            }
        ]

    def _ensure_session(self, *, model: str | None, timeout_seconds: float) -> str:
        """Return a live ACP session id, opening one if needed.

        Reuses the current session when the child is still alive and the
        requested model has not changed; a model switch needs a new session
        because ``session/set_config_option`` is negotiated at session setup.
        """
        if (
            self._session_is_live()
            and self._session_model == (model or "")
            and self._session_hermes_id == _current_hermes_session_id()
        ):
            return str(self._session_id)

        self._shutdown_process()
        self._spawn_process()

        self._rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {
                        "readTextFile": True,
                        "writeTextFile": True,
                    }
                },
                "clientInfo": {
                    "name": "hermes-agent",
                    "title": "Hermes Agent",
                    "version": "0.0.0",
                },
            },
            timeout_seconds=timeout_seconds,
        )
        session_params: dict[str, Any] = {
            "cwd": self._acp_cwd,
            "mcpServers": self._hermes_tools_mcp_servers(),
        }
        claude_options: dict[str, Any] = {}
        thinking = self._acp_thinking_option(model)
        if thinking is not None:
            claude_options["thinking"] = thinking
        # Model is negotiated twice, deliberately. session/set_config_option
        # (below) only accepts values the agent advertises in configOptions --
        # aliases like "opus", never a raw API id: probing
        # "claude-opus-4-8" there returns "Invalid value for config option
        # model". The _meta option goes straight to the SDK's own `model`
        # field, which takes aliases AND raw ids (a bogus id fails the turn
        # with errorKind "model_not_found", so the value is genuinely reaching
        # the API rather than being ignored). Sending both keeps agents that
        # don't read _meta working off the config route.
        requested_model = (model or "").strip()
        if requested_model and _resolve_tool_mode(
            self._acp_command, self._acp_args
        ) == "native":
            claude_options["model"] = requested_model
        if claude_options:
            session_params["_meta"] = {"claudeCode": {"options": claude_options}}
        session = self._rpc(
            "session/new",
            session_params,
            timeout_seconds=timeout_seconds,
        ) or {}
        session_id = str(session.get("sessionId") or "").strip()
        if not session_id:
            raise RuntimeError("Copilot ACP did not return a sessionId.")

        # Negotiate the model over ACP rather than only hinting at it in the
        # prompt text. The agent advertises what it accepts in
        # session/new -> configOptions[id="model"].options[].value; we only
        # send session/set_config_option when the requested model is one of
        # them, so agents that don't support model selection (or don't know
        # this config id) are left untouched.
        requested_model = (model or "").strip()
        if requested_model:
            _select_acp_model(
                lambda method, params: self._rpc(
                    method, params, timeout_seconds=timeout_seconds
                ),
                session,
                session_id,
                requested_model,
            )

        # Permission mode, same negotiate-don't-assume shape as the model.
        # Unset by default so the approval gate keeps firing unless the
        # operator explicitly opts into a wider mode.
        requested_mode = _requested_acp_mode()
        if requested_mode:
            _select_acp_mode(
                lambda method, params: self._rpc(
                    method, params, timeout_seconds=timeout_seconds
                ),
                session,
                session_id,
                requested_mode,
            )

        self._session_id = session_id
        self._session_model = model or ""
        self._session_hermes_id = _current_hermes_session_id()
        self._sent_fingerprint = []
        return session_id

    def _prompt_session(
        self,
        prompt_text: str,
        *,
        session_id: str,
        timeout_seconds: float,
        emit: Any = None,
    ) -> tuple[str, str]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # toolCallId -> {"index": slot in reasoning_parts, "fields": merged
        # ToolCall}. Scoped to this turn: ids are only unique within a prompt,
        # and the slot indices are meaningless against a different list.
        tool_lines: dict[str, dict[str, Any]] = {}
        self._rpc(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [
                    {
                        "type": "text",
                        "text": prompt_text,
                    }
                ],
            },
            timeout_seconds=timeout_seconds,
            text_parts=text_parts,
            reasoning_parts=reasoning_parts,
            tool_lines=tool_lines,
            emit=emit,
        )
        return "".join(text_parts), "".join(reasoning_parts)

    def _run_prompt(
        self,
        prompt_text: str,
        *,
        timeout_seconds: float,
        model: str | None = None,
        emit: Any = None,
    ) -> tuple[str, str]:
        """Ephemeral path: one child process and one session per prompt."""
        with self._session_lock:
            try:
                session_id = self._ensure_session(
                    model=model, timeout_seconds=timeout_seconds
                )
                return self._prompt_session(
                    prompt_text,
                    session_id=session_id,
                    timeout_seconds=timeout_seconds,
                    emit=emit,
                )
            finally:
                self.close()

    def _run_turn_persistent(
        self,
        pairs: list[tuple[str, str]],
        *,
        render: Any,
        timeout_seconds: float,
        model: str | None = None,
        emit: Any = None,
    ) -> tuple[str, str]:
        """Persistent path: keep one session across turns and send only the
        messages it has not seen yet.

        Falls back to a fresh session (and the full transcript) whenever the
        caller's history is not an append-only extension of what was already
        sent -- a compaction, edit, or branch rewrites history, and replaying a
        delta against the wrong prefix would silently desynchronise the agent.
        """
        with self._session_lock:
            fingerprint = _fingerprint_pairs(pairs)
            sent = self._sent_fingerprint
            # Captured separately from `reuse` so a rebuild can report which
            # precondition actually failed; `_session_is_live()` in particular
            # is only meaningful before `_shutdown_process()` runs below.
            live = self._session_is_live()
            model_match = self._session_model == (model or "")
            hermes_match = self._session_hermes_id == _current_hermes_session_id()
            append_only = (
                len(fingerprint) > len(sent) and fingerprint[: len(sent)] == sent
            )
            reuse = live and model_match and hermes_match and append_only

            new_pairs = pairs
            if reuse:
                new_pairs = list(pairs[len(sent):])
                # The agent already knows what it said; echoing its own reply
                # back at it reads as a user turn.
                while new_pairs and new_pairs[0][0] == "assistant":
                    new_pairs.pop(0)
                if not new_pairs:
                    reuse = False
                    new_pairs = pairs

            if reuse:
                logger.info(
                    "ACP session REUSED: sending %d new message(s) "
                    "(already sent %d of %d)",
                    len(new_pairs),
                    len(sent),
                    len(fingerprint),
                )
            else:
                if not live:
                    reason = "no live session"
                elif not model_match:
                    reason = (
                        f"model changed ({self._session_model!r} -> "
                        f"{(model or '')!r})"
                    )
                elif not hermes_match:
                    reason = (
                        f"hermes session changed ({self._session_hermes_id!r} -> "
                        f"{_current_hermes_session_id()!r})"
                    )
                elif not append_only:
                    reason = "history not append-only (compaction/edit/branch)"
                else:
                    reason = "delta contained no user messages"
                logger.info(
                    "ACP session REBUILT (%s): sending full transcript, "
                    "%d message(s)",
                    reason,
                    len(new_pairs),
                )
                self._shutdown_process()

            try:
                session_id = self._ensure_session(
                    model=model, timeout_seconds=timeout_seconds
                )
                result = self._prompt_session(
                    render(new_pairs, not reuse),
                    session_id=session_id,
                    timeout_seconds=timeout_seconds,
                    emit=emit,
                )
            except Exception:
                # A half-consumed session cannot be resynchronised; drop it so
                # the next turn rebuilds from the full transcript.
                self._shutdown_process()
                raise

            self._sent_fingerprint = fingerprint
            return result

    def _emit_tool_lifecycle(
        self, call_id: str, tool_lines: dict[str, dict[str, Any]]
    ) -> None:
        """Surface one native-agent tool call as a real Hermes tool card.

        A native-tool agent executes its own tools, so Hermes never sees them as
        OpenAI ``tool_calls`` and no card is drawn from the normal path. The
        gateway's structured tool feed is the same one the codex bridge uses
        (``tool_start_callback``/``tool_complete_callback``), so emitting into it
        gets the desktop's real tool cards rather than text buried in the
        reasoning block.

        The start emit is deferred until the call's arguments are known.
        ``tool_call`` fires before the agent has finished streaming its input,
        so its ``rawInput`` is ``{}`` and the card label -- which the gateway
        builds once, at start, and never revises on completion -- would be a
        bare tool name with no target. The settled input lands moments later on
        a ``tool_call_update``. A call that reaches a terminal status without
        ever carrying input still emits, so nothing is silently swallowed.

        Every callback is guarded: this is display-only, and a broken UI hook
        must not abort the agent's turn.
        """
        entry = tool_lines.get(call_id)
        if entry is None:
            return
        agent = self._agent()
        if agent is None:
            return
        fields = entry.get("fields") or {}
        status = str(fields.get("status") or "").strip().lower()
        terminal = status in _TERMINAL_TOOL_STATUSES
        raw_input = fields.get("rawInput")
        has_args = isinstance(raw_input, dict) and bool(raw_input)

        if not entry.get("lifecycle_started"):
            if not (has_args or terminal):
                return
            name, args = _acp_tool_display(fields)
            entry["lifecycle_started"] = True
            entry["lifecycle_display"] = (name, args)
            entry["lifecycle_started_at"] = time.monotonic()
            start_cb = getattr(agent, "tool_start_callback", None)
            if start_cb is not None:
                try:
                    start_cb(call_id, name, args)
                except Exception:
                    logger.debug(
                        "tool_start_callback raised for %s", name, exc_info=True
                    )
            progress_cb = getattr(agent, "tool_progress_callback", None)
            if progress_cb is not None:
                preview = ""
                try:
                    from agent.display import build_tool_preview

                    preview = build_tool_preview(name, args) or ""
                except Exception:
                    preview = ""
                try:
                    progress_cb("tool.started", name, preview, args)
                except Exception:
                    logger.debug(
                        "tool_progress_callback raised on tool.started for %s",
                        name, exc_info=True,
                    )

        if not terminal or entry.get("lifecycle_completed"):
            return
        entry["lifecycle_completed"] = True
        name, args = entry.get("lifecycle_display") or _acp_tool_display(fields)
        result = _acp_tool_result_text(fields)
        is_error = status in {"failed", "error"}
        started_at = entry.get("lifecycle_started_at")
        duration = time.monotonic() - started_at if started_at else None
        complete_cb = getattr(agent, "tool_complete_callback", None)
        if complete_cb is not None:
            try:
                complete_cb(call_id, name, args, result)
            except Exception:
                logger.debug(
                    "tool_complete_callback raised for %s", name, exc_info=True
                )
        progress_cb = getattr(agent, "tool_progress_callback", None)
        if progress_cb is not None:
            try:
                progress_cb(
                    "tool.completed", name, None, None,
                    duration=duration, is_error=is_error, result=result,
                )
            except Exception:
                logger.debug(
                    "tool_progress_callback raised on tool.completed for %s",
                    name, exc_info=True,
                )

    def _handle_server_message(
        self,
        msg: dict[str, Any],
        *,
        process: subprocess.Popen[str],
        cwd: str,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
        tool_lines: dict[str, dict[str, Any]] | None = None,
        emit: Any = None,
    ) -> bool:
        method = msg.get("method")
        if not isinstance(method, str):
            return False

        if method == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            kind = str(update.get("sessionUpdate") or "").strip()
            content = update.get("content") or {}
            chunk_text = ""
            if isinstance(content, dict):
                chunk_text = str(content.get("text") or "")
            if kind == "agent_message_chunk" and chunk_text and text_parts is not None:
                text_parts.append(chunk_text)
                if emit is not None:
                    emit("content", chunk_text)
            elif kind == "agent_thought_chunk" and chunk_text and reasoning_parts is not None:
                reasoning_parts.append(chunk_text)
                if emit is not None:
                    emit("reasoning", chunk_text)
            elif kind in {"tool_call", "tool_call_update"} and reasoning_parts is not None:
                # A native-tool agent runs its own tools and only reports them
                # here. Hermes never sees these as OpenAI tool_calls, so
                # without this the whole middle of a turn is invisible: the
                # user watches an agent that reads, edits and runs commands
                # appear to sit idle between prose chunks.
                #
                # `tool_call` announces one call before its arguments have
                # finished streaming, so on its own it renders a bare tool name
                # with no target. The useful detail arrives afterwards as
                # `tool_call_update` refinements carrying the settled rawInput,
                # title and status. Rather than appending each of those as a
                # new line (noise) or discarding them (the old behaviour, which
                # kept the useless stub and threw away the informative
                # refinement), rewrite the call's existing line in place.
                call_id = str(update.get("toolCallId") or "").strip()
                entry = tool_lines.get(call_id) if (tool_lines is not None and call_id) else None
                if entry is not None:
                    fields = _merge_tool_call_fields(entry["fields"], update)
                    entry["fields"] = fields
                    reasoning_parts[entry["index"]] = f"\n{_format_tool_activity(fields)}\n"
                else:
                    fields = _merge_tool_call_fields({}, update)
                    status = str(fields.get("status") or "").strip().lower()
                    # An update for a call we never opened a line for (e.g. the
                    # `tool_call` predates a compaction) is only worth a line if
                    # it is reporting a terminal failure.
                    if kind == "tool_call" or status in {"failed", "error"}:
                        # reasoning_parts is joined with no separator (thought
                        # chunks arrive pre-split), so carry our own line breaks.
                        line = f"\n{_format_tool_activity(fields)}\n"
                        if call_id and tool_lines is not None:
                            reasoning_parts.append(line)
                            tool_lines[call_id] = {
                                "index": len(reasoning_parts) - 1,
                                "fields": fields,
                            }
                        elif not reasoning_parts or reasoning_parts[-1] != line:
                            # No id to refine against later, so fall back to the
                            # old consecutive-duplicate guard.
                            reasoning_parts.append(line)
                # The reasoning line above is the fallback for surfaces with no
                # tool-card UI; this drives the real cards. Both read the same
                # merged fields, so they can't disagree about what ran.
                if call_id and tool_lines is not None:
                    self._emit_tool_lifecycle(call_id, tool_lines)
                    if emit is not None:
                        _stream_tool_line(call_id, tool_lines, emit)
            return True

        if process.stdin is None:
            return True

        message_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "session/request_permission":
            try:
                tool_call = params.get("toolCall") or {}
                options = params.get("options") or []
                command, description = _describe_permission_request(tool_call)
                logger.info(
                    "ACP permission requested: %s (target: %.200s)",
                    description, command,
                )
                # Route through the SAME gates Hermes' own tools use, rather
                # than calling _run_approval_gate directly. Going straight to
                # the gate meant every ACP call drew a card (a plain `ls` or a
                # skill read included), the hardline floor never ran, and the
                # pattern key hashed the full command string so answering
                # [a]lways never matched the next, slightly different call.
                from tools.approval import (
                    check_dangerous_command,
                    request_tool_approval,
                )

                tool_name = _acp_tool_name(tool_call)
                shell_command = (
                    _acp_shell_command(tool_call) if tool_name == "Bash" else ""
                )

                if shell_command:
                    # Real shell text: the dangerous-pattern matcher applies,
                    # including the unconditional hardline floor for commands
                    # with no recovery path. Safe reads auto-approve, so
                    # routine calls stop prompting.
                    result = check_dangerous_command(
                        shell_command,
                        env_type="local",
                    )
                else:
                    # Non-shell tools (Edit, Write, WebFetch, MCP calls) carry
                    # no command to pattern-match. Gate them per tool with a
                    # stable rule_key so one [a]lways covers that tool instead
                    # of only the exact call that was approved.
                    gate_name = tool_name or (
                        str(tool_call.get("kind") or "").strip() or "tool"
                    )
                    result = request_tool_approval(
                        gate_name,
                        description,
                        rule_key=f"copilot-acp:{gate_name}",
                    )
                option_id = (
                    _select_permission_option(options)
                    if result.get("approved")
                    else None
                )
                if option_id:
                    logger.info(
                        "ACP permission APPROVED: %s (optionId: %s)",
                        description, option_id,
                    )
                    response = {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": option_id,
                            }
                        },
                    }
                else:
                    # An approved request with no usable option is a protocol
                    # mismatch, not a user denial — log the two apart so a
                    # missing allow_once/allow_always kind is diagnosable.
                    if result.get("approved"):
                        logger.warning(
                            "ACP permission approved but no allow_once/"
                            "allow_always option offered (options: %s) — "
                            "denying: %s",
                            [o.get("kind") for o in options
                             if isinstance(o, dict)],
                            description,
                        )
                    else:
                        logger.info(
                            "ACP permission DENIED: %s (%s)",
                            description, result.get("message") or "user denied",
                        )
                    response = _permission_denied(message_id)
            except Exception:
                logger.exception("session/request_permission handling failed")
                response = _permission_denied(message_id)
        elif method == "fs/read_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                block_error = get_read_block_error(str(path))
                if block_error:
                    raise PermissionError(block_error)
                try:
                    content = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    content = ""
                line = params.get("line")
                limit = params.get("limit")
                if isinstance(line, int) and line > 1:
                    lines = content.splitlines(keepends=True)
                    start = line - 1
                    end = start + limit if isinstance(limit, int) and limit > 0 else None
                    content = "".join(lines[start:end])
                if content:
                    content = redact_sensitive_text(content, force=True)
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": content,
                    },
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        elif method == "fs/write_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                denied = get_write_denied_error(str(path))
                if denied:
                    raise PermissionError(denied)
                # Approval-gated paths (e.g. ~/.ssh/config) are not hard-denied
                # for interactive tools, but the ACP shim has no human channel
                # to confirm the write — fail closed here.
                if is_write_approval_required(str(path)):
                    raise PermissionError(
                        f"Write denied: '{path}' requires interactive approval "
                        "and cannot be written through the ACP file bridge."
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content") or ""), encoding="utf-8")
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": None,
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        else:
            response = _jsonrpc_error(
                message_id,
                -32601,
                f"ACP client method '{method}' is not supported by Hermes yet.",
            )

        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True
