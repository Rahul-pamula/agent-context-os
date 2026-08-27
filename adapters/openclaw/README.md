# OpenClaw experimental adapter

This adapter has been tested against `OpenClaw 2026.7.1-2 (0790d9f)`. It
provides skills-first lifecycle support. It does not claim OpenClaw project
hooks, automatic memory synchronization, or messaging/gateway conformance.

## Keep the two workspaces separate

Use this repository as the execution directory and a different, private
directory as OpenClaw's workspace. OpenClaw treats its workspace as private
memory, not as a sandbox. `SOUL.md`, `USER.md`, `MEMORY.md`, and `memory/`
belong there; do not add them to this repository merely to make OpenClaw work.

When OpenClaw executes from the repository, it appends the repository-root
`AGENTS.md` to its instructions. It does not load execution-directory
`SOUL.md`, `USER.md`, or `MEMORY.md` as workspace memory.

## Install the lifecycle skills

Copy these eight directories from the repository's `.agents/skills/` directory
to `<private-workspace>/.agents/skills/`:

- `setup` and `context-setup`
- `start` and `context-start`
- `update` and `context-update`
- `end` and `context-end`

Copy all eight together. The short names are aliases for the `context-*` cores,
and copied skills must be refreshed after their repository sources change.
The stable version tested here did not discover the same skills reliably via
`skills.load.extraDirs`, so this adapter does not recommend that shortcut.

OpenClaw's skill precedence puts `<workspace>/skills` ahead of
`<workspace>/.agents/skills`. A same-named skill in `skills/` can therefore
shadow a copied Context OS skill. Check the effective inventory after copying:

```bash
openclaw skills list --json
openclaw skills check --json
```

Run those inventory commands from the private workspace, not from the source
repository. All eight lifecycle skills should report source
`agents-skills-project`.

## Run the lifecycle

Start OpenClaw with the repository as its execution directory, while the
OpenClaw configuration still points at the separate private workspace. Invoke
the lifecycle explicitly:

```text
/skill setup
/skill start
/skill update
/skill end
```

Context OS proposal/apply remains the write-safety boundary. Review the exact
proposal and approve that digest before applying it.

## Boundaries and diagnostics

- OpenClaw skill allowlists control model and command visibility. They are not
  shell-execution authorization. Configure execution approvals separately. If
  an agent skill allowlist is present, include all eight lifecycle skill names
  or the omitted commands will not be visible to that agent.
- Workspace hooks are disabled until explicitly enabled. This experimental
  adapter installs no hook or plugin and makes no blocking-hook claim.
- Native OpenClaw memory is private host state and is not synchronized with
  repository state automatically.
- Use `openclaw doctor --lint --json` for a read-only native diagnostic. Exit 1
  can mean lint findings. Do not use `--fix` as a validation step because it can
  modify host state.
- Context OS `doctor` resolves descriptor probe binaries but never executes
  them. Native OpenClaw diagnostics remain an explicit operator action.

The executable conformance fixture is opt-in because it requires the exact
tested OpenClaw binary. Set `CONTEXTOS_OPENCLAW_BIN` to that executable and run
`python -m unittest tests.test_openclaw_conformance`.
