# resource-guard plugin

Keeps several Claude Code sessions running at once from hanging the machine. It has three parts:

- **A gate.** A PreToolUse hook holds back new heavy work (subagents, workflows, background commands, test
  suites, builds, containers) from sessions the user isn't typing in when memory gets tight.
- **A watchdog.** One detached process per machine freezes background sessions' running work, Docker containers
  included, when load turns critical, then resumes it once the machine calms down.
- **A CLI.** It shows which session uses what, and freezes, resumes or stops a session's work from any
  terminal, Windows included, even when the sessions themselves no longer respond.

Why it exists: running 2–4 sessions in parallel on WSL2 kept ending the same way. Memory ran out, swap (a VHD file on
the Windows side) thrashed, and the machine stopped answering, sessions included. Each session costs 2–3 GB before it
does any work: the claude process plus its own MCP and LSP containers. A `docker compose run ... rspec` in a
background session then tips it over. Most of the hang came from containers, and a container keeps running when its
`docker` client process is stopped, so pausing containers had to be part of the fix.

Claude generated this plugin. The design choices below are the ones I'd make again, and each one says why.

## Install

```bash
/plugin install resource-guard@llm-agent-workflow
/reload-plugins
```

Requirements: Linux or WSL2, Python 3.9+ (standard library only), Docker optional. On WSL1, macOS and native
Windows the hooks do nothing, and `doctor` says so.

**It starts in observe mode.** The gate is active right away. The watchdog only logs `would-freeze` events until you
turn freezing on:

```json
{ "freeze_mode": "enforce" }
```

Put that in `~/.claude/resource-guard.json`. Check `events.jsonl` (below) for a day first, to see whether the
thresholds suit your machine.

**To cap MCP/LSP containers**, add one line to your shell rc file and restart Claude Code (see
[Session servers](#session-servers-mcplsp-containers)). `resource-guard doctor` tells you when it's missing:

```bash
export PATH="$HOME/.claude/.resource-guard/shims:$PATH"
```

## How it works

### Levels

The watchdog samples every 5 s, or every 2 s once load is elevated. It reads `/proc/meminfo`, PSI stall
(`/proc/pressure/*`, computed from the `total=` counter deltas, because `avg10` lags about 10 s behind a thrash), and,
on WSL, Windows host memory and commit.

| Level | Triggers (any one; all configurable) | Effect |
| --- | --- | --- |
| elevated | MemAvailable < 20%, SwapFree < 40%, PSI memory some ≥ 10%, PSI io full ≥ 20% (15% on WSL), host available < 15%, host commit free < 10%, CPU PSI ≥ 80% | background heavy work waits up to 20 s for calm, then runs |
| critical | MemAvailable < 10%, SwapFree < 20%, PSI memory full ≥ 10%, host available < 7%, host commit free < 5% (2 samples in a row) | background heavy work waits, then is denied; foreground gets a prompt; one background session frozen |
| hard | MemAvailable < 5%, SwapFree < 10%, PSI memory full ≥ 30%, host available < 4%, host commit free < 2% | every background session frozen at once |

A level only clears after 3 calm samples in a row, and it steps down to the highest tier seen during them: hard,
then critical, critical, ok lands on critical, not ok. CPU pressure never pushes the level above elevated: it slows
a machine down but doesn't hang it the way memory thrash does.

### Foreground vs background

The **foreground** session is the one with the most recent real prompt (continuation turns don't count). The
previous foreground keeps a 60 s grace period, so switching terminals doesn't freeze the session you just left. The
foreground session is never frozen, and at critical its heavy calls get an `ask` prompt instead of a deny.

Sessions are keyed by `(claude pid, start time)`, not by session ID, which `/clear`, resume and fork all change. The
list comes from `~/.claude/sessions/<pid>.json`, with a `/proc` scan as backup.

### The gate (PreToolUse)

- **Relief** commands are never held back: `docker stop|kill|rm|pause`, `docker compose down|stop`, `kill`, `pkill`,
  `killall`, and this CLI. That holds only when no other segment of the same command is heavy:
  `docker stop x && docker compose run app rspec` is gated as heavy.
- **Heavy** calls are gated: Agent, Task, Workflow and Monitor; `run_in_background`; and any `&&`/`;`/`|` segment
  matching `heavy_patterns`. The patterns are anchored to the command position (after `sudo`, `env`, `timeout`,
  `VAR=value`, `bundle exec`, `npx`, `uv run`, `python -m`), so `grep -rn pytest spec/`, `cat pytest.ini` or
  `docker logs app-run-3f2a` stay light.
- **Light** calls pass silently, without even reading the load.

The gate never answers `allow`, because that would skip your own permission rules. Three denies within five
minutes end that session's turn (`continue: false`), which stops retry loops.

### Freezing

The watchdog never freezes the claude process or its MCP/LSP servers. It freezes **work**:

- **Processes**: anything whose environment has `CLAUDE_PID=<session pid>`. These are Bash and Monitor shells and
  everything they started, `nohup ... &` jobs included.
  - Excluded:
    - nested `claude` processes;
    - hooks and whatever they start, recognized by the `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PROJECT_DIR` that Claude Code
      gives hooks and never gives Bash-tool shells;
    - `git*`, `ssh*`, `scp`, `sftp`, `rsync`, `gpg*` and its daemons, `pinentry*` (they hold locks other sessions
      wait on);
    - detached daemons that only inherited the variable (`dbus-daemon --fork` and friends);
    - processes older than the session, which belong to an earlier session that had the same PID;
    - the CLI's own shell and its ancestors.
  - Every signal re-checks the process start time and goes through a pidfd, so a reused PID never gets hit.
  - **Write-ahead:** a process is recorded in `frozen.json` before it gets SIGSTOP, so a watchdog that dies
    halfway through a freeze can't leave a stopped process nobody knows about.
- **Containers**: `docker pause`, but only containers this session owns.
  - Ownership comes from labels added by a `docker` shim that SessionStart puts first on the Bash tool's `PATH`
    (`dev.claude.pid`, `pid_start`, `client`, `client_start`, `session`). It covers `run`, `create`,
    `container run|create` and `compose run`. Without labels, a live `docker run --name X` claims container `X`
    by exact name only: a short name could otherwise prefix-match somebody else's container ID.
  - Never paused: containers another session references (`docker exec`, by name or a 12+ character ID prefix)
    unless that session is frozen too, compose services (`oneoff=False`: shared databases and caches),
    `never_pause` images (mysql, postgres, redis, mongo by default), session servers (`dev.claude.role=server`,
    next section), and anything unlabeled.

Order:

- **Freeze:** SIGSTOP parent-first, rescan for forks (up to 3 passes), then pause containers.
- **Resume:** unpause first, then SIGCONT.

If a container's client died while frozen (Esc, a background-task limit), the container is stopped after resume:
nothing is left to read its output.

**Escalation at critical:**

- Freeze one session: the one with the most work-memory growth since the last scan. Wait 10 s. The foreground is
  checked again once the freeze holds its lock, so a session the user just typed in is skipped.
- Freeze the next one only while PSI memory stall stays high or MemAvailable keeps falling.
- If memory is low but flat and calm, stop and log `held-by-idle`. Freezing frees no memory, so the rest is held by
  idle servers, and `sessions` shows which.

**Resume** waits for 3 calm samples (PSI under the critical line, memory above the hard floor and not falling), then
resumes one session every 20 s, foreground first, then whoever waited longest. A container the daemon can't unpause
is retried 5 times, then reported (`could not unpause ...`) so one stuck session never blocks the rest. A session that
tips the machine over again within 5 minutes of its resume gets backed off: 60, then 120, then 240 s.

A prompt in a frozen session resumes it right away.

### Session servers (MCP/LSP containers)

Every session runs its own copy of every docker-based MCP and LSP server. On this machine, four sessions held four
sonarqube JVMs (1.7 GB) and four mempalace containers (1.1 GB), and none of them had a memory limit. A JVM started
without `-Xmx` sizes its heap to a quarter of the RAM it sees, so each sonarqube copy could grow to about 4 GB.

Claude Code starts those containers itself, not through the Bash tool, so the Bash-tool shim never sees them. The
`export PATH` line from [Install](#install) puts a stable link to the same shim on Claude Code's own `PATH`. After
that, a `docker run` is treated as a **session server** when its nearest `claude` ancestor is at most 4 levels up
(wrapper scripts like `run-mempalace.sh` count) and nothing on the way carries `CLAUDE_PID`. A session server:

- gets `dev.claude.role=server` plus the usual session labels, so `sessions` lists it under its session and counts it
  in that session's baseline;
- is never paused, and `stop` leaves it running: the session would lose that tool mid-call;
- gets `--memory <cap>` from `server_caps`, inserted just before the image (and before a `--` that precedes it),
  unless the server's own config already sets a limit: `-m`/`--memory` (bundled ones like `-dm 512m` too),
  `--memory-reservation` or `--memory-swap`. A hard limit below either of the last two would make the daemon refuse
  the container.

Hooks get the same treatment. They carry `CLAUDE_PID` plus `CLAUDE_PLUGIN_ROOT` or `CLAUDE_PROJECT_DIR`, so a
container a hook runs (mempalace-docker's save hooks) is labeled and capped as a server, and a paused one can't
hang the hook. That includes the `default` cap for a hook container whose image no glob names; give its image
`none` to opt it out.

Labels are bookkeeping hints, not a trust boundary: any process of yours can set them, as can anyone who can reach
the docker socket. A forged `role=server` label can only keep a container out of a freeze, which an unlabeled one
already is.

**The cap is set at creation, on purpose.** `docker update --memory` could add one later, but a JVM reads its limit
only at startup. A cap added afterwards doesn't shrink the heap; it gets the JVM OOM-killed. Under the 2 GB default,
the sonarqube JVM sizes its heap to 512 MB. Each copy measured about 415 MB in total here.

| Image glob | Cap | Measured here, per copy |
| --- | --- | --- |
| `*mempalace*` | `3g` | up to 1.0 GB |
| `ghcr.io/rvben/rumdl*` | `512m` | about 21 MB |
| anything else (`default`) | `2g` | sonarqube about 415 MB |

SessionStart writes the caps to `~/.claude/.resource-guard/shim.conf`, because the shim is POSIX `sh` and reads no
JSON. Edit `server_caps` in `resource-guard.json`, not that file. The most literal glob wins, `none` turns a cap off,
and a value below docker's 6 MB floor is dropped rather than handed to `docker run`. New caps apply to servers started
after the next SessionStart.

### Watchdog lifecycle

- **Who runs it:** SessionStart starts it detached (own session, stdio closed, every `CLAUDE_*` variable stripped).
- **Singleton:** an flock on `watchdog.lock`.
- **Liveness:** "can a hook take the lock?". A late heartbeat doesn't count, because a watchdog that stalls for a
  minute during a real thrash must not get resumed-over by every hook. One that stays silent for 2 minutes is
  reported as hung (SessionStart, every prompt), with `watchdog stop` as the fix.
- **When it's dead:** the next hook (SessionStart, UserPromptSubmit or PostToolUse, so mid-turn too) starts a new
  one, which resumes what the old one left frozen. When it can't start one (at most once per 30 s; after 3 crashes in
  10 minutes it gives up and SessionStart says so), the hook resumes everything itself, within 6 s.
- **Exit:** after 120 s with no live sessions, after 5 failing ticks in a row, or on TERM, INT or HUP. Either way it
  resumes everything first, ignoring further signals until that is done.

### WSL awareness

- **Detect.** `/proc/sys/kernel/osrelease` or `WSL_DISTRO_NAME`; WSL1 is unsupported.
- **Host memory.** Every 60 s (20 s once elevated), the watchdog asks Windows through `powershell.exe`
  (`Win32_OperatingSystem`, about 1.2 s per call). It reads available memory, standby cache included, and commit
  charge. The VM's `/proc/meminfo` can look fine while Windows itself is out of memory, and commit is what runs out
  first when `vmmemWSL` grows.
- **`.wslconfig`.** `doctor` reads it (via `wslvar USERPROFILE` + `wslpath`) and gives advice only:
  - memory cap vs host RAM;
  - `autoMemoryReclaim=gradual` when it's unset;
  - swap larger than half of memory (swap lives on a VHD, so it turns pressure into long disk stalls);
  - why `CLAUDE_CODE_TOOL_MEMORY_LIMIT` can't work without `[boot] systemd=true`.

  It never edits the file. Applying a change needs `wsl.exe --shutdown`, which stops every distro.

## The CLI

```bash
~/.claude/.resource-guard/bin/resource-guard status          # level, metrics, host memory, watchdog, frozen
~/.claude/.resource-guard/bin/resource-guard sessions        # per session: baseline (tree + servers) vs work
~/.claude/.resource-guard/bin/resource-guard freeze <pid>    # or --others: everyone except the foreground
~/.claude/.resource-guard/bin/resource-guard resume <pid>    # or --all
~/.claude/.resource-guard/bin/resource-guard stop <pid> --yes
~/.claude/.resource-guard/bin/resource-guard doctor          # shims on PATH? on WSL, the Windows escape hatch
~/.claude/.resource-guard/bin/resource-guard watchdog start|stop|status
```

Inside a session, `/resource-guard` runs `status` and `sessions` and summarizes them. Only `status`, `sessions` and
`doctor` are pre-approved there; the rest goes through the normal permission prompt.

`freeze` and `stop` refuse to target the session they run in (its own commands would stop) unless given
`--include-self`, and `freeze --others` leaves it out.

**When WSL itself hangs,** run it from PowerShell or Windows Terminal, by its absolute path. `wsl.exe -e` starts no
shell, so nothing expands `~`. `resource-guard doctor` prints the exact line for your distro and home:

```powershell
wsl.exe -d Ubuntu-22.04 -e /home/<you>/.claude/.resource-guard/bin/resource-guard freeze --others
wsl.exe -d Ubuntu-22.04 -e /home/<you>/.claude/.resource-guard/bin/resource-guard resume --all
```

`wsl.exe --shutdown` is the last resort. It kills every distro and every session, unsaved work included.

## Configuration

`config/defaults.json` holds every default. Override any key in `~/.claude/resource-guard.json`, or point
`RESOURCE_GUARD_CONFIG` at another file. Nested objects merge; lists replace.

| Key | Default | Meaning |
| --- | --- | --- |
| `freeze_mode` | `observe` | `enforce` to actually freeze |
| `gate` | `true` | PreToolUse gate on/off |
| `thresholds`, `wsl_thresholds` | see above | per-level trigger values |
| `gate_wait_seconds` | `20` | how long a held-back call waits for calm; capped at 35 (the hook times out at 45) |
| `max_sessions` | `3` | SessionStart warns at this many live sessions |
| `heavy_patterns`, `relief_patterns` | builds, tests, containers / stop, kill | regexes per command segment |
| `never_pause` | databases and caches | container name/image globs never paused |
| `server_caps` | `default` `2g`, mempalace `3g`, rumdl `512m` | `--memory` for session-server containers, by image glob |
| `never_freeze_commands` | `git*`, `ssh*`, `gpg*`, `rsync`, `pinentry*`, ... | process-name globs never frozen |
| `target_pids` | `[]` | restrict freezing to these session pids (smoke tests) |

**Kill switch:** `RESOURCE_GUARD_DISABLE=1`, or `"enabled": false`. The watchdog resumes everything and exits.

State lives in `~/.claude/.resource-guard/` (mode 0700). Overview:

- `status.json` is the watchdog's last tick.
- `shims` links to this version's docker shim; `shim.conf` holds the server caps.
- `frozen.json` is what it froze.
- `events.jsonl` is the freeze/resume history. It never holds prompt or command text.
- `watchdog.log` and `hook-errors.log` are for debugging.

## Known limits

- **Freezing frees no memory.** It stops growth and contention, and frozen pages can be swapped out, but only
  stopping work gives memory back. `sessions` shows what `stop` would reclaim.
- **The shim only sees `docker` looked up on `PATH`.** Without the `export PATH` line, MCP/LSP containers stay
  unlabeled and uncapped. A config, alias or script that runs `/usr/bin/docker` directly bypasses the shim either way;
  `doctor` flags Bash work that runs `docker run` without labels.
- **Only docker-based servers get a cap.** A server that runs natively (`npx`, `uvx`, `node`) is a plain child of
  `claude`, and capping it needs a cgroup per process, which WSL only has with `[boot] systemd=true`. A `compose run`
  server (ruby-lsp) gets labels but no cap: `compose run` has no `--memory` flag, so set `mem_limit` in the compose
  file instead.
- **A server that outgrows its cap is OOM-killed**, and its tools fail in that session (`/mcp` shows it as failed).
  Raise its cap in `server_caps`, start a new session (or wait for one to start), then reconnect it from `/mcp`.
- **A frozen foreground-style Bash command** keeps the session's turn waiting until Claude Code's command timeout,
  which moves it to the background. The PostToolUse note tells Claude not to retry it.
- **`ask` in `-p` mode** behaves like defer, so headless sessions at critical are not prompted.
- **`~/.claude/sessions/`** is an undocumented Claude Code file format. A `/proc` scan backs it up, but the
  `kind` and `cwd` columns may go blank if the format changes.

## Tests

```bash
python3 -m unittest discover -s plugin-resource-guard/tests
```

The suite builds fake `/proc`, `/sys` and `~/.claude` trees, serves a fake Docker Engine on a unix socket, and runs
the shim under `sh` and `dash`.

- Every test sets `RESOURCE_GUARD_FAKE_SIGNALS`, so signals are logged instead of sent, and points `DOCKER_HOST` at a
  dead socket.
- `test_signals_real.py` is the exception: it stops and continues a real `sh` → `sleep` tree that it spawns itself.
- `test_hook_e2e.py` starts a real, sandboxed watchdog from the hook and checks that SessionStart returns without
  waiting on it.

Coverage, dev-only:

```bash
uvx coverage run --branch --source plugin-resource-guard/hooks,plugin-resource-guard/scripts \
  -m unittest discover -s plugin-resource-guard/tests && uvx coverage report
```

## Changelog

### 0.2.0

- **Session servers.** A stable shim link, `~/.claude/.resource-guard/shims`, for Claude Code's own `PATH`. It
  labels each session's docker-based MCP/LSP servers, and the containers hooks run, as `dev.claude.role=server`.
- **Memory caps.** Those containers get `--memory` from the new `server_caps` setting, set at creation so a JVM sizes
  its heap to it. A cap the server's own config sets wins. SessionStart writes the caps to `shim.conf`.
- **Never paused or stopped.** Server containers count in their session's baseline (`sessions`, the SessionStart
  footprint), but are never paused, and `stop` leaves them running. A session that only has servers is no longer an
  escalation candidate.
- **doctor** checks every live session's `PATH` for the shims and prints the shell rc line when one is missing.

### 0.1.0

Initial release.

- **Gate.** The PreToolUse gate covers Agent, Task, Workflow, Monitor, background commands and heavy shell
  segments. It waits, then denies or asks, never answers `allow`, and stops the turn after a burst of denies.
- **Watchdog.** A singleton watchdog with a hysteresis level machine. It freezes progressively at critical and
  everything at once at hard, re-checks the foreground under the lock, resumes one session at a time with rotation
  and backoff, steps aside after repeated failing ticks, and resumes everything on exit.
- **Freeze and resume.** Bash-work freeze uses `CLAUDE_PID` ancestry, pidfd signals, start-time checks and a
  write-ahead `frozen.json`. Hooks are told apart by their environment, and the CLI never signals its own shell.
  Docker pause/unpause goes through the Engine API over the unix socket, with ownership from shim labels or exact
  names only.
- **Hooks.** SessionStart installs the shim and CLI link and starts the watchdog. UserPromptSubmit tracks the
  foreground and resumes on prompt. PostToolUse tells Claude about freezes. SessionStart, UserPromptSubmit and
  PostToolUse run the dead-man switch. SessionEnd hands off to the watchdog.
- **WSL.** Host memory and commit come from PowerShell. `.wslconfig` advice and a Windows escape hatch are documented.
- **CLI and docs.** CLI: `status`, `sessions`, `freeze`, `resume`, `stop`, `doctor`, `watchdog`. Also adds the
  `/resource-guard` command and the `resource-guard` skill.
