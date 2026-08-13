import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { releaseTypingFocus } from '@/components/ui/keyboard-first'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { BYPASS_PERMISSION_MODE, setSessionPermissionMode } from '@/lib/acp-permission'
import { ChevronDown, ShieldCheck } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

const PILL = cn(
  'h-(--composer-control-size) max-w-32 shrink-0 gap-1 rounded-md px-2 text-xs font-normal',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

/**
 * Composer permission-mode selector for Claude-over-ACP sessions.
 *
 * Renders NOTHING unless the backend says this session is Claude reached over
 * ACP (`session.info.acp_permission.available`). That gate is deliberately the
 * backend's call: the `copilot-acp` provider is generic and is only Claude when
 * `HERMES_COPILOT_ACP_COMMAND` reroutes it, and duplicating that heuristic in
 * TypeScript would be a second copy free to drift from the Python one.
 *
 * Scoped to THIS surface's SessionView, exactly like ModelPill — two panes side
 * by side each pick their own mode, and neither writes global config.
 */
export function PermissionModePill({ compact = false, disabled }: { compact?: boolean; disabled: boolean }) {
  const copy = useI18n().t.composer
  const view = useSessionView()
  const permission = useStore(view.$acpPermission)
  const runtimeId = useStore(view.$runtimeId)
  const [open, setOpen] = useState(false)
  const [pendingBypass, setPendingBypass] = useState<null | string>(null)

  if (!permission.available) {
    return null
  }

  const labelFor = (mode: string) => copy.permissionModeLabels[mode] ?? mode
  const describe = (mode: string) => copy.permissionModeDescriptions[mode] ?? ''
  const current = permission.value

  // No live session yet means no session-scoped RPC to send it to. The mode is
  // still SHOWN (it reports what the next chat will start on, straight from
  // config) but not editable — silently pocketing a click that never reaches a
  // backend would be worse than an honest disable.
  const noSession = !runtimeId
  const readOnly = permission.locked || noSession

  const commit = async (mode: string) => {
    if (!runtimeId || mode === current) {
      return
    }

    try {
      await setSessionPermissionMode(runtimeId, mode)
    } catch (err) {
      // The backend rejects an unadvertised id rather than silently ignoring
      // it, so surface that instead of leaving the menu looking applied. The
      // next session.info repaints the real value either way.
      notifyError(err, copy.permissionModeFailed)
    }
  }

  const onSelect = (mode: string) => {
    // bypassPermissions turns off the approval prompt for everything the agent
    // runs, shell writes included. One extra beat before that is worth it.
    if (mode === BYPASS_PERMISSION_MODE && current !== BYPASS_PERMISSION_MODE) {
      setPendingBypass(mode)

      return
    }

    void commit(mode)
  }

  const title = permission.locked
    ? copy.permissionModeLocked(labelFor(current))
    : noSession
      ? copy.permissionModeDraft(labelFor(current))
      : copy.permissionModeTitle(labelFor(current))

  const label = compact ? (
    <ShieldCheck className="size-3.5 shrink-0 opacity-70" />
  ) : (
    <>
      <span className="truncate">{labelFor(current)}</span>
      <ChevronDown className="size-2.5 shrink-0 opacity-50" />
    </>
  )

  const pillClass = compact
    ? cn(
        'size-(--composer-control-size) shrink-0 justify-center gap-0 rounded-md p-0',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      )
    : PILL

  // Closing the menu ends its claim on the keyboard — same reason ModelPill
  // does this: Radix restores focus to the trigger, so without the release the
  // Enter that picked a mode also swallows the next thing typed.
  const setMenuOpen = (next: boolean) => {
    setOpen(next)

    if (!next) {
      releaseTypingFocus()
    }
  }

  const confirmDialog = (
    <ConfirmDialog
      description={copy.permissionModeBypassWarning}
      destructive
      dismissOnConfirm
      onClose={() => setPendingBypass(null)}
      onConfirm={async () => {
        await commit(pendingBypass ?? BYPASS_PERMISSION_MODE)
      }}
      open={pendingBypass !== null}
      title={copy.permissionModeBypassTitle}
    />
  )

  if (readOnly) {
    return (
      <>
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
            {label}
          </Button>
        </Tip>
        {confirmDialog}
      </>
    )
  }

  return (
    <>
      <DropdownMenu onOpenChange={setMenuOpen} open={open}>
        <Tip label={title} side="top">
          <DropdownMenuTrigger asChild>
            <Button aria-label={title} className={pillClass} disabled={disabled} type="button" variant="ghost">
              {label}
            </Button>
          </DropdownMenuTrigger>
        </Tip>
        <DropdownMenuContent align="end" className="w-72 p-1" side="top" sideOffset={8}>
          <DropdownMenuLabel>{copy.permissionModeMenuTitle}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuRadioGroup onValueChange={onSelect} value={current}>
            {permission.options.map(mode => (
              <DropdownMenuRadioItem className="items-start gap-2" key={mode} value={mode}>
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-xs text-foreground">{labelFor(mode)}</span>
                  {describe(mode) ? (
                    <span className="text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{describe(mode)}</span>
                  ) : null}
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {confirmDialog}
    </>
  )
}
