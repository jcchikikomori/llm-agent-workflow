import { describe, expect, mock, test } from 'claude-code/testing'

import type { EtaRun } from '../types'
import {
  backgroundIdOf,
  bar,
  clock,
  describeRun,
  estimate,
  isOverdue,
  isStale,
  median,
  parseHint,
  parseNotices,
  remember,
  signature,
  stripHint,
  touch,
} from '../hooks/eta'

const T0 = 1_000_000

const BAND = {
  plugin: 'eta',
  surface: 'terminal' as const,
  component: 'AbovePrompt' as const,
  props: {
    hasSurvey: false,
    isWorking: true,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 9 },
    view: {},
  },
}

const BASH_OK = { result: { stdout: '', stderr: '', interrupted: false } }

function run(over: Partial<EtaRun> = {}): EtaRun {
  return {
    id: 'toolu_1',
    label: 'Run model specs',
    key: 'Bash:bundle exec rspec',
    startedAt: T0,
    etaMs: 180_000,
    basis: 'history',
    samples: 4,
    ...over,
  }
}

describe('parseHint', () => {
  test('reads minutes, seconds, hours and mixed forms', () => {
    expect(parseHint('Run model specs ~3m')).toBe(180_000)
    expect(parseHint('Explore auth flow (~90s)')).toBe(90_000)
    expect(parseHint('Build image ~1.5h')).toBe(5_400_000)
    expect(parseHint('Full suite ~2m30s')).toBe(150_000)
    expect(parseHint('Deploy ~4 min')).toBe(240_000)
  })

  test('ignores text with no usable hint', () => {
    expect(parseHint(undefined)).toBeNull()
    expect(parseHint('List files')).toBeNull()
    expect(parseHint('Read ~/notes.md')).toBeNull()
    expect(parseHint('Compare ~5 sessions')).toBeNull()
    expect(parseHint('Too long ~48h')).toBeNull()
  })

  test('finds the hint after a home path', () => {
    expect(parseHint('Copy ~/a to ~/b ~45s')).toBe(45_000)
  })

  test('strips the hint, and the parens around it, from the label', () => {
    expect(stripHint('Run model specs ~3m')).toBe('Run model specs')
    expect(stripHint('Explore auth flow (~90s)')).toBe('Explore auth flow')
    expect(stripHint('Read ~/notes.md')).toBe('Read ~/notes.md')
  })
})

describe('estimate', () => {
  test('history median wins once a strong key has two runs', () => {
    // sorted [172, 181, 190, 240] s -> (181 + 190) / 2 = 185.5 s
    const samples = [172_000, 190_000, 181_000, 240_000]
    expect(median(samples)).toBe(185_500)
    expect(estimate(samples, 60_000, false)).toEqual({ etaMs: 185_500, basis: 'history', samples: 4 })
  })

  test("Claude's hint beats a lone run and a weak key's history", () => {
    expect(estimate([30_000], 90_000, false)).toEqual({ etaMs: 90_000, basis: 'guess', samples: 0 })
    expect(estimate([30_000, 40_000, 50_000], 90_000, true)).toEqual({ etaMs: 90_000, basis: 'guess', samples: 0 })
  })

  test('falls back to a lone run, then to nothing', () => {
    expect(estimate([30_000], null, false)).toEqual({ etaMs: 30_000, basis: 'history', samples: 1 })
    expect(estimate([], null, false)).toEqual({ etaMs: null, basis: 'none', samples: 0 })
  })

  test('keeps the last seven runs', () => {
    expect(remember([1, 2, 3, 4, 5, 6, 7], 8.4)).toEqual([2, 3, 4, 5, 6, 7, 8])
  })

  test('evicts the least recently used signature past the cap', () => {
    expect(touch(['h:a', 'h:b', 'h:c'], 'h:a', 3)).toEqual({ index: ['h:b', 'h:c', 'h:a'], evicted: [] })
    expect(touch(['h:a', 'h:b', 'h:c'], 'h:d', 3)).toEqual({ index: ['h:b', 'h:c', 'h:d'], evicted: ['h:a'] })
  })
})

describe('signature', () => {
  test('files Bash by its whitespace-normalised command, labelled by its description', () => {
    const sig = signature({ tool: 'Bash', command: '  bundle   exec rspec\n spec/models ', description: 'Specs ~3m' })
    expect(sig).toEqual({ key: 'Bash:bundle exec rspec spec/models', label: 'Specs', isWeak: false })
  })

  test('labels Bash by its command when it has no description', () => {
    expect(signature({ tool: 'Bash', command: 'make build' }).label).toBe('make build')
  })

  test('files agents by type, as a weak key', () => {
    const sig = signature({ tool: 'Agent', subagent_type: 'Explore', description: 'Map auth flow ~2m' })
    expect(sig).toEqual({ key: 'Agent:Explore', label: 'Explore: Map auth flow', isWeak: true })
  })

  test('files workflows by name, from the input or the script meta', () => {
    expect(signature({ tool: 'Workflow', name: 'review-changes' }).key).toBe('Workflow:review-changes')
    expect(signature({ tool: 'Workflow', script: "export const meta = { name: 'scan' }" }).key).toBe('Workflow:scan')
  })

  test('shortens MCP tool names in the label', () => {
    expect(signature({ tool: 'mcp__plugin_buildkite__wait_for_build' }).label).toBe('buildkite__wait_for_build')
  })
})

describe('band line', () => {
  test('formats clocks and bars', () => {
    expect(clock(185_500)).toBe('3:06')
    expect(clock(3_729_000)).toBe('1:02:09')
    expect(bar(0.5, 4)).toBe('██░░')
    expect(bar(2, 4)).toBe('████')
  })

  test('shows elapsed / ETA, the bar and the basis while on time', () => {
    const line = describeRun(run(), T0 + 72_000, 100)
    expect(line.tone).toBe('normal')
    expect(line.text).toContain('▸ Run model specs')
    expect(line.text).toContain('1:12 / ~3:00')
    expect(line.text).toContain('hist×4')
  })

  test('turns warn past the ETA and over past 150%', () => {
    expect(describeRun(run(), T0 + 200_000, 100).tone).toBe('warn')

    const over = describeRun(run({ bgId: 'b1' }), T0 + 280_000, 100)
    expect(over.tone).toBe('over')
    expect(over.text).toContain('(bg)')
    expect(over.text).toContain('+1:40 over')
  })

  test('shows elapsed only when there is no estimate', () => {
    const line = describeRun(run({ etaMs: null, basis: 'none', samples: 0 }), T0 + 5_000, 100)
    expect(line.tone).toBe('none')
    expect(line.text).toContain('0:05  no estimate yet')
  })

  test('truncates a long label to fit the band', () => {
    const line = describeRun(run({ label: 'x'.repeat(300) }), T0 + 5_000, 80)
    expect(line.text.length).toBeLessThanOrEqual(80)
    expect(line.text).toContain('…')
  })
})

describe('alerts and cleanup', () => {
  test('alerts once past 150% and at least 15 s over', () => {
    expect(isOverdue(run(), T0 + 269_000)).toBe(false)
    expect(isOverdue(run(), T0 + 270_000)).toBe(true)
    expect(isOverdue(run({ alerted: true }), T0 + 270_000)).toBe(false)
    // 10 s ETA: 150% is 15 s, but only 5 s over, so no alert yet
    expect(isOverdue(run({ etaMs: 10_000 }), T0 + 15_000)).toBe(false)
    expect(isOverdue(run({ etaMs: null }), T0 + 9_999_999)).toBe(false)
  })

  test('drops runs whose end never arrived', () => {
    expect(isStale(run({ etaMs: 60_000 }), T0 + 14 * 60_000)).toBe(false)
    expect(isStale(run({ etaMs: 60_000 }), T0 + 16 * 60_000)).toBe(true)
    expect(isStale(run({ etaMs: null, bgId: 'b1' }), T0 + 60 * 60_000)).toBe(false)
    expect(isStale(run({ etaMs: null, bgId: 'b1' }), T0 + 121 * 60_000)).toBe(true)
  })
})

describe('background ids and notifications', () => {
  test('reads the id a call answered with when it went to the background', () => {
    expect(backgroundIdOf('Bash', { stdout: '', backgroundTaskId: 'b7' })).toBe('b7')
    expect(backgroundIdOf('Agent', { status: 'async_launched', agentId: 'a3' })).toBe('a3')
    expect(backgroundIdOf('Workflow', { status: 'async_launched', taskId: 'w9' })).toBe('w9')
    expect(backgroundIdOf('Bash', { stdout: 'done', interrupted: false })).toBeUndefined()
    expect(backgroundIdOf('Agent', { status: 'completed', agentId: 'a3' })).toBeUndefined()
    expect(backgroundIdOf('Bash', undefined)).toBeUndefined()
  })

  test('parses every notification in a row', () => {
    const text = [
      '<task-notification><task-id>b7</task-id><status>completed</status></task-notification>',
      '<task-notification><task-id>a3</task-id><tool-use-id>toolu_9</tool-use-id>',
      '<status>killed</status></task-notification>',
    ].join('\n')
    expect(parseNotices(text)).toEqual([
      { taskId: 'b7', toolUseId: undefined, status: 'completed' },
      { taskId: 'a3', toolUseId: 'toolu_9', status: 'killed' },
    ])
    expect(parseNotices('no notice here')).toEqual([])
  })
})

describe('in a session', () => {
  test("a foreground call shows Claude's hint, then its own history on the next run", async ($, on) => {
    const time = mock.clock(on, { now: T0 })
    mock.store(on)
    on('tool.call', { tool: 'Bash' }, async () => {
      await time.sleep(20_000)

      return BASH_OK
    })

    const first = $.tool.call({ tool: 'Bash', command: 'sleep 20', description: 'Nap ~20s' })
    await time.advance(5_000)
    const firstBand = await $.ui.mount(BAND)
    const firstLine = await firstBand.find({ type: 'Text', text: /Nap/ })
    expect(firstLine?.text).toContain('0:05 / ~0:20')
    expect(firstLine?.text).toContain('guess')
    await firstBand.unmount()
    await time.advance(15_000)
    await first

    const second = $.tool.call({ tool: 'Bash', command: 'sleep   20', description: 'Nap again' })
    await time.advance(5_000)
    const secondBand = await $.ui.mount(BAND)
    const secondLine = await secondBand.find({ type: 'Text', text: /Nap again/ })
    expect(secondLine?.text).toContain('0:05 / ~0:20')
    expect(secondLine?.text).toContain('hist×1')
    await secondBand.unmount()
    await time.advance(15_000)
    await second
  })

  test('a quick call never reaches the band', async ($, on) => {
    const time = mock.clock(on, { now: T0 })
    mock.store(on)
    on('tool.call', { tool: 'Bash' }, async () => {
      await time.sleep(1_000)

      return BASH_OK
    })
    on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
      const { Text } = $.ui.resolve(e)

      return <Text>idle</Text>
    })

    const quick = $.tool.call({ tool: 'Bash', command: 'ls', description: 'List' })
    await time.advance(1_000)
    await quick
    const band = await $.ui.mount(BAND)
    expect(await band.find({ type: 'Text', text: /idle/ })).toBeDefined()
    expect(await band.find({ type: 'Text', text: /List/ })).toBeUndefined()
    await band.unmount()
  })

  test('a background shell stays on the band until its notification arrives', async ($, on) => {
    const time = mock.clock(on, { now: T0 })
    mock.store(on)
    on('tool.call', { tool: 'Bash' }, () => ({
      result: { stdout: '', stderr: '', interrupted: false, backgroundTaskId: 'b42' },
    }))
    on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
      const { Text } = $.ui.resolve(e)

      return <Text>idle</Text>
    })

    await $.tool.call({ tool: 'Bash', command: 'sleep 40', description: 'Long nap ~40s', run_in_background: true })
    await time.advance(10_000)
    const band = await $.ui.mount(BAND)
    expect((await band.find({ type: 'Text', text: /Long nap/ }))?.text).toContain('(bg)')

    await $.session.append({
      door: 'prompt',
      origin: { kind: 'task-notification' },
      uuid: 'row-1',
      message: {
        type: 'user',
        role: 'user',
        content: [
          { type: 'text', text: '<task-notification><task-id>b42</task-id><status>completed</status></task-notification>' },
        ],
      },
    })
    await time.settle()
    expect(await band.find({ type: 'Text', text: /Long nap/ })).toBeUndefined()
    await band.unmount()
  })

  test('an overrun raises one toast and one notification', async ($, on) => {
    const time = mock.clock(on, { now: T0 })
    mock.store(on, { 'h:Bash:make build': [20_000, 20_000] })
    const toasts: string[] = []
    const notes: string[] = []
    on('session.start', () => ({ cwd: '/tmp' }))
    on('ui.toast', ($, e) => {
      toasts.push(e.text)

      return { value: undefined }
    })
    on('ui.notify', ($, e) => {
      notes.push(e.text)

      return { value: { isSent: true as const, channel: 'terminal_bell' } }
    })
    on('tool.call', { tool: 'Bash' }, async () => {
      await time.sleep(60_000)

      return BASH_OK
    })

    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    const build = $.tool.call({ tool: 'Bash', command: 'make build', description: 'Build' })
    // ETA 20 s from history: the alert is due at 35 s (150% = 30 s, and 15 s over)
    await time.advance(34_000)
    expect(toasts).toEqual([])
    await time.advance(2_000)
    expect(toasts).toHaveLength(1)
    expect(toasts[0]).toContain('Build is 0:1')
    expect(notes).toHaveLength(1)
    await time.advance(10_000)
    expect(toasts).toHaveLength(1)
    await time.advance(14_000)
    await build
  })

  test("adds the hint section to Claude's system prompt", async ($, on) => {
    on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'hello', scope: 'shared' as const }] }))

    const composed = await $.prompt.compose({
      model: 'claude-opus-5-5',
      promptModel: 'claude-opus-5-5',
      surfaces: ['terminal'],
      tools: ['Bash'],
      outputStyle: null,
      traits: [],
    })
    expect(composed.sections.map(section => section.id)).toEqual(['intro', 'eta:hint'])
    expect(composed.sections[1]?.text).toContain('~<n>m')
  })
})
