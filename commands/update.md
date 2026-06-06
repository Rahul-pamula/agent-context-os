---
name: update
description: Mid-session checkpoint — save progress without ending the session
allowed-tools: Read, Write, Edit, Bash
x-source: skills-sync/commands/update.md
x-source-version: ea93149
---

# /update — Quick Checkpoint

Lightweight save without closing the session. Use when switching tasks or after completing something significant.

## Instructions

### 1. Scan recent conversation
Identify in 30 seconds:
- What was just worked on
- Any decisions made
- Any state changes needed

### 2. Append to session log
Run `date +%Y-%m-%d` for TODAY, `date +%H:%M` for TIME.

Append to `sessions/{TODAY}.md`:

```markdown
## Update: {TIME}
- {what was worked on, 1-3 bullets max}
```

Create the file with a header if it doesn't exist yet.

### 3. Update state if something changed
Only touch `state/current.md` if a priority shifted, a thread opened or closed, or a task was completed. Skip otherwise.

### 4. Confirm
One line: "Checkpointed: {brief description}"
