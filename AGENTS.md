# Context OS

This repository is the durable source of truth for personal, project, and
session context. Keep provider-neutral state here and host-specific behavior in
its adapter directory.

## Session lifecycle

- First-time onboarding: use `$setup`.
- Begin work: use `$start`.
- Save a mid-session checkpoint: use `$update`.
- Close a session: use `$end`.

These workflows require explicit invocation. The `$context-setup`,
`$context-start`, `$context-update`, and `$context-end` compatibility names
remain supported.

## Lifecycle kernel

- `bash scripts/contextos.sh start` is the read-only continuity inventory.
- Setup, update, and end use `propose` then exact-digest `apply`; never edit
  lifecycle state directly.
- Present every proposal diff. Apply only after explicit approval of that exact
  proposal and report its receipt.
- Run `bash scripts/contextos.sh doctor` when discovery, runtime setup, a lock, or
  copied skills may be stale.

## Context routing

- Read `ROUTING.md` before loading task-specific context.
- Treat `TODO.md` as the backlog and `state/current.md` as top-of-mind context.
- Keep each fact in one canonical file and link to it elsewhere.
- Load only what the current task needs. Identity and session data may be sensitive.

## Safety

- Follow `docs/safety-contract.md` before any external write, publish,
  destructive action, credential change, or permission expansion.
- Show proposed context changes before broad or destructive rewrites.
- Never commit or push without explicit approval.
- Optional integrations stay disabled until chosen and configured. Review
  `references/integrations.md` for data and side-effect boundaries.

## Portability boundary

- `.agents/skills/` contains portable workflow cores.
- `.claude/` contains Claude Code commands, hooks, settings, and memory adapters.
- `.codex/hooks.json` maps Codex events to the same read-only policy checks.
- `adapters/hermes/` documents optional Hermes hooks and skill installation.
- `adapters/openclaw/` documents experimental skills-first OpenClaw support.
- `adapters/cursor/` documents separate experimental Cursor IDE and CLI support.
- `adapters/devin/` documents experimental Devin cloud-session and Review support.
- Runtime manifests under `runtimes/` declare support instead of implying parity.
- Kernel proposal/apply is the enforcement boundary on every host; hooks are
  defense in depth and host-local memory is never shared automatically.

## Hermes Agent

- Hermes loads `AGENTS.md` as project context.
- Expose `.agents/skills/` as an external skill directory, or install the four
  short aliases and all four `context-*` cores together. Copied skills must be
  refreshed after source changes.
- Invoke `/setup`, `/start`, `/update`, and `/end` explicitly.
- Keep Hermes `MEMORY.md` and `USER.md` separate from repository state. See
  `docs/memory-across-agents.md`.
- `.claude/hooks/` does not run under Hermes. The optional Hermes adapter maps
  supported events to portable checks; kernel enforcement does not depend on it.

## OpenClaw

- Keep OpenClaw's private workspace and native memory outside this repository.
- Copy all four short lifecycle aliases and all four `context-*` cores into the
  private workspace's `.agents/skills/`; refresh copied skills after changes.
- Run OpenClaw from the repository directory so root `AGENTS.md` is included,
  then invoke `/skill setup`, `/skill start`, `/skill update`, or `/skill end`.
- This experimental adapter installs no OpenClaw hook or plugin. Skill
  allowlists do not replace separate shell-execution authorization.
- See `adapters/openclaw/README.md` for the tested version and diagnostics.

## Cursor

- Open the repository as the IDE workspace or start the Agent CLI from its root;
  both discover `AGENTS.md` and `.agents/skills/` natively.
- Invoke `/context-setup`, `/context-start`, `/context-update`, and
  `/context-end` explicitly; Cursor CLI reserves `/update` for self-updates.
- Keep `.cursor/rules` non-overlapping with this file because Cursor does not
  document their conflict order. IDE and CLI permissions remain separate.
- Cursor CLI also reads the removable root `CLAUDE.md` when present. Treat its
  short lifecycle commands as Claude adapters, not Cursor invocations.
- This experimental adapter ships no Cursor hook or native-memory bridge. See
  `adapters/cursor/README.md` for authorization and conformance boundaries.

## Validation

Run `bash scripts/validate-all.sh --workspace` after changing personal context
or adding workspace-owned skills and commands. Product contributors and CI run
the strict form without `--workspace` after changing instructions, hooks,
scripts, manifests, or generated references.
