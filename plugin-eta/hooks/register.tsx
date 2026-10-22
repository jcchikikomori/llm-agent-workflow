import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { EtaRun } from '../types'
import {
  HINT_PROMPT,
  MAX_LINES,
  RECORD_MIN_MS,
  SKIP_TOOLS,
  backgroundIdOf,
  describeRun,
  estimate,
  isOverdue,
  isStale,
  isVisible,
  overrunMessage,
  parseHint,
  parseNotices,
  remember,
  signature,
  touch,
} from './eta'
import type { ToolLike, Tone } from './eta'

type Engine = EngineInterface

const running = atom({ plugin: 'eta', key: 'running' } as const, [])

/** Rows that carry the model's or a tool's words: a notification never comes in by these. */
const NOT_NOTICES: ReadonlySet<string> = new Set(['tool-result', 'tool-message', 'response'])

const TONE_STYLE: Record<Tone, { color?: 'warning' | 'error'; dimColor?: true }> = {
  normal: {},
  warn: { color: 'warning' },
  over: { color: 'error' },
  none: { dimColor: true },
}

async function loadSamples($: Engine, key: string): Promise<number[]> {
  const stored = await $.store.get(`h:${key}`)

  return Array.isArray(stored) ? stored.filter((n): n is number => typeof n === 'number') : []
}

async function recordSample($: Engine, key: string, ms: number): Promise<void> {
  if (ms < RECORD_MIN_MS) {
    return
  }

  const storeKey = `h:${key}`
  await $.store.set(storeKey, remember(await loadSamples($, key), ms))

  const stored = await $.store.get('idx')
  const { index, evicted } = touch(Array.isArray(stored) ? stored.map(String) : [], storeKey)
  await $.store.set('idx', index)

  for (const old of evicted) {
    await $.store.delete(old)
  }
}

/** Takes the matching run off the band and, when it ended well, files how long it took. */
async function finish($: Engine, matches: (run: EtaRun) => boolean, isOk: boolean): Promise<void> {
  const hit = (await read($, running)).find(matches)
  if (hit === undefined) {
    return
  }

  await update($, running, list => list.filter(run => run.id !== hit.id))

  if (isOk) {
    await recordSample($, hit.key, (await $.clock.now()) - hit.startedAt)
  }
}

/** Once a second while anything runs: redraw, drop runs whose end got lost, alert on overruns. */
async function tick($: Engine): Promise<void> {
  const list = await read($, running)
  if (list.length === 0) {
    return
  }

  const now = await $.clock.now()
  $.ui.invalidate('ui.render')

  const due = list.filter(run => isOverdue(run, now))
  const stale = list.filter(run => isStale(run, now))
  if (due.length === 0 && stale.length === 0) {
    return
  }

  const dueIds = new Set(due.map(run => run.id))
  const staleIds = new Set(stale.map(run => run.id))
  await update($, running, current =>
    current
      .filter(run => !staleIds.has(run.id))
      .map(run => (dueIds.has(run.id) ? { ...run, alerted: true } : run)),
  )

  for (const run of due) {
    const message = overrunMessage(run, now)
    $.ui.toast(message, { timeoutMs: 8000 })
    await $.ui.notify(message, { title: 'Claude Code: ETA overrun' }).catch(() => undefined)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    $.clock.every(1000, () => {
      void tick($).catch(() => undefined)
    })

    return next(e)
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)

    return { sections: [...composed.sections, { id: 'eta:hint', text: HINT_PROMPT, scope: 'session' }] }
  })

  on('tool.call', async ($, e, next) => {
    if (SKIP_TOOLS.has(e.tool)) {
      return next(e)
    }

    const call = e as unknown as ToolLike
    const sig = signature(call)
    const startedAt = await $.clock.now()
    // A subagent's own calls feed the history but stay off the band: the agent's line covers them.
    const isShown = e.agentId === undefined

    if (isShown) {
      const est = estimate(await loadSamples($, sig.key), parseHint(call.description), sig.isWeak)
      const run: EtaRun = {
        id: e.tool_use_id,
        label: sig.label,
        key: sig.key,
        startedAt,
        etaMs: est.etaMs,
        basis: est.basis,
        samples: est.samples,
      }
      await update($, running, list => [...list.filter(one => one.id !== run.id), run])
    }

    let bgId: string | undefined
    try {
      const ran = await next(e)
      bgId = backgroundIdOf(e.tool, ran.result)

      const isClean = ran.deny === undefined && ran.isError !== true && bgId === undefined
      const isInterrupted = (ran.result as { interrupted?: unknown } | undefined)?.interrupted === true
      if (isClean && !isInterrupted) {
        await recordSample($, sig.key, (await $.clock.now()) - startedAt)
      }

      return ran
    } finally {
      if (isShown) {
        await update($, running, list =>
          bgId === undefined
            ? list.filter(run => run.id !== e.tool_use_id)
            : list.map(run => (run.id === e.tool_use_id ? { ...run, bgId } : run)),
        )
      }
    }
  }).catch(($, e, next) => next(e))

  // A background shell, agent or workflow ends with a <task-notification> row.
  on('session.append', async ($, e, next) => {
    const stored = await next(e)

    if (!NOT_NOTICES.has(e.door)) {
      const text = e.message.content
        .map(block => (block.type === 'text' && typeof block.text === 'string' ? block.text : ''))
        .join('\n')

      for (const notice of text.includes('<task-notification>') ? parseNotices(text) : []) {
        await finish(
          $,
          run =>
            (notice.taskId !== undefined && run.bgId === notice.taskId) ||
            (notice.toolUseId !== undefined && run.id === notice.toolUseId),
          notice.status === 'completed',
        )
      }
    }

    return stored
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)

    if (e.agentId !== undefined) {
      // A background agent's loop ended: the same end its notification reports, whichever lands first.
      await finish($, run => run.bgId === e.agentId, !e.isAborted)
    } else {
      // The main turn ended, so no foreground call of it can still be running.
      await update($, running, list => list.filter(run => run.bgId !== undefined))
    }

    return done
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) {
      return next(e)
    }

    const now = await $.clock.now()
    const shown = (await read($, running)).filter(run => isVisible(run, now))
    if (shown.length === 0) {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const room = Math.max(1, Math.min(MAX_LINES, e.props.maxRows - 1))
    const lines = shown.slice(0, room).map(run => describeRun(run, now, e.props.bodyColumns))

    return (
      <Box flexDirection="column">
        {lines.map(line => (
          <Text {...TONE_STYLE[line.tone]} wrap="truncate-end">
            {line.text}
          </Text>
        ))}
        {shown.length > room && <Text dimColor>+{shown.length - room} more running</Text>}
      </Box>
    )
  })
}
