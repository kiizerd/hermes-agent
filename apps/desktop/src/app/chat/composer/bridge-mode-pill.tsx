import { useStore } from '@nanostores/react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import {
  BRIDGE_SYSTEM_PROMPT_MODE,
  NATIVE_SYSTEM_PROMPT_MODE,
  setSessionSystemPromptMode
} from '@/lib/acp-system-prompt-mode'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { $draftAcpSystemPromptMode, setDraftAcpSystemPromptMode } from '@/store/session'

const PILL = cn(
  'h-(--composer-control-size) max-w-24 shrink-0 gap-1 rounded-md px-2 text-xs font-normal',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

/**
 * Composer bridge/native system-prompt toggle for Claude-over-ACP sessions.
 *
 * Renders NOTHING unless the backend says this session is Claude reached over
 * ACP (`session.info.acp_system_prompt_mode.available`) — same backend-decided
 * gate as PermissionModePill, and for the same reason: duplicating the
 * copilot-acp-is-really-Claude heuristic in TypeScript would drift from the
 * Python original.
 *
 * Unlike PermissionModePill this is a plain click-to-toggle, not a dropdown:
 * only two states exist and neither is a "dangerous" pick worth a confirm
 * dialog.
 *
 * The editable window is the DRAFT, and essentially only the draft. There is
 * no `session/set_mode`-equivalent RPC for systemPrompt — it is sent once,
 * inside `session/new`, which the backend opens lazily on the first turn. So
 * the two states are:
 *
 *   no runtime id  → editable, writes the sticky `$draftAcpSystemPromptMode`
 *                    store. `createBackendSessionForSend` replays that onto
 *                    the new session before its first turn, the same way it
 *                    replays a YOLO armed on the draft.
 *   runtime id     → the backend's `locked` decides. It is false only in the
 *                    narrow gap between `session.create` and the first turn
 *                    (and again after a model switch tears the subprocess
 *                    down), and true otherwise.
 *
 * Writing the draft pick to a local store rather than through the gateway is
 * forced, not chosen: with no session there is no `session_id` to scope a
 * `config.set` to, and the global config key is the DEFAULT for every new
 * chat — using it here would retarget every other pane.
 */
export function BridgeModePill({ compact = false, disabled }: { compact?: boolean; disabled: boolean }) {
  const copy = useI18n().t.composer
  const view = useSessionView()
  const mode = useStore(view.$acpSystemPromptMode)
  const runtimeId = useStore(view.$runtimeId)
  const draftMode = useStore($draftAcpSystemPromptMode)

  if (!mode.available) {
    return null
  }

  const noSession = !runtimeId

  // A draft shows its own pending pick. `mode.value` is the LAST session's
  // mirror (nothing clears it on New Chat, which is also what keeps
  // `available` true for the draft at all), so it is the fallback, not the
  // source, once the user has picked.
  const current = noSession
    ? draftMode || mode.value || BRIDGE_SYSTEM_PROMPT_MODE
    : mode.value || BRIDGE_SYSTEM_PROMPT_MODE

  const isNative = current === NATIVE_SYSTEM_PROMPT_MODE
  const label = isNative ? copy.bridgeModeNativeLabel : copy.bridgeModeBridgeLabel

  const readOnly = !noSession && mode.locked

  const title = mode.locked
    ? copy.bridgeModeLocked(label)
    : noSession
      ? copy.bridgeModeDraft(label)
      : copy.bridgeModeTitle(label)

  const onClick = async () => {
    if (readOnly) {
      return
    }

    const next = isNative ? BRIDGE_SYSTEM_PROMPT_MODE : NATIVE_SYSTEM_PROMPT_MODE

    if (!runtimeId) {
      // Draft: no RPC target exists yet. Parked in the sticky store and
      // replayed at session creation.
      setDraftAcpSystemPromptMode(next)

      return
    }

    try {
      await setSessionSystemPromptMode(runtimeId, next)
      // Keep the sticky draft in step so the NEXT new chat follows this pick,
      // matching how model/effort/fast behave.
      setDraftAcpSystemPromptMode(next)
    } catch (err) {
      // The backend rejects a locked/unavailable session rather than silently
      // ignoring it, so surface that instead of leaving the pill looking
      // applied. The next session.info repaints the real value either way.
      notifyError(err, copy.bridgeModeFailed)
    }
  }

  const pillClass = compact
    ? cn(
        'size-(--composer-control-size) shrink-0 justify-center gap-0 rounded-md p-0',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      )
    : cn(PILL, isNative && 'bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary')

  const content = compact ? <span className="text-[0.625rem] font-semibold">{isNative ? 'N' : 'B'}</span> : label

  if (readOnly) {
    return (
      <Tip label={title} side="top">
        {/* Not `disabled`: a disabled button drops its tooltip, and the
            tooltip is the only place that explains WHY it can't be changed. */}
        <Button
          aria-disabled
          aria-label={title}
          className={cn(pillClass, 'cursor-default opacity-70')}
          onClick={event => event.preventDefault()}
          type="button"
          variant="ghost"
        >
          {content}
        </Button>
      </Tip>
    )
  }

  return (
    <Tip label={title} side="top">
      <Button
        aria-label={title}
        aria-pressed={isNative}
        className={pillClass}
        disabled={disabled}
        onClick={() => void onClick()}
        type="button"
        variant="ghost"
      >
        {content}
      </Button>
    </Tip>
  )
}
