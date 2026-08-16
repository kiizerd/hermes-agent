import type { AcpSystemPromptModeState } from '@/app/types'
import { $gateway } from '@/store/gateway'

/**
 * Claude-over-ACP bridge/native system-prompt mode — the renderer half of
 * `session.info`'s `acp_system_prompt_mode` block. See the type's own doc
 * comment in `@/app/types` for what the two values mean.
 *
 * SESSION-scoped for the same reason as acp-permission.ts: the pick lives on
 * the per-session CopilotACPClient in the backend, never in config.yaml, so
 * two panes open side by side each keep their own.
 */

export const EMPTY_ACP_SYSTEM_PROMPT_MODE: AcpSystemPromptModeState = {
  available: false,
  locked: false,
  options: [],
  source: '',
  value: ''
}

export const BRIDGE_SYSTEM_PROMPT_MODE = 'bridge'
export const NATIVE_SYSTEM_PROMPT_MODE = 'native'

/**
 * Normalize an untyped `acp_system_prompt_mode` payload.
 *
 * Defaults to UNAVAILABLE on anything malformed or missing, same direction as
 * `normalizeAcpPermission` and for the same reason: an older backend sends no
 * block at all, and defaulting to available would paint a pill whose clicks
 * the backend would reject.
 */
export function normalizeAcpSystemPromptMode(raw: unknown): AcpSystemPromptModeState {
  if (!raw || typeof raw !== 'object') {
    return EMPTY_ACP_SYSTEM_PROMPT_MODE
  }

  const payload = raw as Record<string, unknown>

  if (payload.available !== true) {
    return EMPTY_ACP_SYSTEM_PROMPT_MODE
  }

  const options = Array.isArray(payload.options)
    ? payload.options.filter((option): option is string => typeof option === 'string' && option.length > 0)
    : []

  return {
    available: true,
    locked: payload.locked === true,
    options,
    source: typeof payload.source === 'string' ? payload.source : '',
    value: typeof payload.value === 'string' ? payload.value : ''
  }
}

/** Two states are equal when every field is — used to skip no-op store writes. */
export function acpSystemPromptModeEquals(a: AcpSystemPromptModeState, b: AcpSystemPromptModeState): boolean {
  return (
    a.available === b.available &&
    a.locked === b.locked &&
    a.source === b.source &&
    a.value === b.value &&
    a.options.length === b.options.length &&
    a.options.every((option, index) => option === b.options[index])
  )
}

/**
 * Pin one session's bridge/native mode via gateway `config.set` — same
 * session-scoped RPC shape as `setSessionPermissionMode`.
 *
 * Unlike permission mode there is no mid-session re-negotiation: the backend
 * rejects this once the live ACP session has opened (`acp_system_prompt_mode
 * is locked once the session has started`), because there is no ACP RPC to
 * resend systemPrompt. Callers should already be gating on `locked` in the
 * UI; this rejection is the honest backstop, not the primary guard.
 */
export async function setSessionSystemPromptMode(sessionId: string, mode: string): Promise<string> {
  const gateway = $gateway.get()

  if (!gateway) {
    throw new Error('Hermes gateway unavailable')
  }

  const result = await gateway.request<{ value?: string }>('config.set', {
    key: 'acp_system_prompt_mode',
    session_id: sessionId,
    value: mode
  })

  return typeof result?.value === 'string' ? result.value : mode
}
