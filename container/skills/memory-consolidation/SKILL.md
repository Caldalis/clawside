---
name: memory-consolidation
description: Nightly maintenance playbook — promote recurring facts into long-term memory, prune stale entries
---

# Memory consolidation (dreaming)

You were woken by a scheduled task to tidy your memory. This is silent
maintenance: the user is likely asleep. Do not disturb them.

## Output contract for this task (CRITICAL)

Your final reply MUST be a single `<internal>...</internal>` block
summarizing what you changed (or "nothing to change"). Do NOT send any
`<message>` — unless you found something that genuinely requires the
user's urgent attention.

## Steps

1. List recent logs: `run_bash: ls /workspace/agent/memory/`
2. Read the last ~7 days of daily logs (`read_file`), plus
   `/workspace/agent/CLAUDE.local.md`.
3. **Promote**: facts, preferences, and project context that recur
   across days belong in `CLAUDE.local.md`. Merge them into the right
   section with `edit_file` — rewrite and integrate, never append a
   raw copy of log lines.
4. **Prune**: remove or rewrite `CLAUDE.local.md` entries that are
   stale, superseded, or resolved. Keep the file organized and small —
   it is loaded into your context every private turn, so every line
   costs tokens forever.
5. Do NOT modify daily logs or session archives — they are records,
   and the search index references their line numbers.
6. If nothing needs changing, change nothing. An empty diff is a
   perfectly good outcome.
