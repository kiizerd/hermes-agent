import type * as React from 'react'

import type { ChatMessage } from '@/lib/chat-messages'
import type { SessionMessage, UsageStats } from '@/types/hermes'

export interface ContextSuggestion {
  text: string
  display: string
  meta?: string
}

export interface ImageAttachResponse {
  attached?: boolean
  path?: string
  text?: string
  message?: string
  // Returned by the byte-upload variant (image.attach_bytes) used in remote mode.
  count?: number
  bytes?: number
  name?: string
  width?: number
  height?: number
  token_estimate?: number
}

export interface ImageDetachResponse {
  detached?: boolean
  count?: number
}

export interface FileAttachResponse {
  attached?: boolean
  message?: string
  // Gateway-side absolute path the file was staged to.
  path?: string
  // Workspace-relative path used to build ref_text.
  ref_path?: string
  // Rewritten @file: ref that resolves on the gateway (workspace-relative).
  ref_text?: string
  // True when bytes/host file were copied into the session workspace.
  uploaded?: boolean
  name?: string
}

export interface SlashExecResponse {
  output?: string
  warning?: string
}

export interface BrowserManageResponse {
  connected?: boolean
  url?: string
  messages?: string[]
}

/** Response from the `session.compress` RPC. `messages` is the post-compress
 *  history (same shape `session.resume` returns via `_history_to_messages`),
 *  so the desktop can replace its transcript from it rather than leaving stale
 *  bubbles on screen. `summary` carries the "compressed N → M messages" line. */
export interface SessionCompressResponse {
  host_ack?: {
    output?: string
  }
  info?: {
    title?: string
    usage?: Partial<UsageStats>
  }
  messages?: SessionMessage[]
  removed?: number
  status?: string
  summary?: {
    aborted?: boolean
    headline?: string
    noop?: boolean
    note?: null | string
    token_line?: string
  }
  usage?: Partial<UsageStats>
}

export interface SessionSteerResponse {
  // 'queued' == accepted into the live turn's steer slot (injected at the next
  // tool-result boundary); 'rejected' == no live tool window, caller queues.
  status?: 'queued' | 'rejected'
  text?: string
}

export interface SessionRedirectResponse {
  status?: 'redirected' | 'queued' | 'rejected'
  text?: string
}

export interface SessionTitleResponse {
  title?: string
  // True when the session row isn't persisted yet and the title was queued
  // to be applied on the first turn (see tui_gateway session.title handler).
  pending?: boolean
  session_key?: string
}

export interface HandoffRequestResponse {
  queued?: boolean
  session_key?: string
  platform?: string
  // Human-readable home channel name for the destination platform.
  home_name?: string
}

export interface HandoffStateResponse {
  // '' | 'pending' | 'running' | 'completed' | 'failed'
  state?: string
  platform?: string
  error?: string
}

export interface HandoffFailResponse {
  failed?: boolean
  state?: string
}

export interface ExecCommandDispatchResponse {
  type: 'exec' | 'plugin'
  output?: string
}

export interface AliasCommandDispatchResponse {
  type: 'alias'
  target: string
}

export interface SkillCommandDispatchResponse {
  type: 'skill'
  name: string
  message?: string
  /** The invocation the UI renders (`/work fix the leak`). `message` is the
   *  expanded skill body — model-facing scaffolding no surface may show. */
  display?: string
}

export interface SendCommandDispatchResponse {
  type: 'send'
  message: string
  notice?: string
  /** Set for a skill-bundle send: see SkillCommandDispatchResponse.display. */
  display?: string
}

export interface PrefillCommandDispatchResponse {
  type: 'prefill'
  message: string
  notice?: string
}

export type CommandDispatchResponse =
  | ExecCommandDispatchResponse
  | AliasCommandDispatchResponse
  | SkillCommandDispatchResponse
  | SendCommandDispatchResponse
  | PrefillCommandDispatchResponse

export type SidebarNavId = 'artifacts' | 'command-center' | 'cron' | 'messaging' | 'new-session' | 'settings' | 'skills'

export interface SidebarNavItem {
  /** Built-in view id, or a contributed row's namespaced contribution id. */
  id: SidebarNavId | (string & {})
  label: string
  icon: React.ComponentType<{ className?: string }>
  route?: string
  action?: 'new-session'
  /** Keybind action id — when set, the tooltip shows the keybind hint. */
  keybindActionId?: string
}

/**
 * Claude-over-ACP permission mode, mirrored from `session.info.acp_permission`.
 *
 * `available` is the pill's visibility gate and the ONLY thing the renderer may
 * use for it. The `copilot-acp` provider is generic — it is Claude only when
 * this machine reroutes it — and that reroute test lives in Python
 * (`hermes_cli.models._copilot_acp_is_rerouted`). Re-deriving it here would be
 * a second copy free to drift, so the backend decides and the renderer obeys.
 */
export interface AcpPermissionState {
  available: boolean
  /** Effective mode id, e.g. `plan`. Empty means "leave the agent as it is". */
  value: string
  /** Which rung of the ladder won: `env` | `session` | `config` | `agent`. */
  source: string
  /** An operator pinned `HERMES_ACP_PERMISSION_MODE`; the picker can't win. */
  locked: boolean
  /** Mode ids the agent advertised (or the known fallbacks before a session). */
  options: string[]
}

/**
 * Claude-over-ACP bridge/native system-prompt mode, mirrored from
 * `session.info.acp_system_prompt_mode`.
 *
 * "bridge" appends Hermes' instructions onto Claude Code's own preset
 * (identity, tool-schema guidance, auto CLAUDE.md/env context all stay).
 * "native" replaces the preset outright with Hermes' own full system prompt,
 * so the session runs as Hermes rather than as Claude Code.
 *
 * Same gating contract as `AcpPermissionState`: `available` is the pill's
 * visibility gate, decided backend-side. The one real difference from
 * permission mode is `locked` — there is no ACP RPC to resend systemPrompt,
 * so this locks the moment the live session opens, not just under an
 * operator env pin.
 */
export interface AcpSystemPromptModeState {
  available: boolean
  /** `bridge` | `native`. Empty only when `available` is false. */
  value: string
  /** Which rung won: `session` | `config` | `default`. */
  source: string
  /**
   * True once the live ACP session has opened.
   *
   * Scoped to the SESSION, not the chat. Anything that tears the ACP
   * subprocess down — a mid-chat model switch is the realistic one — clears
   * the backend's session id and this goes false again until the next turn
   * reopens it. That is the intended escape hatch: the lock exists because a
   * running session can't be re-prompted, and after a rebuild there is no
   * running session to contradict.
   */
  locked: boolean
  options: string[]
}

export interface ClientSessionState {
  storedSessionId: string | null
  messages: ChatMessage[]
  branch: string
  cwd: string
  model: string
  provider: string
  reasoningEffort: string
  serviceTier: string
  fast: boolean
  yolo: boolean
  acpPermission: AcpPermissionState
  acpSystemPromptMode: AcpSystemPromptModeState
  personality: string
  busy: boolean
  awaitingResponse: boolean
  streamId: string | null
  sawAssistantPayload: boolean
  /** This window picked up a turn it did not start — it resumed onto a session
   *  that was already running somewhere else (leaving HUD mode, opening a
   *  pop-out mid-turn). It therefore holds the reply but never received the
   *  prompt, so the usual "I streamed it, my transcript is complete" shortcut
   *  is false and the turn must hydrate from stored history when it settles. */
  adoptedRunningTurn: boolean
  pendingBranchGroup: string | null
  interrupted: boolean
  /** True after message.interim finalized a bubble in the still-running turn. */
  interimBoundaryPending: boolean
  /** A blocking clarify prompt is waiting on the user for this session. Drives
   *  the sidebar "needs input" indicator; cleared when the turn resumes/ends. */
  needsInput: boolean
  /** Epoch ms the current turn started, or null when idle. Per-session so a
   *  background turn's elapsed timer keeps counting while another session is
   *  focused, and switching sessions doesn't zero a still-running turn's clock.
   *  Seeded optimistically at submit (before the backend accepts), so it is a
   *  CLOCK, not proof the turn is live — gate on turnLive for that.
   *  The global $turnStartedAt mirrors whichever session is currently viewed. */
  turnStartedAt: number | null
  /** The backend has confirmed this turn is running (message.start, a
   *  running=true session.info edge, or resuming onto an in-flight turn).
   *  False while a submit is only optimistically armed — the discriminator the
   *  no-payload settle gate needs now that turnStartedAt is seeded at send. */
  turnLive: boolean
  /** Cumulative token usage, updated per completed turn. Per-session twin of
   *  the primary-only $currentUsage — the statusbar reads it for a focused
   *  tile's context count. Null until the first turn reports. */
  usage: null | UsageStats
}
