import type { EtaBasis, EtaRun } from '../types'

/** A run shows on the band once it has run this long. */
export const SHOW_AFTER_MS = 3_000
/** Runs shorter than this are not filed, so quick commands never crowd out long ones. */
export const RECORD_MIN_MS = 2_000
/** Past runs kept per signature; the estimate is their median. */
export const HISTORY_SIZE = 7
/** Signatures kept in the store; the least recently used leave first. */
export const HISTORY_KEYS_CAP = 300
export const MAX_LINES = 4
export const BAR_WIDTH = 12
/** The alert fires past this share of the ETA... */
export const ALERT_RATIO = 1.5
/** ...and only once the run is at least this far over it. */
export const ALERT_MIN_OVER_MS = 15_000
const STALE_FG_MS = 15 * 60_000
const STALE_BG_MS = 30 * 60_000
const STALE_BG_NO_ETA_MS = 2 * 60 * 60_000
const HINT_MAX_MS = 24 * 60 * 60_000

/** Tools that wait on the person or run open-ended, where an ETA means nothing. */
export const SKIP_TOOLS: ReadonlySet<string> = new Set([
  'AskUserQuestion',
  'EnterPlanMode',
  'ExitPlanMode',
  'Monitor',
  'ScheduleWakeup',
])

export const HINT_PROMPT = [
  'ETA hints: when a Bash, Agent or Workflow call will likely run longer than 30 seconds, end its',
  '`description` with your estimate as `~<n>s`, `~<n>m` or `~<n>h` (for example `Run model specs ~3m`).',
  'The person watches a live countdown built from it and from past runs of the same command,',
  'so give one honest number, not a range. Leave it off quick calls.',
].join(' ')

export type ToolLike = { readonly tool: string } & Readonly<Record<string, unknown>>
export type Signature = { key: string; label: string; isWeak: boolean }
export type Estimate = { etaMs: number | null; basis: EtaBasis; samples: number }
export type TaskNotice = { taskId?: string; toolUseId?: string; status?: string }
export type Tone = 'normal' | 'warn' | 'over' | 'none'
export type RunLine = { text: string; tone: Tone }

const HINT_RE =
  /~\s*(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+(?:\.\d+)?)\s*m(?:in(?:utes?|s)?)?)?\s*(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?|s)?)?)?(?![a-z])/gi

/** Reads Claude's `~3m` / `~90s` / `~1h30m` hint out of a description; null when none. */
export function parseHint(text: unknown): number | null {
  if (typeof text !== 'string' || !text.includes('~')) {
    return null
  }

  for (const [, hours, minutes, seconds] of text.matchAll(HINT_RE)) {
    if (hours === undefined && minutes === undefined && seconds === undefined) {
      continue
    }

    const ms = (Number(hours ?? 0) * 3600 + Number(minutes ?? 0) * 60 + Number(seconds ?? 0)) * 1000
    if (ms >= 1000 && ms <= HINT_MAX_MS) {
      return Math.round(ms)
    }
  }

  return null
}

/** The description without its hint, so the label does not repeat the ETA. */
export function stripHint(text: string): string {
  return text
    .replace(HINT_RE, match => (/\d/.test(match) ? '' : match))
    .replace(/\(\s*\)/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * Files a call under a key that a later run of the same work hits again.
 * A weak key (an agent type, an MCP tool) lumps together work of very different
 * sizes, so Claude's hint beats its history.
 */
export function signature(e: ToolLike): Signature {
  const description = typeof e.description === 'string' ? stripHint(e.description) : ''

  switch (e.tool) {
    case 'Bash': {
      const command = typeof e.command === 'string' ? e.command.replace(/\s+/g, ' ').trim() : ''

      return { key: `Bash:${command.slice(0, 200)}`, label: description || command, isWeak: false }
    }
    case 'Agent': {
      const type = typeof e.subagent_type === 'string' && e.subagent_type ? e.subagent_type : 'general-purpose'

      return { key: `Agent:${type}`, label: description ? `${type}: ${description}` : type, isWeak: true }
    }
    case 'Workflow': {
      const name = workflowName(e)

      return { key: `Workflow:${name}`, label: `workflow ${name}`, isWeak: false }
    }
    default: {
      const tool = e.tool.replace(/^mcp__(?:plugin_)?/, '')

      return { key: `Tool:${e.tool}`, label: description ? `${tool}: ${description}` : tool, isWeak: true }
    }
  }
}

function workflowName(e: ToolLike): string {
  if (typeof e.name === 'string' && e.name) {
    return e.name
  }

  if (typeof e.script === 'string') {
    const name = /name:\s*['"]([^'"]+)['"]/.exec(e.script)?.[1]
    if (name) {
      return name
    }
  }

  if (typeof e.scriptPath === 'string' && e.scriptPath) {
    return e.scriptPath.split('/').pop() || e.scriptPath
  }

  return 'inline'
}

export function median(xs: readonly number[]): number {
  const sorted = [...xs].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)

  return sorted.length % 2 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2
}

/** History wins once it has 2+ runs on a strong key; else the hint; else a lone past run; else nothing. */
export function estimate(samples: readonly number[], hintMs: number | null, isWeak: boolean): Estimate {
  if (samples.length >= 2 && !isWeak) {
    return { etaMs: median(samples), basis: 'history', samples: samples.length }
  }

  if (hintMs !== null) {
    return { etaMs: hintMs, basis: 'guess', samples: 0 }
  }

  if (samples.length >= 1) {
    return { etaMs: median(samples), basis: 'history', samples: samples.length }
  }

  return { etaMs: null, basis: 'none', samples: 0 }
}

export function remember(samples: readonly number[], ms: number): number[] {
  return [...samples, Math.round(ms)].slice(-HISTORY_SIZE)
}

/** Moves `key` to the recent end of the index; `evicted` are the store keys past the cap. */
export function touch(
  index: readonly string[],
  key: string,
  cap = HISTORY_KEYS_CAP,
): { index: string[]; evicted: string[] } {
  const next = [...index.filter(k => k !== key), key]
  const evicted = next.length > cap ? next.slice(0, next.length - cap) : []

  return { index: next.slice(-cap), evicted }
}

/** `0:42`, `3:06`, `1:02:09`. */
export function clock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = String(total % 60).padStart(2, '0')

  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`
}

export function bar(fraction: number, width = BAR_WIDTH): string {
  const filled = Math.round(Math.min(1, Math.max(0, fraction)) * width)

  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

function truncate(text: string, room: number): string {
  return text.length <= room ? text : `${text.slice(0, Math.max(1, room - 1))}…`
}

export function isVisible(run: EtaRun, now: number): boolean {
  return now - run.startedAt >= SHOW_AFTER_MS
}

export function isOverdue(run: EtaRun, now: number): boolean {
  if (run.etaMs === null || run.alerted) {
    return false
  }

  const elapsed = now - run.startedAt

  return elapsed >= run.etaMs * ALERT_RATIO && elapsed - run.etaMs >= ALERT_MIN_OVER_MS
}

/** A run whose end never reached the mod (a lost notification, a reload mid-call) leaves after this. */
export function isStale(run: EtaRun, now: number): boolean {
  const elapsed = now - run.startedAt
  const triple = run.etaMs === null ? 0 : run.etaMs * 3

  if (run.bgId === undefined) {
    return elapsed > Math.max(STALE_FG_MS, triple)
  }

  return elapsed > (run.etaMs === null ? STALE_BG_NO_ETA_MS : Math.max(STALE_BG_MS, triple))
}

/** One band line, sized to `columns`: label, elapsed / ETA, bar, where the ETA came from, overrun. */
export function describeRun(run: EtaRun, now: number, columns: number): RunLine {
  const elapsed = Math.max(0, now - run.startedAt)
  const name = run.bgId === undefined ? run.label : `${run.label} (bg)`
  let tail: string
  let tone: Tone

  if (run.etaMs === null) {
    tail = `  ${clock(elapsed)}  no estimate yet`
    tone = 'none'
  } else {
    const ratio = elapsed / run.etaMs
    const basis = run.basis === 'history' ? `hist×${run.samples}` : 'guess'
    const over = elapsed > run.etaMs ? `  +${clock(elapsed - run.etaMs)} over` : ''
    tail = `  ${clock(elapsed)} / ~${clock(run.etaMs)}  ${bar(ratio)}  ${basis}${over}`
    tone = ratio >= ALERT_RATIO ? 'over' : ratio > 1 ? 'warn' : 'normal'
  }

  return { text: `▸ ${truncate(name, Math.max(8, columns - tail.length - 2))}${tail}`, tone }
}

export function overrunMessage(run: EtaRun, now: number): string {
  const elapsed = now - run.startedAt
  const eta = run.etaMs ?? 0
  const next =
    run.bgId === undefined ? 'Press Esc to interrupt, or type to steer.' : 'Ask Claude to stop it, or keep waiting.'

  return `${run.label} is ${clock(elapsed - eta)} past its ~${clock(eta)} estimate. ${next}`
}

/** The id a call answered with when it went to the background; undefined for a call that finished. */
export function backgroundIdOf(tool: string, result: unknown): string | undefined {
  if (typeof result !== 'object' || result === null) {
    return undefined
  }

  const record = result as Record<string, unknown>

  if (tool === 'Bash' && typeof record.backgroundTaskId === 'string') {
    return record.backgroundTaskId
  }

  if (record.status === 'async_launched' || record.status === 'remote_launched') {
    const id = record.agentId ?? record.taskId

    return typeof id === 'string' ? id : undefined
  }

  return undefined
}

const NOTICE_RE = /<task-notification>([\s\S]*?)<\/task-notification>/g

function tagOf(body: string, name: string): string | undefined {
  return new RegExp(`<${name}>([^<]*)</${name}>`).exec(body)?.[1]?.trim() || undefined
}

/** The background tasks a delivered row says have ended. */
export function parseNotices(text: string): TaskNotice[] {
  return [...text.matchAll(NOTICE_RE)].map(([, body = '']) => ({
    taskId: tagOf(body, 'task-id'),
    toolUseId: tagOf(body, 'tool-use-id'),
    status: tagOf(body, 'status'),
  }))
}
