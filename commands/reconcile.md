---
name: reconcile
description: Scan for multi-session drift and SSOT violations. Run after parallel Claude Code sessions or when something feels off.
allowed-tools: Read, Glob, Grep, Bash
---

# /reconcile — Multi-Session Drift Check

Running multiple Claude Code sessions in parallel (especially with worktrees) lets files drift out of sync. This scans for inconsistencies and proposes fixes. Read-only by default.

## When to use
- After merging worktree branches back to the default branch
- When something "feels off" after parallel work
- After a crash where multiple sessions were open
- As a periodic sanity check during heavy parallel workflows

## Instructions

### 1. Scan recent history across all branches

```bash
git log --all --oneline --since="24 hours ago" --graph
git stash list
```

Look for: multiple branches touching the same files, unmerged branch commits, forgotten stashes, conflicting changes.

### 2. Check for file-level conflicts

For each file modified on more than one branch, diff the versions:

```bash
git diff <default-branch>..<branch> -- <file>
```

Flag where both branches changed the same lines, one branch deleted what another modified, or `**Last Updated:**` fields diverged.

### 3. Check state-file consistency

- **Duplicate entries** — the same task or decision logged twice from different sessions
- **Contradictory state** — `state/current.md` priorities vs `state/weekly-priorities.md`; a `TODO.md` task that contradicts `state/blockers.md`; one session marked something done while another added it in-progress
- **Timestamp drift** — `**Last Updated:**` dates that don't match the most recent actual edit
- **Orphaned references** — sections pointing at files or items removed in another session

### 4. Check SSOT violations

If the project has single-source-of-truth rules (a fact lives in one file; others reference it):

- Scan for the same fact duplicated across files with different values
- Confirm cross-references point to files that still exist (`bash scripts/check-links.sh` covers tracked markdown)

### 5. Report

```
RECONCILE — [DATE]

BRANCHES CHECKED:
- [branch — last commit date]

FILE CONFLICTS:
- [file] — modified on [branch1] and [branch2] — [conflict type]

STATE DRIFT:
- [issue]

SSOT VIOLATIONS:
- [duplicated fact] — in [file1] and [file2]

PROPOSED FIXES:
1. [fix]

OVERALL: [CLEAN / N issues found]
```

### 6. Apply fixes (with approval only)

Present each fix individually and wait for approval. Common fixes: keep the newer version of a conflicting file, remove duplicates (keep the more detailed one), correct timestamps to the actual last-edit date, or resolve an SSOT violation by keeping the canonical source and updating the references.

## Design Principles

- **Read-only by default.** Report, then wait for approval.
- **Trust recent over old.** When two versions conflict, the more recent edit is usually right.
- **Preserve intent.** Don't auto-resolve — different sessions may have had different goals.
- **Fast.** Git commands and file reads only — under 30 seconds.
- **Complement `/recover`.** Reconcile checks *content* drift; `/recover` handles *structural* worktree and branch cleanup.
