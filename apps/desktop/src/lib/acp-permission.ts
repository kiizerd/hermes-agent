import type { AcpPermissionState } from '@/app/types'
import { $gateway } from '@/store/gateway'

/**
 * Claude-over-ACP permission mode — the renderer half of `session.info`'s
 * `acp_permission` block.
 *
 * The mode is SESSION-scoped on purpose. It lives on the per-session
 * CopilotACPClient in the backend, never in config.yaml, so two panes open side
 * by side each own their mode; a control that wrote global config would
 * silently retarget the other pane the moment you used it.
 */

export const EMPTY_ACP_PERMISSION: AcpPermissionState = {
  available: false,
  locked: false,
  options: [],
  source: '',
  value: ''
}

/**
 * The one mode that needs a confirm before it is applied: it turns off the
 * approval prompt for everything the agent runs, including shell writes.
 */
export const BYPASS_PERMISSION_MODE = 'bypassPermissions'

/**
 * Normalize an untyped `acp_permission` payload.
 *
 * Defaults to UNAVAILABLE on anything malformed or missing. That direction
 * matters: an older backend sends no block at all, and defaulting to available
 * would paint a dropdown whose clicks the backend would reject.
 */
export function normalizeAcpPermission(raw: unknown): AcpPermissionState {
  if (!raw || typeof raw !== 'object') {
    return EMPTY_ACP_PERMISSION
  }

  const payload = raw as Record<string, unknown>

  if (payload.available !== true) {
    return EMPTY_ACP_PERMISSION
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
export function acpPermissionEquals(a: AcpPermissionState, b: AcpPermissionState): boolean {
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
 * Pin one session's permission mode via gateway `config.set` — the same
 * session-scoped RPC the model picker uses, never a global config write.
 *
 * The backend applies it at the next turn's `session/set_mode` (the running
 * turn owns the ACP session lock), and answers `deferred: true` when a turn was
 * in flight. Resolves to the effective mode the backend confirmed, which may
 * differ in case from what was sent — it normalizes against the ids the agent
 * actually advertises.
 */
export async function setSessionPermissionMode(sessionId: string, mode: string): Promise<string> {
  const gateway = $gateway.get()

  if (!gateway) {
    throw new Error('Hermes gateway unavailable')
  }

  const result = await gateway.request<{ deferred?: boolean; value?: string }>('config.set', {
    key: 'permission_mode',
    session_id: sessionId,
    value: mode
  })

  return typeof result?.value === 'string' ? result.value : mode
}
