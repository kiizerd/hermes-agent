import { describe, expect, it } from 'vitest'

import { acpPermissionEquals, EMPTY_ACP_PERMISSION, normalizeAcpPermission } from './acp-permission'

describe('normalizeAcpPermission', () => {
  it('accepts a well-formed available payload', () => {
    expect(
      normalizeAcpPermission({
        available: true,
        locked: false,
        options: ['default', 'plan'],
        source: 'session',
        value: 'plan'
      })
    ).toEqual({
      available: true,
      locked: false,
      options: ['default', 'plan'],
      source: 'session',
      value: 'plan'
    })
  })

  // Defaulting to UNAVAILABLE is the load-bearing direction: an older backend
  // sends no block at all, and defaulting the other way would paint a dropdown
  // whose clicks that backend would reject.
  it.each([
    ['undefined', undefined],
    ['null', null],
    ['a string', 'plan'],
    ['a number', 3],
    ['an empty object', {}],
    ['available:false', { available: false, value: 'plan' }],
    ['available as a truthy string', { available: 'yes', value: 'plan' }],
    ['available as 1', { available: 1, value: 'plan' }]
  ])('reports unavailable for %s', (_label, input) => {
    expect(normalizeAcpPermission(input)).toEqual(EMPTY_ACP_PERMISSION)
  })

  it('drops non-string and empty option entries', () => {
    const state = normalizeAcpPermission({
      available: true,
      options: ['default', 3, null, '', 'plan', { id: 'nope' }],
      value: 'plan'
    })

    expect(state.options).toEqual(['default', 'plan'])
  })

  it('tolerates a missing options array', () => {
    expect(normalizeAcpPermission({ available: true, value: 'plan' }).options).toEqual([])
  })

  it('coerces non-string value/source to empty rather than rendering [object Object]', () => {
    const state = normalizeAcpPermission({ available: true, source: 7, value: { mode: 'plan' } })

    expect(state.value).toBe('')
    expect(state.source).toBe('')
  })

  it('treats locked as strictly boolean true', () => {
    expect(normalizeAcpPermission({ available: true, locked: 'yes' }).locked).toBe(false)
    expect(normalizeAcpPermission({ available: true, locked: true }).locked).toBe(true)
  })
})

describe('acpPermissionEquals', () => {
  const base = { available: true, locked: false, options: ['default', 'plan'], source: 'config', value: 'plan' }

  it('is true for a structurally identical state', () => {
    expect(acpPermissionEquals(base, { ...base, options: ['default', 'plan'] })).toBe(true)
  })

  it.each([
    ['value', { value: 'default' }],
    ['source', { source: 'session' }],
    ['locked', { locked: true }],
    ['available', { available: false }],
    ['option order', { options: ['plan', 'default'] }],
    ['option count', { options: ['default'] }]
  ])('is false when %s differs', (_label, over) => {
    expect(acpPermissionEquals(base, { ...base, ...over })).toBe(false)
  })
})
