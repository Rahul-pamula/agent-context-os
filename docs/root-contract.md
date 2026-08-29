# Root contract

**Status:** Accepted for v0.12 compatibility mode; distinct-root execution is
reserved for the external-project attachment milestone.

Context OS uses three root roles. The normal v0.12 full-template wrapper path
colocates them; a core-only JSON ContextRoot may instead run through an already
loaded trusted kernel installation. Their ownership and authority are defined
separately now so a later attachment flow does not have to reinterpret existing
proposals, receipts, or safety claims.

## Root roles

| Role | Owns | Lifecycle access |
|---|---|---|
| **KernelRoot** | Authoritative executable product assets: `contextos/`, executable wrappers, runtime and component manifests, schemas, and immutable bundle sources and locks | Read product authority and execute the kernel. Lifecycle setup/update/end never mutate these KernelRoot-owned authority paths. |
| **ContextRoot** | Tracked workspace intent, materialized repository instructions and portable skill bodies, and durable context: routing, identity, projects, state, sessions, and tasks; plus local `.context-os/` inputs, proposals, locks, staging, journals, receipts, host state, and installed-bundle state | The only mutation authority for lifecycle setup/update/end. Transaction targets and journal entries are ContextRoot-relative. |
| **WorkingRoot** | The nominal application working directory whose files describe the work being performed. In v0.12 this is the discovered root; its containing Git repository may be an ancestor. | Application-owned paths are read-only evidence for lifecycle. When roles are colocated, lifecycle may mutate only paths authorized as ContextRoot content; other application edits remain ordinary host/tool actions outside proposal/apply. |

Role ownership is stronger than physical containment. In a future attachment
mode, the canonical roots must be distinct and non-overlapping: none may be
nested beneath another. v0.12 permits the full-template colocation below and a
CLI-only core workspace to use the already loaded trusted kernel without
turning that installation into ContextRoot or WorkingRoot authority.
Component policy and root role answer different questions: `managed` records
bundle upgrade/customization policy, not runtime immutability. In colocated
v0.12, materialized `AGENTS.md`, `CLAUDE.md`, and `.agents/skills/**` are
ContextRoot instruction paths that the explicitly reviewed setup allowlist may
personalize even though the component manifest also manages their upgrade
provenance. Kernel code, wrappers, schemas, manifests, and bundle authority are
not on that setup allowlist.

The v0.12 setup allowlist is explicit: root files `ROUTING.md`, `TODO.md`,
`CLAUDE.md`, and `AGENTS.md`; content beneath `identity/`, `projects/`, and
`state/`; and portable skills beneath `.agents/skills/`. Update and end use the
configured state/session paths subject to the product-authority guard below.

## v0.12 compatibility decision

Every supported v0.12 workspace binds lifecycle state and nominal work to one
discovered path:

```text
ContextRoot == nominal WorkingRoot == canonical discovered root
```

On the normal full-template path, `scripts/contextos.sh` loads the kernel from
that same root, so `KernelRoot == ContextRoot` as well. A core-only JSON
workspace is CLI-discoverable without product files; there, KernelRoot is the
origin of the already loaded trusted `contextos` package and is not discovered
from `--root`. v0.12 exposes no KernelRoot path field. This executable-source
exception is not external application attachment and grants no lifecycle write
authority outside ContextRoot.

`GitEvidenceScope` is a documentation-only evidence source, not a fourth
authority root or a v0.12 path/identity field. It is the nearest valid containing
Git worktree, when one exists, used only for existing commit evidence. It is
absent when the nearest valid containing worktree has no existing commit, or
when none exists; evidence never falls outward past a nearer worktree. It
normally equals the discovered root, but an intentionally nested ContextRoot
makes it an ancestor of the nominal WorkingRoot. That compatibility case does
not authorize lifecycle mutation outside ContextRoot.

The existing `--root` option supplies the starting path for ContextRoot and
nominal WorkingRoot discovery; it does not require its argument itself to be
the root. Discovery
ascends from that path to the nearest valid `contextos.workspace.json` or legacy
compound marker and stops at a nested `.git` boundary. Without `--root`, the
starting path is process cwd. The shell wrapper resolves the repository root
from its wrapper directory and changes there before running the kernel. In the
v0.12 full-template colocated mode, host lifecycle skills require the exact
host-supplied directory containing
`AGENTS.md` and `scripts/contextos.sh`. That is an adapter heuristic, not
ContextRoot discovery: a core-only JSON workspace remains CLI-discoverable
without those files, and split mode must replace the heuristic. Future
split-mode role options must identify their exact role roots and must not inherit
this upward-search compatibility behavior.

Consequences that must be stated rather than inferred:

- `start.git_head`, proposal `source_git_head`, and receipt Git evidence all
  describe `GitEvidenceScope`, the nearest containing Git worktree. A legacy or
  non-top-level ContextRoot may therefore report an enclosing
  Git worktree's HEAD; the nominal WorkingRoot remains the discovered path and
  v0.12 exposes neither the evidence-scope path nor a second mutation authority.
  A new enclosing HEAD therefore invalidates proposal commit evidence even when
  the commit did not touch ContextRoot; these fields do not represent
  uncommitted worktree status.
- Starting an agent in an unrelated application repository and reaching into a
  separate Context OS repository is not a supported v0.12 lifecycle path.
- When supplied, `--root` is the sole discovery start. A relative value resolves
  against process cwd like any path argument; Context OS never falls back to
  cwd, a skill installation directory, or an installed-package location.
- External attachment requires versioned binding, evidence, and receipt
  contracts; it cannot silently change the meaning of v0.12 fields.

The release therefore publishes the trustworthy full-template, full-closure,
colocated mode. It does not claim external project attachment or slim
composition.

## Resolution and canonicalization

Before v0.12 release, every public lifecycle/report entrypoint must canonicalize
its root exactly once before using it for containment or identity checks.

- In v0.12, CLI discovery returns the canonical ContextRoot and nominal
  WorkingRoot; full-template wrapper execution also loads KernelRoot there.
- Public Python lifecycle/report functions that accept a raw root path must
  normalize it to the same canonical ContextRoot spelling before workspace
  resolution. Direct callers and CLI callers are one contract, not separate
  safety tiers. Issue #113 implements and tests this release requirement.
- Strict lifecycle reads and hooks fail closed when a path changes identity or
  becomes link-like after canonicalization. `doctor` remains diagnostic: a
  late race produces a structured `unknown`/warning result rather than a
  traceback or a followed link.
- Discovery never falls through an invalid nearer marker or climbs past a
  nested Git boundary to capture an outer ContextRoot.

In split mode, KernelRoot will come from the trusted installation or wrapper,
ContextRoot from an exact explicit option or validated local binding, and
WorkingRoot from an exact explicit host working directory or argument. In that
mode, KernelRoot location and process cwd must never be implicit authority for
ContextRoot. The v0.12 cwd-based discovery compatibility described above is the
deliberate exception.

## Legacy linked-path boundary

v0.12 preserves the explicitly tested pre-JSON behavior for an internal linked
`state_dir`, both with legacy `workspace.yaml` and with no tracked configuration:
readiness may read the resolved internal target, and update/end proposal/apply
uses that resolved internal path rather than the link spelling. Migration and
activation continue to reject the link. This is a compatibility exception, not
part of the canonical no-follow guarantee.

`doctor` must identify the linked pre-JSON path, scope the exception, and direct
the owner to migrate. Canonical JSON workspaces continue to reject linked or
reparse-point state paths for `start`, hooks, and diagnostics. A future
distinct-root mode requires canonical tracked configuration and does not inherit
the exception. Issue #112 owns the diagnostic and control work.

## Future attachment binding

External attachment will keep stable project identity separate from
machine-specific location:

- ContextRoot stores a schema-versioned tracked project identity. It never
  stores an absolute KernelRoot or WorkingRoot path.
- Ignored local state beneath `ContextRoot/.context-os/` maps that identity to
  canonical machine-local KernelRoot and WorkingRoot paths plus observed
  repository identity.
- The first attachment slice adds no tracked pointer or Context OS lifecycle
  file to WorkingRoot.
- Moving a repository requires explicit rebinding after identity validation.
  A path match, remote-name match, cwd, symlink, or copied marker is not
  authorization.
- A missing, stale, or mismatched binding makes strict lifecycle operations
  fail before proposal or mutation. Read-only diagnostics may report it as
  unavailable or stale.

The exact binding schema and CLI belong to the external-project attachment
milestone, not v0.12.

## Write and evidence boundaries

Lifecycle setup/update/end obey these invariants:

1. Proposal, lock, staging, journal, receipt, and every target path are owned by
   ContextRoot. Host skills conventionally place reviewed input beneath
   `ContextRoot/.context-os/inputs/`; the CLI also accepts an explicit input
   file elsewhere, whose bytes are read but whose location is not mutation
   authority.
2. Journal and receipt target paths are relative to ContextRoot; recovery can
   restore only ContextRoot targets.
3. WorkingRoot state may be bound as read-only evidence, but it never grants
   mutation authority and never replaces ContextRoot target hashes.
4. KernelRoot product assets supply trusted code and schemas; ContextRoot
   content cannot shadow them to widen authorization.
5. Materializing a bundle is a separately named installation/composition
   boundary with an explicit reviewed destination. It is not a setup/update/end
   lifecycle write to an attached WorkingRoot.

For both canonical JSON and pre-JSON compatibility configuration, update/end
proposal publication, apply, and recovery reject configured state or session
targets whose first path component is a product-authority namespace. Config
readability therefore cannot widen lifecycle mutation authority.

The v0.12 protected namespaces are `.agents/`, `.claude/`, `.codex/`,
`.cursor/`, `.github/`, `adapters/`, `bundles/`, `components/`, `contextos/`,
`integrations/`, `runtimes/`, `scripts/`, and `workspace/`. This update/end
guard includes extensible host instruction surfaces; it does not make those
paths KernelRoot-owned or remove setup's narrower `.agents/skills/` authority.

When split mode ships, receipts and reports must use role-qualified identities
and Git fields. The ambiguous v0.12 `git_head` fields cannot silently acquire a
second meaning.

## Required controls

The later root split must retain all v0.12 controls and add distinct-root
fixtures. Each row names a must-fire behavior and its must-not-fire complement.

| Surface | Must fire | Must not fire |
|---|---|---|
| Compatibility | `--root R` starts discovery at `R`, resolves ContextRoot and the nominal WorkingRoot to the nearest canonical root, keeps full-template wrapper KernelRoot colocated while permitting a trusted already-loaded kernel for core-only CLI use, attributes existing Git commit fields to `GitEvidenceScope`, and keeps existing commands/receipts valid | A supplied discovery start falls back to cwd or installed product paths; an installed KernelRoot gains ContextRoot authority; an enclosing Git worktree gains lifecycle mutation authority or is presented as ContextRoot-authored work; future exact role options search upward |
| Context discovery | Nearest valid marker wins | Invalid inner marker falls outward, or discovery crosses nested `.git` |
| Canonicalization | Equivalent permitted entrypoints produce one canonical identity for CLI and direct API calls | Link/reparse swaps or alias changes redirect a role after validation |
| Start | Reads ContextRoot continuity and, in split mode, separately reports WorkingRoot identity/status/history | Writes any root, reads context state from WorkingRoot, or presents KernelRoot commits as user work |
| Propose | Publishes reviewed proposal state only beneath ContextRoot | Absolute, escaping, unresolved link-like, canonical-config linked, KernelRoot, or WorkingRoot targets reach publication; the documented pre-JSON internal-link exception is resolved before target calculation |
| Apply/recovery | Changes and restores only ContextRoot-relative targets under the existing digest, lock, journal, and receipt protocol | Crafted or legacy artifacts cause KernelRoot/WorkingRoot mutation |
| Runtime registration | Reads runtime/component authority from KernelRoot and records local state beneath ContextRoot | Installation modifies WorkingRoot or lets ContextRoot shadow product authority |
| Doctor | Diagnoses each role independently and stays total across late races | Executes runtime probes, follows links, mutates a root, or hides an unexecuted check as green |
| Hooks | Resolves protected ContextRoot paths against the declared role | Treats an ordinary WorkingRoot-relative source path as ContextRoot-relative |
| Binding | Explicit identity match permits a local binding and explicit rebind handles a move | cwd, copied marker, stale path, or matching remote name alone authorizes use |
| Root isolation | Split-mode lifecycle succeeds with a read-only KernelRoot and unchanged WorkingRoot snapshot | Any successful or rejected lifecycle path changes KernelRoot or WorkingRoot bytes, modes, or tree shape |

## Implementation surface inventory

Distinct-root work must update these surfaces together rather than creating a
second ad hoc root path:

- `contextos/cli.py`: unambiguous role options; `--root` remains the v0.12
  colocated compatibility form.
- `contextos/kernel.py`: typed root resolution; ContextRoot workspace and
  transaction guards; role-qualified Git evidence, reports, hooks, receipts,
  and recovery.
- `scripts/contextos.sh` and hook wrappers: load KernelRoot assets without
  replacing the caller's WorkingRoot or discovering ContextRoot from cwd.
- `.agents/skills/context-*` and runtime adapters: execute in WorkingRoot while
  invoking an explicit/bound ContextRoot through KernelRoot.
- `adapters/openclaw/plugin/lib.js`: replace its colocated root alias with exact
  ContextRoot and WorkingRoot bindings while preserving ownership,
  continuation, and no-plugin-apply boundaries.
- runtime/component/bundle validation: read product authority from KernelRoot;
  never from writable context or application content.
- materialization: distinguish creation/reconciliation of a ContextRoot from
  lifecycle activity inside an attached application repository.

Issue #116 owns that implementation and its Windows/Linux Claude-to-Codex
golden path. Issue #118 owns later desired-composition schema work and is not a
v0.12 dependency except for the bounded verification task tracked separately in
#106.
