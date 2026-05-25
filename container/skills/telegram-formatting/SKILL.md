---
name: telegram-formatting
description: Format messages correctly for Telegram using MarkdownV2 syntax
triggers:
  - on: channel_type
    value: telegram
---

# Telegram formatting (MarkdownV2)

You're delivering into Telegram. Telegram's MarkdownV2 parser is strict —
unescaped special characters break the entire message render. Use the
rules below.

## What renders

| Effect      | Syntax                 |
|-------------|------------------------|
| Bold        | `*bold*`               |
| Italic      | `_italic_`             |
| Underline   | `__underline__`        |
| Strike      | `~strike~`             |
| Spoiler     | `\|\|spoiler\|\|`       |
| Inline code | `` `code` ``           |
| Code block  | ```` ```code``` ````   |
| Link        | `[text](https://url)`  |
| Mention     | `@username`            |

## Escape these characters with `\\`

`_ * [ ] ( ) ~ \` > # + - = | { } . !`

A bare `.` or `-` outside formatting WILL break the message. Escape them.
Example: `Version 2\\.0\\.1 released\\!`

## What NOT to use

- `## headings` — Telegram doesn't have headings; they render as raw
  hashes. Use `*bold*` for section titles instead.
- `**double asterisks**` — Markdown-flavor-style bold isn't MarkdownV2.
  Use single `*`.
- HTML tags like `<b>`, `<i>` — wrong parser.
- Bare URLs without `[text](url)` work, but escape any `.` in the path
  if you write them as plain text (`example\\.com/path`).

## Code blocks

Code inside a fenced ``` block does NOT need escaping — it's literal.
Optionally tag the language: ```` ```python```` for syntax highlight.

## Length

Telegram message limit is 4096 characters. If you have more to say, split
into multiple `send_message` calls or send a file.

## Don't auto-prepend a signature

Don't start replies with "Andy:" or any prefix — the platform shows your
bot name already. Just write the body.
