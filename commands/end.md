---
name: end
description: End a session — log what happened and update state for next time
allowed-tools: Read, Write, Edit, Glob, Bash
x-source: skills-sync/commands/end.md
x-source-version: 173c978
---

# /end — Close Session

## Instructions

### 1. Get today's date
Run `date +%Y-%m-%d` and store as TODAY.

### 2. Summarize the session
Ask the user (or infer from conversation history):
- What did we work on?
- Any decisions made?
- What's next?

### 3. Write the session log
Create or update `sessions/{TODAY}.md`:

```markdown
# Session — {TODAY}

## What happened
- [Bullet summary of work done]

## Decisions
- [Any decisions made or conclusions reached]

## Next time
- [Open threads, next actions, things to pick up]
```

If a session file already exists for today, append a new section with a timestamp header (`## Session 2 — HH:MM`) rather than overwriting.

### 4. Update state/current.md
- Update "Active priorities" if any shifted
- Add or remove "Open threads" based on what happened
- Update "Recent context" with a brief note on what was covered
- Set `**Last Updated:**` to today's date

### 5. Update state/weekly-priorities.md (if relevant)
If a priority was completed or changed, update it. Don't force an update if nothing changed.

### 6. Propose auto-memory updates

Scan the session for durable patterns worth preserving across *all* conversations in this project — not just today's work. Claude Code auto-loads `MEMORY.md` from this project's memory dir (`~/.claude/projects/<encoded-cwd>/memory/`, where `<encoded-cwd>` is the project path with `/`, `\`, and `:` replaced by `-`) at the start of every conversation, so anything saved here compounds. See [`docs/auto-memory.md`](../docs/auto-memory.md) for the spec and the four typed categories.

Propose 0–2 additions. Good candidates:
- Environment quirks or tool behaviors confirmed this session
- Workflow preferences the user expressed ("always do X", "never do Y")
- Debugging solutions that will recur
- Stable facts about projects, people, or processes

Bad candidates:
- Session-specific context (what was worked on today — that's the session log's job)
- Anything already in `CLAUDE.md` or the state files
- Unverified conclusions from a single observation

**Friction-point check:** before presenting proposals, ask yourself — *was there a friction point this session that a memory entry would have prevented?* A tool you had to re-learn, an error you'd hit before, a convention you had to re-infer. If yes, write the entry. Repeating a mistake is a system failure — turn it into a durable rule.

Present proposals inline; don't write to memory without confirmation:

```
MEMORY PROPOSALS:
- [proposed addition 1]
- [proposed addition 2]
(Reply "save" to apply, or skip)
```

If nothing qualifies, skip silently.

### 7. Quick drift check

Run `git log --oneline --all --since="6 hours ago"` to check for commits from other sessions.

- If any commits touched files that were also edited in this session, flag the potential conflict to the user.
- If no parallel commits are found, skip silently — do not mention this step.
- This is a fast check, not a full `/reconcile`.

### 8. Confirm with user
Show a brief summary: "Session logged. State updated. Next priorities: [X, Y, Z]." If any memory proposals are awaiting a save/skip reply, note that.
