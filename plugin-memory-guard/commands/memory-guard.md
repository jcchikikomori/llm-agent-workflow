---
description: Guides Claude through the memory-guard flow when a watched .claude/**, root AGENTS.md, or docs/ticket-tracking/** path is flagged by the memory-guard plugin hooks — save the change to memory, then apply the project's remove/stash preference (asking once, ever, per project, if none is set yet) via the single apply_action.py script.
---

# memory-guard

When the memory-guard plugin flags one or more watched paths (under `.claude/`,
the project's root `AGENTS.md`, or `docs/ticket-tracking/`), follow this
procedure exactly. The plugin's injected instruction text tells you which of
the two cases below you're in — read it carefully, it already checked whether
this project has a standing preference.

**The first-time `AskUserQuestion` call (Case A below) is mandatory, including
in Auto Mode or any other "work without stopping to ask" mode.** That general
bias exists for ordinary implementation judgment calls — it does not apply
here. This is a one-time, per-project question the user explicitly asked for;
silently picking an action and continuing is not an acceptable substitute, no
matter what session mode is active.

**Always resolve via `apply_action.py`, never by hand-writing `git stash` /
`rm` yourself.** A real session was observed where paths got recorded as
`action: "stash"` in the state file while `git status` still showed them
modified afterward — the resolution was marked done without the underlying
git command ever actually running. `apply_action.py` closes that gap: it
recomputes the live dirty watched list itself, performs the deletion/stash,
and marks every path resolved, all in one atomic step. There is no supported
path where you construct the git/rm command manually.

## Case A — no project preference set yet (first time ever for this repo)

1. **Collect every path flagged so far this turn.** Handle them together in
   one pass — one memory review and one question, not one interruption per
   file.
2. **For each path, judge memory-worthiness** from the actual diff (the
   `old_string`/`new_string` from the edit that triggered the flag, or
   `git diff -- <path>` for the full change):
   - **Skip** pure whitespace/formatting/reflow, typo fixes with no semantic
     change, or a reorder of existing content with nothing new.
   - **Save** when the diff adds a rule, convention, decision, constraint
     ("must", "never", "always", "policy"), a credential/URL/environment
     detail, or a ticket-tracking status/scope note.
3. **Save each memory-worthy change** (see "Saving to memory" below) —
   before applying any action. The file is the only copy of that content
   until the memory save actually lands.
4. **Ask the user ONCE** — this is a per-project question, not per-file or
   per-turn — via `AskUserQuestion`: should watched `.claude`-scoped changes
   in *this project* be **Removed** (deleted from disk — the content is
   already preserved in memory, so the working-tree copy is disposable) or
   **Stashed** (`git stash`, scoped only to the flagged paths)? Make clear
   this choice will be remembered and applied automatically for this project
   from now on.
5. **Persist the answer, then apply it — two commands, in order**:

   ```bash
   python3 <set_preference.py path from the instruction> --repo-root "<repo_root>" --action <remove|stash>
   python3 <apply_action.py path from the instruction> --repo-root "<repo_root>" --action <remove|stash> --session-id <id>
   ```

   `apply_action.py` recomputes every currently-dirty watched path itself
   and resolves all of them — you don't need to pass individual paths.

## Case B — project preference already set (every time after the first)

The plugin's instruction already tells you the standing action
(`remove` or `stash`) — do not ask the user again.

1. Collect every path flagged this turn.
2. For each, judge memory-worthiness and save (see below) — this step still
   happens every time, only the remove/stash *question* is one-time.
3. Apply the standing action — one command:

   ```bash
   python3 <apply_action.py path from the instruction> --repo-root "<repo_root>" --action <remove|stash> --session-id <id>
   ```

## Saving to memory

- Check whether any `mempalace_*` tools are present in your current tool
  roster. If so, call `mempalace_check_duplicate` first, then
  `mempalace_add_drawer` with the content **verbatim** (wing = project,
  room = something like `decisions` or `conventions`).
- If `mempalace_*` tools are not available, fall back to the file-based
  auto-memory system described in the user's global AGENTS.md
  ("Memory — Dual-Layer System"): write or append a
  `~/.config/opencode/projects/<project-slug>/memory/project_<slug>.md` (or
  `feedback_<slug>.md` if the change reads as a behavioral rule rather than
  a project fact), then add one line to that project's `MEMORY.md` index in
  the same style as its existing entries.

## What `apply_action.py` actually does

- Recomputes the live dirty watched list via `git status --porcelain`,
  filtered through the same matcher the hooks use — never trusts any
  earlier-recorded list, so nothing stale or already-handled gets swept in.
- `remove`: deletes each of those files from disk.
- `stash`: runs a single `git stash push -u -- <path1> <path2> ...` scoped
  to exactly those paths — never a bare `git stash push` with no pathspec,
  since that would stash the entire working tree.
- Marks every path it touches `"resolved"` in the session state file, so
  the hooks stop re-flagging them.
- If nothing is currently dirty under the watched paths (already resolved
  another way since the flag), it no-ops and says so — it never touches
  anything outside the watched-path list.

## Rules

- Never touch anything beyond the flagged, currently-dirty watched paths —
  this guard exists specifically so unrelated work is never swept up,
  whether by stash or by deletion.
- Don't ask the user one question per file — the whole point of the
  standing preference is that this is asked at most once, ever, per
  project.
- Don't save trivial/cosmetic diffs to memory — that defeats the point of
  having a memory-worthiness filter.
- `docs/ticket-tracking/**` files are documented elsewhere as things that
  must never be committed — if a user hasn't set a preference yet and asks
  for a recommendation, Remove is usually the safer default for those
  specifically, since they shouldn't linger in the working tree either.
- To let a user change their mind later, `scripts/reset_preference.py
  --repo-root <path>` clears the standing preference so the next flagged
  change asks Case A again.