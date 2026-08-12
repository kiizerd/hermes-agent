"""Hermes-tools-as-MCP server for the codex_app_server runtime.

When the user runs `openai/*` turns through the codex app-server, codex
owns the loop and builds its own tool list. By default, that means
Hermes' richer tool surface — web search, browser automation,
delegate_task subagents, vision analysis, persistent memory, skills,
cross-session search, image generation, TTS — is unreachable.

This module exposes a curated subset of those Hermes tools to the
spawned codex subprocess via stdio MCP. Codex registers it as a normal
MCP server (per `~/.codex/config.toml [mcp_servers.hermes-tools]`) and
the user gets full Hermes capability inside a Codex turn.

Scope (what we expose):
  - web_search, web_extract              — Firecrawl, no codex equivalent
  - browser_navigate / _click / _type /  — Camofox/Browserbase automation
    _snapshot / _scroll / _back / _press /
    _get_images / _console / _vision
  - vision_analyze                       — image inspection by vision model
  - image_generate                       — image generation
  - skill_view, skills_list,             — Hermes' skill library (read +
    skill_manage                           write: the background skill-review
                                           fork reaches Hermes only through
                                           this bridge, so without the write
                                           tool it cannot save skills)
  - text_to_speech                       — TTS
  - memory, session_search               — persistent memory + cross-session
                                           recall. Dispatched through
                                           _AGENT_LOOP_DISPATCH below, not
                                           handle_function_call.
  - kanban_* (complete/block/comment/    — kanban worker + orchestrator
    heartbeat/show/list/create/            handoff (stateless: read env var,
    unblock/link)                          write ~/.hermes/kanban.db)

What we DO NOT expose:
  - terminal / shell                     — codex's own shell tool
  - read_file / write_file / patch       — codex's apply_patch + shell
  - search_files / process               — codex's shell
  - clarify                              — codex's own UX
  - delegate_task / todo                 — `_AGENT_LOOP_TOOLS` in Hermes
                                           (model_tools.py) whose backing
                                           state genuinely lives on the
                                           agent: `agent._todo_store`, and
                                           subagent spawning off the live
                                           loop. A stateless callback can't
                                           supply either.

`memory` and `session_search` are also `_AGENT_LOOP_TOOLS`, but only their
*dispatch* needs the agent — the state behind them is on disk.
`MemoryStore(...).load_from_disk()` and `SessionDB()` are both constructible
standalone (run_agent.py:600 opens a SessionDB exactly this way when a
frontend forgot to pass one), so this server builds them per call rather
than reaching for an agent. Constructing per call is also what keeps writes
coherent: nothing here holds a long-lived in-memory copy that a concurrent
Hermes session could make stale.

Run with: python -m agent.transports.hermes_tools_mcp_server
Spawned by: CodexAppServerSession.ensure_started() when the runtime is
            active and config opts in.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# JSON Schema type -> Python type mapping for signature generation
_JSON_TO_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _signature_from_schema(schema: dict | None) -> tuple[inspect.Signature, dict[str, type]]:
    """Build a Python function signature and annotations from a JSON schema.

    Args:
        schema: JSON Schema dict with "properties" and "required" keys.

    Returns:
        (signature, annotations_dict) where signature has KEYWORD_ONLY params
        and annotations maps param names to Python types.
    """
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    params, annots = [], {}

    for pname, pspec in props.items():
        if pname.startswith("_"):
            continue
        py = _JSON_TO_PY.get((pspec or {}).get("type"), Any)
        ann, default = (
            (py, inspect.Parameter.empty)
            if pname in required
            else (Optional[py], None)
        )
        annots[pname] = ann
        params.append(
            inspect.Parameter(
                pname, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default
            )
        )

    return inspect.Signature(params, return_annotation=str), annots


# Tools we expose. Each name MUST match a registered Hermes tool that
# `model_tools.handle_function_call()` can dispatch.
#
# What we deliberately DO NOT expose:
#   - terminal / shell / read_file / write_file / patch / search_files /
#     process — codex's built-ins cover these and approval routes through
#     codex's own UI.
#   - delegate_task / memory / session_search / todo — these are
#     `_AGENT_LOOP_TOOLS` in Hermes (model_tools.py:493). They require
#     the running AIAgent context to dispatch (mid-loop state), so a
#     stateless MCP callback can't drive them. Hermes' default runtime
#     keeps these working; the codex_app_server runtime cannot.
EXPOSED_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_snapshot",
    "browser_scroll",
    "browser_back",
    "browser_get_images",
    "browser_console",
    "browser_vision",
    "vision_analyze",
    "image_generate",
    "skill_view",
    "skills_list",
    # skill_manage: turn_finalizer.py gates background skill review on
    # "skill_manage" in agent.valid_tool_names, and under a native ACP
    # runtime the review fork reaches Hermes tools only through this
    # server — memory review could write, skill review had no tool.
    # Pure-kwargs handler (tools/skill_manager_tool.py), not in
    # _AGENT_LOOP_TOOLS, so plain handle_function_call dispatch works.
    "skill_manage",
    "text_to_speech",
    # Agent-loop tools with disk-backed state. handle_function_call refuses
    # these by name (model_tools.py `_AGENT_LOOP_TOOLS`), so they dispatch
    # through _AGENT_LOOP_DISPATCH instead. Registration is still gated by
    # get_tool_definitions() below: when the memory toolset is disabled in
    # config, `memory` never appears in the schema list and is skipped.
    "memory",
    "session_search",
    # Kanban worker handoff tools — gated on HERMES_KANBAN_TASK env var
    # (set by the kanban dispatcher when spawning a worker). Without these
    # in the callback, a worker spawned with openai_runtime=codex_app_server
    # could do the work but couldn't report completion back to the kernel,
    # making it hang until timeout. Stateless dispatch — they just read
    # the env var and write to ~/.hermes/kanban.db.
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_show",
    "kanban_list",
    # NOTE: kanban_create / kanban_unblock / kanban_link are orchestrator-
    # only — the kanban tool gates them on HERMES_KANBAN_TASK being unset.
    # They're exposed here for orchestrator agents running on the codex
    # runtime that need to dispatch new tasks.
    "kanban_create",
    "kanban_unblock",
    "kanban_link",
)


def _dispatch_memory(args: dict[str, Any]) -> str:
    """Run the memory tool against a freshly loaded disk store.

    The store is built per call on purpose. A long-lived instance here would
    drift from disk whenever another Hermes session wrote memory, and the
    write path (MemoryStore.save_to_disk, called by the tool itself) would
    then persist a stale snapshot over it.
    """
    from tools.memory_tool import MemoryStore, memory_tool

    mem_cfg: dict = {}
    try:
        from hermes_cli.config import load_config_readonly

        section = (load_config_readonly() or {}).get("memory")
        if isinstance(section, dict):
            mem_cfg = section
    except Exception:
        logger.debug("memory: config load failed, using defaults", exc_info=True)

    store = MemoryStore(
        memory_char_limit=mem_cfg.get("memory_char_limit", 2200),
        user_char_limit=mem_cfg.get("user_char_limit", 1375),
    )
    store.load_from_disk()
    return memory_tool(
        action=args.get("action"),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
        store=store,
    )


def _dispatch_session_search(args: dict[str, Any]) -> str:
    """Run cross-session recall against a read-only view of the state DB.

    ``read_only=True`` matters: search never writes, and a writable handle
    would contend with the live Hermes process for the same SQLite file --
    including the TRUNCATE-WAL that SessionDB.close() performs on writable
    connections.

    ``current_session_id`` comes from HERMES_SESSION_ID, which the parent
    Hermes process exports at agent init (agent_init.py:1540) and the
    caller forwards into this subprocess's environment. Absent it, recall
    still works; only "which of these is the current session" degrades.
    """
    from hermes_state import SessionDB
    from tools.session_search_tool import session_search

    db = SessionDB(read_only=True)
    try:
        return session_search(
            query=args.get("query", ""),
            role_filter=args.get("role_filter"),
            limit=args.get("limit", 3),
            session_id=args.get("session_id"),
            around_message_id=args.get("around_message_id"),
            window=args.get("window", 5),
            sort=args.get("sort"),
            profile=args.get("profile"),
            db=db,
            current_session_id=os.environ.get("HERMES_SESSION_ID") or None,
        )
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("session_search: SessionDB.close() failed", exc_info=True)


# Tools that model_tools.handle_function_call refuses by name because they
# are agent-loop tools, but whose state is reachable without a live agent.
_AGENT_LOOP_DISPATCH = {
    "memory": _dispatch_memory,
    "session_search": _dispatch_session_search,
}


def _build_server() -> Any:
    """Create the MCP server with Hermes tools attached. Lazy imports
    so the module can be imported without the mcp package installed
    (we degrade to a clear error only when actually run)."""
    try:
        # mcp 2.0 removed `mcp.server.fastmcp`; `mcp.server.MCPServer` is the
        # same decorator/add_tool surface under the new name.
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - install hint
        raise ImportError(
            f"hermes-tools MCP server requires the 'mcp' package: {exc}"
        ) from exc

    # Discover Hermes tools so dispatch works.
    from model_tools import (
        get_tool_definitions,
        handle_function_call,
    )

    mcp = MCPServer(
        "hermes-tools",
        instructions=(
            "Hermes Agent's tool surface, exposed for use inside a native "
            "agent session (Codex, Claude via ACP). Use these for "
            "capabilities the host agent's built-in toolset doesn't cover: "
            "web search/extract, browser automation, "
            "subagent delegation, vision, image generation, persistent "
            "memory, skills, and cross-session search."
        ),
    )

    # Pull authoritative Hermes tool schemas for the ones we expose, so
    # MCP clients see the same parameter docs Hermes gives the model.
    all_defs = {
        td["function"]["name"]: td["function"]
        for td in (get_tool_definitions(quiet_mode=True) or [])
        if isinstance(td, dict) and td.get("type") == "function"
    }

    exposed_count = 0

    for name in EXPOSED_TOOLS:
        spec = all_defs.get(name)
        if spec is None:
            logger.debug(
                "skipping %s — not registered in this Hermes process", name
            )
            continue

        description = spec.get("description") or f"Hermes {name} tool"
        params_schema = spec.get("parameters") or {"type": "object", "properties": {}}

        # The SDK wants a Python callable and derives the input schema from
        # its signature — there is no inputSchema parameter on either the
        # decorator or add_tool(). So build a closure that takes the arguments
        # dict, dispatches via handle_function_call, returns the result
        # string, and carries a __signature__ synthesized from the Hermes
        # JSON Schema (see _signature_from_schema) for the SDK to read.
        def _make_handler(tool_name: str, schema: dict | None):
            sig, annots = _signature_from_schema(schema)

            def _dispatch(**kwargs: Any) -> str:
                try:
                    # Filter out None values before dispatch so unset optionals
                    # aren't forwarded to the handler.
                    args = {k: v for k, v in kwargs.items() if v is not None}
                    override = _AGENT_LOOP_DISPATCH.get(tool_name)
                    if override is not None:
                        return override(args or {})
                    return handle_function_call(tool_name, args or {})
                except Exception as exc:
                    logger.exception("tool %s raised", tool_name)
                    return json.dumps({"error": str(exc), "tool": tool_name})

            _dispatch.__name__ = tool_name
            _dispatch.__doc__ = description
            _dispatch.__signature__ = sig
            _dispatch.__annotations__ = {**annots, "return": str}
            return _dispatch

        try:
            mcp.add_tool(
                _make_handler(name, params_schema),
                name=name,
                description=description,
            )
        except TypeError:
            # Older mcp SDK signature — fall back to decorator-style. The
            # synthesized __signature__ on the handler still drives schema
            # generation there.
            handler = _make_handler(name, params_schema)
            handler = mcp.tool(name=name, description=description)(handler)

        exposed_count += 1

    logger.info(
        "hermes-tools MCP server registered %d/%d tools",
        exposed_count,
        len(EXPOSED_TOOLS),
    )
    return mcp


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for `python -m agent.transports.hermes_tools_mcp_server`."""
    argv = argv or sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv

    log_level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,  # MCP uses stdio for protocol — logs MUST go to stderr
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Quiet mode: keep Hermes' own banners off stdout (which is the MCP wire).
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")

    try:
        server = _build_server()
    except ImportError as exc:
        sys.stderr.write(f"hermes-tools MCP server cannot start: {exc}\n")
        return 2

    # MCPServer.run() defaults to stdio transport, which is what codex
    # spawns us on.
    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.exception("hermes-tools MCP server crashed")
        sys.stderr.write(f"hermes-tools MCP server error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
