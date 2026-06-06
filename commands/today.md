---
name: today
description: Morning heartbeat — briefing, deadline check, and staleness scan
allowed-tools: Read, Write, Bash, Glob, mcp__google-workspace
x-source: skills-sync/commands/today.md
x-source-version: 173c978
---

# /today — Morning Heartbeat

Lightweight daily check-in. Run at the start of each day or after any long gap between sessions. Designed to finish in under 60 seconds — if it's slow, it won't get used.

## Instructions

### 1. Establish context
Run `date +%Y-%m-%d` and `date +%A`. Read `state/heartbeat-log.md` (if present) to find the last check-in date.

### 2. Scan recent activity
Catch work even from sessions that closed without `/end`:

```bash
git log --oneline --since="3 days ago"
```

Also check `sessions/` for logs newer than the last heartbeat. Note what was last worked on for continuity.

### 3. Load state
Read `state/current.md` and `state/weekly-priorities.md`.

### 4. Check staleness (escalating)
- `state/current.md` `**Last Updated:**` older than 3 days → flag
- `state/weekly-priorities.md` `**Week of:**` from a previous week → flag
- Open threads in `current.md` carrying a date annotation:
  - older than 7 days → "stale — still relevant?"
  - older than 14 days → "likely stale — remove or convert to a task?"

Present these as proposals. **Do not auto-update.**

### 5. Surface deadlines
Scan `TODO.md` for unchecked items (`[ ]`) with a date in the next 7 days, or marked urgent / time-sensitive. List them, most urgent first.

### 6. Pull live data (if MCP available)
Same as `/start` — calendar for the next 7 days, unread email count. Skip if MCP isn't connected.

### 7. Deliver the heartbeat

```
MORNING CHECK-IN — [DATE] ([day of week])
Last heartbeat: [date] ([N] days ago)

SINCE LAST CHECK-IN:
- [N] commits: [brief themes]
- Session logs: [found / none]

STATE:
- current.md — [fresh / N days stale]
- weekly-priorities.md — [fresh / N days stale]

DEADLINES (next 7 days):
- [items, most urgent first]

[If stale items found:]
STALE ITEMS:
- [item] — [N] days old [propose: remove / convert to task]

[Calendar highlights, if MCP available]
```

Skip any section that's clean — if everything's fresh and nothing's due, say so in one line.

### 8. Log the heartbeat
Append to `state/heartbeat-log.md`:

```markdown
## [DATE]
- Commits since last: [N]
- State staleness: [summary]
- Deadlines flagged: [count]
- Stale items flagged: [count]
```

### 9. Transition
Ask: "What's the focus today?" Do NOT re-run the full `/start` flow — this is the lighter, faster check-in.

> Memory curation lives in `/dream`, not here. `/today` surfaces staleness and deadlines; `/dream` proposes memory changes. Keeping them separate avoids two commands editing memory.
