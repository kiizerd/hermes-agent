import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import type { AcpSystemPromptModeState } from '@/app/types'
import { EMPTY_ACP_PERMISSION } from '@/lib/acp-permission'
import { EMPTY_ACP_SYSTEM_PROMPT_MODE } from '@/lib/acp-system-prompt-mode'
import { $draftAcpSystemPromptMode, setDraftAcpSystemPromptMode } from '@/store/session'

import { BridgeModePill } from './bridge-mode-pill'

const setSessionSystemPromptMode = vi.hoisted(() => vi.fn(async () => 'ok'))

vi.mock('@/lib/acp-system-prompt-mode', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/acp-system-prompt-mode')>()),
  setSessionSystemPromptMode
}))

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
})

const mode = (over: Partial<AcpSystemPromptModeState> = {}): AcpSystemPromptModeState => ({
  available: true,
  locked: false,
  options: ['bridge', 'native'],
  source: 'config',
  value: 'bridge',
  ...over
})

function renderPill(state: AcpSystemPromptModeState, runtimeId: null | string = 'runtime-1') {
  const view: SessionView = {
    kind: 'tile',
    $acpPermission: atom(EMPTY_ACP_PERMISSION),
    $acpSystemPromptMode: atom(state),
    $awaitingResponse: atom(false),
    $busy: atom(false),
    $cwd: atom(''),
    $fast: atom(false),
    $lastVisibleIsUser: atom(false),
    $messages: atom([]),
    $messagesEmpty: atom(true),
    $model: atom(''),
    $provider: atom(''),
    $reasoningEffort: atom(''),
    $runtimeId: atom(runtimeId),
    $storedId: atom(null),
    $turnStartedAt: atom(null)
  }

  return render(
    <SessionViewProvider value={view}>
      <BridgeModePill disabled={false} />
    </SessionViewProvider>
  )
}

const pill = () => screen.getByRole('button')

afterEach(() => {
  cleanup()
  setSessionSystemPromptMode.mockReset()
  setSessionSystemPromptMode.mockResolvedValue('ok')
  setDraftAcpSystemPromptMode('')
})

// Same backend-decided gate as PermissionModePill: `copilot-acp` is a GENERIC
// provider and is only Claude because this machine reroutes it, so the pill
// must obey `available` rather than guessing from the provider slug.
describe('BridgeModePill visibility gate', () => {
  it('renders nothing when the backend says the session is not Claude over ACP', () => {
    const { container } = renderPill(EMPTY_ACP_SYSTEM_PROMPT_MODE)

    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for an available:false payload that still carries a value', () => {
    // Negative control: the pass above must come from `available`, not merely
    // from the state being empty.
    const { container } = renderPill(mode({ available: false, value: 'native' }))

    expect(container.firstChild).toBeNull()
  })

  it('labels itself from the current mode', () => {
    renderPill(mode({ value: 'native' }))

    expect(pill().textContent).toBe('Native')
  })
})

// The regression this whole file exists for. The draft is the ONLY window in
// which the pick can still reach `session/new`, so a pill that is read-only
// there is a pill that can never be used: by the time a runtime id exists the
// backend has almost always already opened the ACP session and locked it.
describe('BridgeModePill draft window', () => {
  it('is editable with no runtime id', () => {
    renderPill(mode(), null)

    expect(pill().getAttribute('aria-disabled')).toBeNull()
  })

  it('parks the pick in the sticky draft store instead of calling the gateway', () => {
    renderPill(mode(), null)

    fireEvent.click(pill())

    expect($draftAcpSystemPromptMode.get()).toBe('native')
    expect(setSessionSystemPromptMode).not.toHaveBeenCalled()
  })

  it('renders the draft pick over the previous session mirror', () => {
    // Nothing clears `acpSystemPromptMode` on New Chat — that staleness is
    // what keeps `available` true for a draft at all — so the draft store has
    // to win once the user has actually picked.
    setDraftAcpSystemPromptMode('native')
    renderPill(mode({ value: 'bridge' }), null)

    expect(pill().textContent).toBe('Native')
  })

  it('toggles back off the draft pick', () => {
    setDraftAcpSystemPromptMode('native')
    renderPill(mode({ value: 'bridge' }), null)

    fireEvent.click(pill())

    // Explicit "bridge", not empty: it has to be able to override a config
    // default of `native`, which an empty store would defer to.
    expect($draftAcpSystemPromptMode.get()).toBe('bridge')
  })
})

describe('BridgeModePill live session', () => {
  it('sends the gateway RPC while the backend still reports unlocked', async () => {
    renderPill(mode({ locked: false }))

    fireEvent.click(pill())

    await waitFor(() => expect(setSessionSystemPromptMode).toHaveBeenCalledWith('runtime-1', 'native'))
  })

  it('keeps the sticky draft in step so the next new chat follows the pick', async () => {
    renderPill(mode({ locked: false }))

    fireEvent.click(pill())

    await waitFor(() => expect($draftAcpSystemPromptMode.get()).toBe('native'))
  })

  it('is read-only once the backend reports the session locked', () => {
    renderPill(mode({ locked: true }))

    expect(pill().getAttribute('aria-disabled')).not.toBeNull()

    fireEvent.click(pill())

    expect(setSessionSystemPromptMode).not.toHaveBeenCalled()
  })

  it('explains WHY it is locked rather than going flatly disabled', () => {
    // A `disabled` button drops its tooltip, and the tooltip is the only
    // place the reason appears.
    renderPill(mode({ locked: true, value: 'native' }))

    expect(pill().hasAttribute('disabled')).toBe(false)
    expect(pill().getAttribute('aria-label')).toMatch(/locked/i)
  })
})
