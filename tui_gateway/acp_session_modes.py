"""Claude-over-ACP session modes: permission mode and bridge/native prompt mode.

FORK-ONLY MODULE. Upstream has no file at this path, so it can never produce a
rebase conflict. It exists to keep this feature out of ``tui_gateway/server.py``,
which upstream rewrites constantly -- 485 commits in the 90 days before this
extraction, against 371 fork lines living there. Every one of those lines was a
line a future rebase could collide with.

The split rule this follows: code that is purely ADDITIVE to upstream moves into
a fork-only file; code that MODIFIES upstream's own logic stays where it is.
Isolating a modification means overriding an upstream function, which trades a
loud failure (a conflict marker you must resolve) for a silent one (your override
shadows an upstream bugfix forever, with no signal). Everything here is additive:
seven helpers and two ``config.set`` arms upstream has no concept of.

``server.py`` keeps four thin call sites and re-exports ``_acp_permission_mode_info``
/ ``_acp_system_prompt_mode_info`` by importing them. **That re-export is
load-bearing, not cosmetic:** ``tui_gateway/methods_config.py`` handler bodies are
rebound onto server.py's globals at install time (see ``method_ctx.py``), so they
resolve those two names off server.py's namespace at call time instead of
importing them. Drop the import and ``config.get permission_mode`` dies with a
NameError.

This module imports nothing from ``server.py``. The four server helpers the
``config.set`` arms need (``_ok`` / ``_err`` / ``_emit`` / ``_session_info``) are
injected as arguments -- a reverse import would be a cycle, and injection keeps
the arms testable without standing up a gateway.

Design notes, moved here with the code:

Both modes are SESSION-SCOPED by design. The mode lives on the per-session
``CopilotACPClient`` (``_mode_override``), never in config.yaml: every AIAgent
builds its own client, so two panes side by side each own their mode, and a
dropdown writing global config would silently retarget the other pane.

The DURABLE store is the session dict, not the client. A model switch rebuilds
``agent.client`` from scratch (``run_agent._apply_model_switch``), which would
drop an override held only on the client; re-pushing from the session at turn
start makes the pick survive that, and covers a draft that picked a mode before
its agent existed.

The bridge/native LOCK follows that same split, and for the same reason: a
client can only speak for the lifetime of its own ACP subprocess, and the chat
outlives it. See ``_SYSTEM_PROMPT_MODE_LATCH_SESSION_KEY``.
"""

import logging
import os

logger = logging.getLogger(__name__)


_PERMISSION_MODE_SESSION_KEY = "permission_mode_override"

_SYSTEM_PROMPT_MODE_SESSION_KEY = "acp_system_prompt_mode_override"
_SYSTEM_PROMPT_MODE_OPTIONS: tuple[str, ...] = ("bridge", "native")

# Stamped once a chat has actually started a turn.
#
# The client's own ``locked`` is scoped to the ACP SUBPROCESS -- it is
# ``bool(self._session_id)``, and ``_shutdown_process`` clears that id on a model
# switch, a transcript divergence, and a failed resync. So the composer pill
# silently became editable again mid-chat every time the child was rebuilt, and
# a pick made in that window really did take effect on the next ``session/new``.
# systemPrompt is sent once per ACP session with no RPC to re-send it, so that
# spliced a DIFFERENT system prompt into the middle of one transcript.
#
# This latch is scoped to the CHAT instead, and it is a hard one: once a chat has
# taken a turn its bridge/native pick is frozen for the life of that chat, model
# switches included. Starting a new chat is the escape hatch -- a fresh session
# dict carries no latch, which is also why nothing has to clear it.
_SYSTEM_PROMPT_MODE_LATCH_SESSION_KEY = "acp_system_prompt_mode_latched"

_EMPTY_MODE_INFO = {"available": False, "value": "", "source": "", "locked": False, "options": []}


def _empty_info() -> dict:
    """A fresh copy of the unavailable-mode block.

    Returned by value on every failure path -- callers put these straight into
    ``session.info`` payloads, and handing out one shared dict would let a
    mutation downstream poison every later response.
    """
    return dict(_EMPTY_MODE_INFO)


def _acp_permission_client(session: dict | None):
    """The CopilotACPClient behind a session, or None if it isn't one.

    Duck-typed rather than ``isinstance``: importing CopilotACPClient here
    would pull the ACP transport into every gateway start (including profiles
    that never touch the provider), and these two methods are the whole
    contract this module needs.
    """
    agent = (session or {}).get("agent")
    if agent is None:
        return None
    client = getattr(agent, "client", None)
    if client is None:
        return None
    if not callable(getattr(client, "set_permission_mode", None)):
        return None
    if not callable(getattr(client, "permission_mode_state", None)):
        return None
    return client


def _acp_provider_is_claude(provider: str) -> bool:
    """True when ``provider`` is a copilot-acp slug rerouted to Claude Code.

    The ``copilot-acp`` provider is generic -- it is only Claude because this
    machine points ``HERMES_COPILOT_ACP_COMMAND`` at claude-agent-acp. The
    reroute test has one owner in ``hermes_cli.models``; re-deriving it here
    (or in TypeScript) would be a second copy free to drift.
    """
    if (provider or "").strip().lower() != "copilot-acp":
        return False
    try:
        from hermes_cli.models import _copilot_acp_is_rerouted

        return bool(_copilot_acp_is_rerouted())
    except Exception:
        logger.debug("copilot-acp reroute probe failed", exc_info=True)
        return False


def _apply_session_permission_mode(session: dict | None) -> str:
    """Push the session's pinned permission mode onto its live ACP client.

    Idempotent and safe to call every turn: ``set_permission_mode`` only
    assigns, and the client's own ``_sync_acp_mode`` skips an unchanged value,
    so a re-push costs nothing on the wire. Returns the effective mode, or ""
    when there is nothing to push to yet.
    """
    if not session:
        return ""
    pinned = str(session.get(_PERMISSION_MODE_SESSION_KEY) or "").strip()
    if not pinned:
        return ""
    client = _acp_permission_client(session)
    if client is None:
        return ""
    try:
        return str(client.set_permission_mode(pinned) or "")
    except Exception:
        logger.debug("failed to apply session permission mode", exc_info=True)
        return ""


def _acp_permission_mode_info(session: dict | None, provider: str) -> dict:
    """The ``acp_permission`` block published on session.info.

    ``available`` is what gates the composer pill, so it must be false for
    every non-Claude provider -- the pill is meaningless for OpenAI/Gemini and
    for a plain GitHub-Copilot copilot-acp session.
    """
    if not _acp_provider_is_claude(provider):
        return _empty_info()
    client = _acp_permission_client(session)
    if client is None:
        # Draft or not-yet-built agent: report the pin the user already made so
        # the pill paints their choice instead of blinking back to default
        # while the session builds.
        pinned = str((session or {}).get(_PERMISSION_MODE_SESSION_KEY) or "").strip()
        try:
            from agent.copilot_acp_client import _FALLBACK_ACP_MODE_IDS, _requested_acp_mode

            return {
                "available": True,
                "value": _requested_acp_mode(pinned),
                "source": "session" if pinned else "config",
                "locked": bool((os.environ.get("HERMES_ACP_PERMISSION_MODE") or "").strip()),
                "options": list(_FALLBACK_ACP_MODE_IDS),
            }
        except Exception:
            logger.debug("permission-mode fallback state failed", exc_info=True)
            return _empty_info()
    try:
        state = dict(client.permission_mode_state())
        state["available"] = True
        return state
    except Exception:
        logger.debug("permission-mode state failed", exc_info=True)
        return _empty_info()


def _acp_system_prompt_mode_client(session: dict | None):
    """The CopilotACPClient behind a session, or None -- mirrors ``_acp_permission_client``."""
    agent = (session or {}).get("agent")
    if agent is None:
        return None
    client = getattr(agent, "client", None)
    if client is None:
        return None
    if not callable(getattr(client, "set_system_prompt_mode", None)):
        return None
    if not callable(getattr(client, "system_prompt_mode_state", None)):
        return None
    return client


def _apply_session_system_prompt_mode(session: dict | None) -> str:
    """Push the session's pinned bridge/native pick onto its live ACP client.

    Unlike ``_apply_session_permission_mode`` this is not re-applied every
    turn for a live session -- it can't be, there's no RPC for it -- but
    calling it unconditionally is harmless: once the client's own
    ``_session_id`` is set the write is inert. What this covers is a draft
    that picked a mode before its agent existed, and a model switch that
    rebuilt ``agent.client`` from scratch before the first turn ran.
    """
    if not session:
        return ""
    pinned = str(session.get(_SYSTEM_PROMPT_MODE_SESSION_KEY) or "").strip()
    if not pinned:
        return ""
    client = _acp_system_prompt_mode_client(session)
    if client is None:
        return ""
    try:
        return str(client.set_system_prompt_mode(pinned) or "")
    except Exception:
        logger.debug("failed to apply session system prompt mode", exc_info=True)
        return ""


def _system_prompt_mode_latched(session: dict | None) -> bool:
    """True once this CHAT has committed to a bridge/native pick.

    Three independent tells, because no single one covers every way a chat
    arrives at "already started":

    * the explicit latch, stamped at turn start -- the live path, and the only
      one that holds during turn 1, while ``history`` is still the empty
      pre-turn snapshot;
    * a resume handle: the desktop resumes with ``defer_history=True``, so
      ``session["history"]`` is ``[]`` for the first moment of a chat that
      already has fifty turns behind it, and the pill would flash editable;
    * a non-empty history -- the durable fallback covering every other resume
      path, and what keeps this honest across a gateway restart, which drops
      the in-memory flag but not the transcript.

    Read without ``history_lock``: this only asks whether the list is empty, and
    a list being appended to is never momentarily empty.
    """
    if not session:
        return False
    if session.get(_SYSTEM_PROMPT_MODE_LATCH_SESSION_KEY):
        return True
    if "resume_history_ready" in session:
        return True
    return bool(session.get("history"))


def _latch_system_prompt_mode(session: dict | None) -> None:
    """Freeze this chat's bridge/native pick. Called once per turn, at turn start.

    Gated on the ACP client rather than on ``_acp_provider_is_claude``: a session
    whose agent has no ``set_system_prompt_mode`` is not an ACP session at all,
    so this leaves no dead key on the OpenAI/Gemini chats sharing the process --
    and it costs no config read on the turn-start path.
    """
    if session is None:
        return
    if _acp_system_prompt_mode_client(session) is None:
        return
    session[_SYSTEM_PROMPT_MODE_LATCH_SESSION_KEY] = True


def _acp_system_prompt_mode_info(session: dict | None, provider: str) -> dict:
    """The ``acp_system_prompt_mode`` block published on session.info.

    Mirrors ``_acp_permission_mode_info``'s shape and gating exactly, so the
    Bridge pill can reuse the same ``available``/``locked`` contract the
    permission pill already established.
    """
    if not _acp_provider_is_claude(provider):
        return _empty_info()
    client = _acp_system_prompt_mode_client(session)
    if client is None:
        pinned = str((session or {}).get(_SYSTEM_PROMPT_MODE_SESSION_KEY) or "").strip()
        try:
            from agent.copilot_acp_client import _acp_config_str

            configured = (_acp_config_str("system_prompt_mode") or "").strip().lower()
        except Exception:
            configured = ""
        default_mode = configured if configured in _SYSTEM_PROMPT_MODE_OPTIONS else "bridge"
        return {
            "available": True,
            "value": pinned if pinned in _SYSTEM_PROMPT_MODE_OPTIONS else default_mode,
            "source": "session" if pinned else "config",
            # A resumed chat reaches here whenever its agent has not been rebuilt
            # yet, so this branch has to honour the latch too -- reporting False
            # would hand the user an editable pill on a fifty-turn transcript.
            "locked": _system_prompt_mode_latched(session),
            "options": list(_SYSTEM_PROMPT_MODE_OPTIONS),
        }
    try:
        state = dict(client.system_prompt_mode_state())
        state["available"] = True
        # OR, never AND. The client's own lock is the ACP subprocess's lifetime
        # and drops on every rebuild; the chat's latch outlives it. Deliberately
        # additive: the client stays free to lock a chat this latch has not
        # reached yet (the gap between session/new and the first turn end).
        if _system_prompt_mode_latched(session):
            state["locked"] = True
        return state
    except Exception:
        logger.debug("system-prompt-mode state failed", exc_info=True)
        return _empty_info()


def acp_session_info_blocks(session: dict | None, provider: str) -> dict:
    """Both ACP mode blocks for a ``session.info`` payload, as one mapping.

    Both keys are ALWAYS present so the composer can HIDE a pill on a false
    ``available`` rather than having to guess from a missing key -- an older
    backend and a non-Claude session would be indistinguishable otherwise.
    """
    return {
        "acp_permission": _acp_permission_mode_info(session, provider),
        "acp_system_prompt_mode": _acp_system_prompt_mode_info(session, provider),
    }


def apply_session_acp_modes(session: dict | None) -> None:
    """Re-push both session-pinned ACP modes onto the live client.

    Call at turn start, AFTER the model sync: a model switch rebuilds
    ``agent.client``, and the fresh client knows nothing about a mode the user
    pinned on the old one. Also covers a draft that picked a mode before its
    agent existed. A no-op for every non-ACP provider and every unpinned
    session, so it is safe to call unconditionally.

    The bridge/native latch is stamped LAST, after the pick has been pushed: a
    turn is about to run, so whatever mode this session is on is the mode its
    transcript will be written under, and it must not change again.
    """
    _apply_session_permission_mode(session)
    _apply_session_system_prompt_mode(session)
    _latch_system_prompt_mode(session)


def _session_provider(session: dict) -> str:
    """Provider for a session -- an explicit ``model_override`` wins over the agent.

    The override is checked first because a pending model switch is the user's
    latest intent; the live agent may still be on the model they switched away
    from.
    """
    override = session.get("model_override")
    provider = ""
    if isinstance(override, dict):
        # ``.get("provider")`` can be an explicit ``None`` (a model_override
        # with no provider key -- see session.create's `provider` being
        # omitted whenever the desktop's provider selection is empty), and
        # bare `str(None)` would stringify to the literal "None", which is
        # truthy and skips the agent fallback below. `or ""` catches that.
        provider = str(override.get("provider") or "").strip()
    if not provider:
        provider = str(getattr(session.get("agent"), "provider", "") or "").strip()
    return provider


def handle_acp_config_set(
    key: str,
    value,
    session: dict | None,
    rid,
    params: dict,
    *,
    ok,
    err,
    emit,
    session_info,
) -> dict | None:
    """The ``config.set`` arms for the two ACP session modes.

    Returns an RPC response dict when ``key`` is one of ours, or ``None`` to let
    server.py's own ``if key ==`` chain carry on. ``ok`` / ``err`` / ``emit`` /
    ``session_info`` are server.py's helpers, injected so this module never has
    to import server.py.
    """
    if key == "permission_mode":
        # Claude-over-ACP permission mode, SESSION-SCOPED only. There is
        # deliberately no scope="global" arm: config.yaml already owns the
        # starting mode for new chats (copilot_acp.permission_mode, editable in
        # Settings), and letting a per-chat control write it would flip every
        # other open pane -- the exact bug per-session scoping exists to avoid.
        if session is None:
            return err(rid, 4002, "permission_mode requires a session")
        if not _acp_provider_is_claude(_session_provider(session)):
            return err(rid, 4002, "permission_mode is only available on Claude over ACP")

        raw = str(value or "").strip()
        client = _acp_permission_client(session)
        if raw:
            # Validate against what the agent ACTUALLY advertises when a
            # session is live; fall back to the known ids otherwise. Rejecting
            # here (rather than letting _select_acp_mode log a warning and move
            # on) is what stops the dropdown from silently doing nothing.
            allowed: list[str] = []
            if client is not None:
                try:
                    allowed = list(client.permission_mode_state().get("options") or [])
                except Exception:
                    allowed = []
            if not allowed:
                try:
                    from agent.copilot_acp_client import _FALLBACK_ACP_MODE_IDS

                    allowed = list(_FALLBACK_ACP_MODE_IDS)
                except Exception:
                    allowed = []
            match = next((m for m in allowed if m == raw), None)
            if match is None:
                match = next((m for m in allowed if m.lower() == raw.lower()), None)
            if match is None:
                return err(
                    rid,
                    4002,
                    f"unknown permission mode: {value}; pick one of {'|'.join(allowed)}",
                )
            raw = match

        # The session dict is the durable store; the client is only transport.
        # "" clears the pin and falls back to config for the next turn.
        if raw:
            session[_PERMISSION_MODE_SESSION_KEY] = raw
        else:
            session.pop(_PERMISSION_MODE_SESSION_KEY, None)

        effective = raw
        if client is not None:
            try:
                effective = str(client.set_permission_mode(raw) or "")
            except Exception:
                logger.debug("set_permission_mode failed", exc_info=True)

        # Applied at the NEXT turn's session/set_mode, not now: this runs on the
        # gateway request thread while a running turn owns the client's session
        # lock. Report it so the UI can say so instead of implying the change
        # governs a prompt already in flight.
        deferred = bool(session.get("running"))
        agent = session.get("agent")
        if agent is not None:
            emit("session.info", params.get("session_id", ""), session_info(agent, session))
        return ok(
            rid,
            {
                "key": key,
                "value": effective,
                "scope": "session",
                "deferred": deferred,
            },
        )

    if key == "acp_system_prompt_mode":
        # Claude-over-ACP bridge/native pick, SESSION-SCOPED only -- same
        # reasoning as permission_mode above: config.yaml
        # (copilot_acp.system_prompt_mode) owns the default for new chats,
        # and a per-chat control writing it globally would retarget every
        # other open pane.
        if session is None:
            return err(rid, 4002, "acp_system_prompt_mode requires a session")
        if not _acp_provider_is_claude(_session_provider(session)):
            return err(rid, 4002, "acp_system_prompt_mode is only available on Claude over ACP")

        raw = str(value or "").strip().lower()
        client = _acp_system_prompt_mode_client(session)

        # The chat-scoped latch is checked FIRST and independently of the client:
        # after a subprocess rebuild the client's own lock reads False, and this
        # is the whole point of the latch -- a chat that has taken a turn cannot
        # change the system prompt its transcript was written under, no matter
        # what tore the child down in between.
        if _system_prompt_mode_latched(session):
            return err(
                rid,
                4002,
                "acp_system_prompt_mode is locked for this chat; start a new chat to change it",
            )

        # Unlike permission_mode there is no live re-negotiation possible --
        # systemPrompt is sent once, at session/new, and there is no RPC to
        # resend it. So a session that has already opened its ACP session
        # rejects the write outright rather than silently accepting a pick
        # that can never take effect.
        if client is not None:
            try:
                if bool(client.system_prompt_mode_state().get("locked")):
                    return err(
                        rid,
                        4002,
                        "acp_system_prompt_mode is locked once the session has started",
                    )
            except Exception:
                logger.debug("system_prompt_mode lock check failed", exc_info=True)

        if raw and raw not in _SYSTEM_PROMPT_MODE_OPTIONS:
            return err(
                rid,
                4002,
                f"unknown system prompt mode: {value}; pick one of {'|'.join(_SYSTEM_PROMPT_MODE_OPTIONS)}",
            )

        # The session dict is the durable store; the client is only transport
        # -- same split as permission_mode, and for the same reason: a model
        # switch before the first turn rebuilds agent.client from scratch.
        if raw:
            session[_SYSTEM_PROMPT_MODE_SESSION_KEY] = raw
        else:
            session.pop(_SYSTEM_PROMPT_MODE_SESSION_KEY, None)

        effective = raw
        if client is not None:
            try:
                effective = str(client.set_system_prompt_mode(raw) or "")
            except Exception:
                logger.debug("set_system_prompt_mode failed", exc_info=True)

        agent = session.get("agent")
        if agent is not None:
            emit("session.info", params.get("session_id", ""), session_info(agent, session))
        return ok(rid, {"key": key, "value": effective, "scope": "session", "deferred": False})

    return None
