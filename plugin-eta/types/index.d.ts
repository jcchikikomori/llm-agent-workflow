/** Where a run's ETA came from: past runs of the same signature, Claude's `~3m` hint, or nothing yet. */
export type EtaBasis = 'history' | 'guess' | 'none'

/** One long action the band is tracking. */
export type EtaRun = {
  /** The tool_use_id of the call that started it. */
  id: string
  /** What the band shows: the call's description, else its command or tool. */
  label: string
  /** The history signature its duration is filed under. */
  key: string
  /** Epoch milliseconds when the call started. */
  startedAt: number
  /** The estimate in milliseconds; null when there is none yet. */
  etaMs: number | null
  basis: EtaBasis
  /** How many past runs the estimate is the median of; 0 for a guess. */
  samples: number
  /** The background task, agent or workflow id once the call went to the background. */
  bgId?: string
  /** True once the overrun alert fired, so it fires once per run. */
  alerted?: boolean
}

declare module 'claude-code' {
  interface PluginState {
    eta: { running: EtaRun[] }
  }
}
