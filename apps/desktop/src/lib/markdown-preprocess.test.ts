import { describe, expect, it } from 'vitest'

import { preprocessMarkdown } from './markdown-preprocess'

const openFenceLines = (text: string): string[] =>
  text.split('\n').filter(line => /^\s*(?:```|~~~)/.test(line))

describe('preprocessMarkdown streaming fences', () => {
  it('keeps an untagged fence intact at every streaming prefix', () => {
    // An untagged ``` opener is what a reply looks like mid-stream. Every
    // prefix from the opener onward must still render as a fenced block, or
    // the body flickers as loose prose until the closing fence lands.
    // The body carries real code signals on purpose: a signal-free body is
    // demoted to prose by design once the block closes (see the test below),
    // which would make the tail of this sweep assert the opposite.
    const full = [
      'Here you go:',
      '',
      '```',
      'const total = 1',
      'console.log(total)',
      'export default total',
      '```',
      '',
      'Done.'
    ].join('\n')
    // Start one character into the body: an opener with a still-empty body is
    // dropped on purpose, so there is no fence to assert on yet.
    const firstBodyChar = full.indexOf('```') + 5

    for (let cut = firstBodyChar; cut <= full.length; cut += 1) {
      const rendered = preprocessMarkdown(full.slice(0, cut))

      expect(openFenceLines(rendered).length, `prefix ${cut} lost its fence`).toBeGreaterThan(0)
    }
  })

  it('does not demote an untagged streaming fence to prose', () => {
    // A truncated body has no code signals yet, so the prose heuristic used to
    // strip the fence and then restore it once real code arrived.
    const streaming = ['```', 'first line of output', 'second line of output', 'third line of output'].join(
      '\n'
    )

    expect(openFenceLines(preprocessMarkdown(streaming)).length).toBeGreaterThan(0)
  })

  it('still scrubs stray backticks after a completed block', () => {
    // Regression guard: the dangling-fence pass now also matches a balanced
    // block's closing fence, and must not protect the trailing text from the
    // backtick scrubber.
    const rendered = preprocessMarkdown(['```js', 'const a = 1', '```', '', 'trailing ``` noise'].join('\n'))

    expect(rendered).toContain('const a = 1')
    expect(rendered.split('\n').at(-1)).not.toContain('```')
  })

  it('still demotes a completed untagged fence whose body has no code signals', () => {
    // Pins pre-existing behaviour the streaming fix must not undo: LLMs wrap
    // plain prose in a bare fence constantly, and once the block is closed the
    // whole body is available to judge, so the heuristic is trusted again.
    const rendered = preprocessMarkdown(
      ['```', 'This is a sentence of prose.', 'And another sentence here.', 'A third sentence too.', '```'].join(
        '\n'
      )
    )

    expect(openFenceLines(rendered).length).toBe(0)
  })

  it('leaves a tagged prose fence classified as prose', () => {
    // Fix B is scoped to untagged fences; an explicit prose tag is still
    // trusted, closed or not.
    const rendered = preprocessMarkdown(
      ['```text', 'This is a sentence of prose.', 'And another sentence here.', 'A third sentence too.'].join(
        '\n'
      )
    )

    expect(openFenceLines(rendered).length).toBe(0)
  })
})
