---
description: Slash-command entry point for the gh-issue-to-pr agent — drives a single GitHub issue (by number, URL, or free-text reference) end-to-end to a merged PR — investigate, plan, branch, implement, test, commit (with confirmation), PR, review, merge, close. Use when the user runs /gh-issue-to-pr, or asks to "pick up issue #N", "work ticket #N end to end", or resume mid-flow ("I've pushed, open the PR", "checks are green, merge it").
---

# gh-issue-to-pr

`/gh-issue-to-pr $ARGUMENTS` is a slash-command entry point for this plugin's own
`gh-issue-to-pr` agent (`agents/opencode-gh-issue-to-pr.md`). It means exactly the same thing
as the natural-language triggers the agent already documents — e.g. "pick up issue #42",
"work ticket #17 end to end", or a raw issue URL. This command exists only to route the slash
command; the agent's system prompt is the single source of truth for the actual workflow
(investigate, plan, branch, scout, implement, test, stage, draft commit + stop, open PR,
review, checklist, merge with confirmation, close with confirmation).

## What to do

Take `$ARGUMENTS` as the issue reference (number, URL, or free-text description) and hand off
to the `gh-issue-to-pr` agent via the Agent tool, passing the reference through as the prompt.
Run it in the foreground — the user is waiting on the next step (investigation, then a plan
to confirm), not background work.

If `$ARGUMENTS` is empty, ask which issue to pick up rather than guessing.

Mid-flow resumption phrases ("I've pushed, open the PR", "checks are green, merge it") also
route here when typed as `/gh-issue-to-pr <phrase>` — the agent's own phase logic figures out
where in the lifecycle it's resuming from context (existing branch/PR for the issue number).

## Rules

- Never substitute this command's own judgment for the agent's — no reimplementing the phases
  here; always delegate to the agent so there is one source of truth for the workflow.
- Never treat an approved plan as standing approval for `git commit`, `git push`, `gh pr merge`,
  or `gh issue close` — those still require the user's explicit turn-by-turn confirmation, per
  the agent's own hard rules.