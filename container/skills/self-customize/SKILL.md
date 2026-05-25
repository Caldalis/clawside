---
name: self-customize
description: Add packages or MCP servers to your environment, or edit your own config
---

# Self-customize

You can modify your own environment when it would help. Three surfaces,
each with a different cost. Pick the smallest one that solves the
problem.

## Decision tree

| You want to…                                       | Use                       |
|----------------------------------------------------|---------------------------|
| Remember a fact, preference, or context long-term  | Edit `CLAUDE.local.md`    |
| Refine your own base instructions / persona        | Edit `CLAUDE.md`          |
| Add a new skill or override a built-in one         | Write `skills/<name>/SKILL.md` |
| Add a Python package or apt dep to your container  | `install_packages` MCP tool |
| Add a new MCP server (e.g. a tool integration)     | `add_mcp_server` MCP tool |
| Patch source code of clawside itself               | Not supported — ask the user |

## CLAUDE.local.md — your long-term memory

Path: `/workspace/agent/CLAUDE.local.md`.

This is yours. No approval required. The host never overwrites it. Edit
it any time a piece of context is worth keeping across sessions:

- User preferences ("they prefer terse replies")
- Recurring context ("project X uses Postgres 16, repo at ~/code/x")
- Facts the user told you ("Alice's birthday is March 14")
- Workflow defaults ("always deploy to staging first")

Keep it organized — sections, not a blob. Prune stale entries.

## CLAUDE.md — your base prompt

Path: `/workspace/agent/CLAUDE.md`.

Edit when the user gives you durable feedback about how you should
behave (tone, default verbosity, specific habits). Changes take effect
on the next container restart.

## skills/ — modular instructions

Path: `/workspace/agent/skills/<name>/SKILL.md`.

A skill is a folder with `SKILL.md` (YAML frontmatter + body). Add one
when a task family needs its own playbook (e.g. "code review",
"weekly summary"). With `triggers` frontmatter you can have the skill
auto-load on certain conditions; without it, the skill is lazy and you
load it on demand via the `load_skill` tool.

Trigger options:
- `on: always` — every turn
- `on: first_message_in_session` — first reply only
- `on: channel_type` + `value: <name>` — when delivering to that channel

Skills land on next container restart.

## install_packages — environment changes

Use the `install_packages` MCP tool when you genuinely need a new
binary or library. It requires admin approval and triggers a container
rebuild + restart automatically. Don't request packages for one-off
tasks; just write a script that uses what's already available.

## add_mcp_server — new tools

Use `add_mcp_server` when there's a specific MCP server that would
unlock a capability you don't have (e.g. a specific API integration).
Requires admin approval and triggers a container restart.

## What not to do

- Don't rewrite `CLAUDE.md` cosmetically just because you can.
- Don't install packages "just in case".
- Don't add skills the user didn't ask for or that don't serve a clear
  recurring task — clutter is its own cost.
