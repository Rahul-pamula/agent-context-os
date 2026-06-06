---
name: reconcile
description: Tripwire check for multi-session drift. NOT read-only — step 1 runs `git pull --rebase` to sync the branch and surface collisions (a failed rebase is the signal), then scans state files, SSOT rules, and recent commits for parallel-session inconsistencies. Run after parallel work, or when something feels off.
allowed-tools: Read, Bash, Glob, Grep
x-source: skills-sync/commands/reconcile.md
x-source-version: c9b6c33
---

# /reconcile — Multi-Session Drift Check

Scan the repo for inconsistencies introduced by parallel Claude Code sessions (especially with worktrees). Read-only tripwire — it flags problems but doesn't fix them without approval.

> This is the generic core. A consuming repo with single-source-of-truth rules (e.g. a pipeline file that other files reference) adds those specific cross-checks as its own overlay — see step 4.

## Instructions

### 0. Orientation (run FIRST)

Scope the check before reading any files:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
find "$REPO_ROOT" -name "*.md" -mtime -1 -not -path "*/node_modules/*" | sort   # recently modified
git -C "$REPO_ROOT" log --oneline -10
```

Focus the reconcile on files that actually changed recently.

### 1. Pull latest

```bash
cd "$REPO_ROOT" && git pull --rebase 2>&1 || echo "pull failed — check for conflicts"
```

If pull fails with conflicts, stop and report them — that's the #1 signal of a parallel-session collision.

### 2. Uncommitted / cross-session changes

```bash
git status --short
git stash list
```

Flag unstaged changes you didn't make ("likely from another session — review before proceeding") and any forgotten stashes.

### 3. Cross-branch scan + file-level conflicts

```bash
git log --all --oneline --since="24 hours ago" --graph
```

Look for multiple branches touching the same files and unmerged branch commits. For each file modified on more than one branch, diff the versions:

```bash
git diff <default-branch>..<branch> -- <file>
```

Flag where both branches changed the same lines, one branch deleted what another modified, or `**Last Updated:**` fields diverged.

### 4. SSOT violations

If the project has single-source-of-truth rules (a fact lives in one file; others reference it):

- Scan for the same fact duplicated across files with **different values** (DUPLICATE)
- Flag a cross-reference that hardcoded a value instead of pointing at the source (STALE COPY)
- Confirm cross-references resolve to files that still exist — `bash scripts/check-links.sh` covers tracked markdown if present

A consuming repo lists its canonical-fact table here in its own overlay; the generic core only checks the *pattern*.

### 5. State-file consistency

Read the state files (e.g. `state/current.md`, `state/weekly-priorities.md`, `state/blockers.md`) and check:

- **Duplication** — an actionable task in `current.md` that should only live in `TODO.md`; the same item logged twice from different sessions
- **Contradiction** — one file marks something done while another still has it open
- **Timestamp drift** — `**Last Updated:**` older than the most recent commit touching that file
- **Orphaned references** — sections pointing at files or items another session removed

### 6. Report

```
RECONCILE — [DATE]

GIT: pull [clean/conflicts] · uncommitted [none/list] · branches [N, collisions?]
SSOT: [PASS / violations]
STATE: [PASS / issues]
OVERALL: [CLEAN / N issues found]
```

List each issue with a proposed fix. Wait for approval before changing anything.

### 7. Fix mode (with approval only)

On explicit "fix all" / "clean it up", apply the proposed fixes and commit:

```
reconcile: fix [N] drift issues from parallel sessions
```

## Design Principles

- **Read-only by default.** Never edit without explicit approval.
- **Trust recent over old.** When two versions conflict, the more recent edit is usually right.
- **Fast.** Targeted checks only — under 30 seconds. Don't deep-read every file.
- **Specific.** Every flag names the file, the line, and the conflict. "Something seems off" is not a flag.
- **No false alarms.** A cross-reference that correctly points at its source is fine — only flag real value mismatches or duplicated facts.
- **Complement `/recover`.** Reconcile checks *content* drift; `/recover` handles *structural* worktree and branch cleanup.
