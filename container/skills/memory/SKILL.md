---
name: memory
description: Write durable facts to your daily log and long-term memory
triggers:
  - on: always
---

# Memory

You have file-based memory that survives container restarts. Facts you
do not write down are lost when this session's context is compressed.

## Two places, different purposes

| What | Where | How |
|---|---|---|
| Today's events, decisions, task outcomes, things the user said | Daily log | `memory_append(text)` |
| Durable facts: preferences, conventions, people, recurring context | `CLAUDE.local.md` | `edit_file` / `write_file` |

## Daily log — write early, write often

Call `memory_append` the moment something memorable happens. Do not
ask permission, do not announce it. One entry = one fact or event,
2-3 sentences max. Worth writing:

- The user states a fact, preference, or deadline
- A decision is made (include the *why*)
- A task completes or fails (include the outcome)
- You learn something about the user's projects or workflow

## Long-term memory — curate, don't dump

`/workspace/agent/CLAUDE.local.md` is loaded into your context in every
private session. Promote a fact there when it keeps mattering across
days. Keep it organized in sections; rewrite and prune stale entries
while you edit. Never let it become a raw log — that is what daily
logs are for.

## Recall

Today's and yesterday's logs plus CLAUDE.local.md are auto-loaded in
private sessions. For anything older — or anything in the knowledge
base — search the index, then read the exact lines:

    memory_search("pricing decision")           → snippets with path:lines
    memory_get("memory/2026-06-20.md", 12, 31)  → exact lines with context

ALWAYS run memory_search before answering questions about past work,
decisions, dates, people, or preferences. If the first query misses,
rephrase (synonyms, names, dates) and search again — one retry is
cheap, a wrong answer is not.

Reference documents the user gives you can live in
`/workspace/agent/knowledge/` — they are indexed automatically and
searchable via memory_search(source="knowledge").

## Transcripts & maintenance

Conversation transcripts are archived to `sessions/*.md` automatically
before history is compressed or cleared — nothing is ever truly lost,
and archives are searchable via memory_search. When a compression
summary cites an archive path, you can memory_get it for full detail.

The user can enable a nightly memory-tidying routine by asking you to
schedule it. When they do, call:

    schedule_task(prompt="Load the memory-consolidation skill and
    follow it strictly.", process_after="<tomorrow 04:00>",
    recurrence="0 4 * * *")

## Privacy

Memory is auto-loaded ONLY in private sessions, never in group chats.
In group sessions you may still write memory, but be careful about
repeating private facts aloud.
