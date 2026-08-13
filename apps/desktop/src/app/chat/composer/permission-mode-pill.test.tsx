import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import type { AcpPermissionState } from '@/app/types'
import { EMPTY_ACP_PERMISSION } from '@/lib/acp-permission'

import { PermissionModePill } from './permission-mode-pill'

const setSessionPermissionMode = vi.hoisted(() => vi.fn(async () => 'ok'))

vi.mock('@/lib/acp-permission', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/acp-permission')>()),
  setSessionPermissionMode
}))

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

const OPTIONS = ['default', 'plan', 'acceptEdits', 'bypassPermissions']

const permission = (over: Partial<AcpPermissionState> = {}): AcpPermissionState => ({
  available: true,
  locked: false,
  options: OPTIONS,
  source: 'config',
  value: 'default',
  ...over
})

function renderPill(state: AcpPermissionState, runtimeId: null | string = 'runtime-1') {
  const view: SessionView = {
    kind: 'tile',
    $acpPermission: atom(state),
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
    $storedId: atom(null)
  }

  return render(
    <SessionViewProvider value={view}>
      <PermissionModePill disabled={false} />
    </SessionViewProvider>
  )
}

/** Radix opens on pointerDown, not click — same as the statusbar menu tests. */
function openMenu() {
  fireEvent.pointerDown(screen.getByRole('button', { name: /Permission mode/ }), { button: 0 })
}

afterEach(() => {
  cleanup()
  setSessionPermissionMode.mockReset()
  setSessionPermissionMode.mockResolvedValue('ok')
})

// The gate. `copilot-acp` is a GENERIC provider — it is Claude only because
// this machine reroutes it — so the backend decides availability and the pill
// must obey it rather than guessing from the provider slug.
describe('PermissionModePill visibility gate', () => {
  it('renders nothing when the backend says the session is not Claude over ACP', () => {
    const { container } = renderPill(EMPTY_ACP_PERMISSION)

    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for an available:false payload that still carries a value', () => {
    // Negative control for the assertion above: the pass there must come from
    // `available`, not merely from the state being empty.
    const { container } = renderPill(permission({ available: false, value: 'plan' }))

    expect(container.firstChild).toBeNull()
  })

  it('renders the current mode when available', () => {
    renderPill(permission({ value: 'plan' }))

    expect(screen.getByRole('button', { name: /Permission mode: Plan/ })).toBeTruthy()
  })

  it('falls back to the raw id for a mode it has no label for', () => {
    renderPill(permission({ options: ['weirdMode'], value: 'weirdMode' }))

    expect(screen.getByRole('button', { name: /Permission mode: weirdMode/ })).toBeTruthy()
  })
})

describe('PermissionModePill switching', () => {
  it('sends the picked mode for THIS surface only', async () => {
    renderPill(permission({ value: 'default' }), 'runtime-A')

    openMenu()
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /Plan/ }))

    await waitFor(() => expect(setSessionPermissionMode).toHaveBeenCalledWith('runtime-A', 'plan'))
  })

  it('does not re-send the mode that is already active', async () => {
    renderPill(permission({ value: 'plan' }))

    openMenu()
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /Plan/ }))

    expect(setSessionPermissionMode).not.toHaveBeenCalled()
  })

  it('requires a confirm before bypassPermissions, and sends nothing until confirmed', async () => {
    renderPill(permission({ value: 'default' }))

    openMenu()
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /Bypass/ }))

    // The dialog is open and NOTHING has been sent yet — that gap is the point.
    expect(await screen.findByText(/Skip all approval prompts\?/)).toBeTruthy()
    expect(setSessionPermissionMode).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(setSessionPermissionMode).toHaveBeenCalledWith('runtime-1', 'bypassPermissions'))
  })

  it('sends nothing when the bypass confirm is cancelled', async () => {
    renderPill(permission({ value: 'default' }))

    openMenu()
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /Bypass/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(setSessionPermissionMode).not.toHaveBeenCalled()
  })

  it('offers only the modes the backend reported, not a hardcoded list', async () => {
    renderPill(permission({ options: ['default', 'plan'], value: 'default' }))

    openMenu()

    expect(await screen.findByRole('menuitemradio', { name: /Plan/ })).toBeTruthy()
    expect(screen.queryByRole('menuitemradio', { name: /Accept edits/ })).toBeNull()
    expect(screen.queryByRole('menuitemradio', { name: /Bypass/ })).toBeNull()
  })
})

describe('PermissionModePill read-only states', () => {
  it('does not open a menu when an operator pinned the mode via env', () => {
    renderPill(permission({ locked: true, source: 'env', value: 'plan' }))

    const button = screen.getByRole('button', { name: /pinned by HERMES_ACP_PERMISSION_MODE/ })

    fireEvent.pointerDown(button, { button: 0 })
    fireEvent.click(button)

    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(setSessionPermissionMode).not.toHaveBeenCalled()
  })

  it('shows but does not edit on a draft with no session to scope the change to', () => {
    renderPill(permission({ value: 'plan' }), null)

    const button = screen.getByRole('button', { name: /send a message to change it/ })

    fireEvent.pointerDown(button, { button: 0 })
    fireEvent.click(button)

    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(setSessionPermissionMode).not.toHaveBeenCalled()
  })

  it('IS interactive with a session and no lock (control for the two above)', async () => {
    renderPill(permission({ value: 'default' }), 'runtime-live')

    openMenu()

    expect(await screen.findByRole('menuitemradio', { name: /Plan/ })).toBeTruthy()
  })
})
