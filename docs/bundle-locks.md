# Immutable bundle locks and structural plans

Context OS bundle locks are detached, content-addressed descriptions of an
offline source tree. They make component planning reproducible without treating
a branch, tag, URL, or `latest` channel as immutable identity.

The lock's `bundle_sha256` covers the canonical `bundle` payload. That payload
binds the bundle name and exact version, supported schema/protocol versions, the
component manifest, every non-development component file's raw SHA-256 and
size, and its portable executable bit. The lock is detached and is not one of
its own payload files, so there is no self-hash exception.

Hash agreement proves integrity, not publisher authenticity. A consumer must
obtain the expected `bundle_sha256` out of band and pass it explicitly. A lock
and files found in the same untrusted directory are only self-consistent.

## Maintainer generation

Generate from the local Git index so line-ending conversion and host filesystem
modes cannot change release identity:

```bash
bash scripts/contextos.sh bundle generate \
  --source . \
  --name agent-context-os-template \
  --bundle-version 0.12.0
```

Generation prints JSON and does not write a lock. It rejects unclassified or
owned-but-untracked index paths. Release automation can publish the printed lock
beside an archive made from the same index.

## Offline verification

Verification requires both a local source and the independently obtained exact
digest. It performs no network lookup:

```bash
bash scripts/contextos.sh bundle check \
  --lock /path/to/contextos.bundle.lock.json \
  --source /path/to/extracted-bundle \
  --source-mode directory \
  --expect-sha256 <64-lowercase-hex-digest>
```

Every locked path is checked as a regular, non-link-like, singly linked file.
Raw bytes, sizes, portable path identity, the component graph, and executable
modes are verified. Windows directory sources cannot expose a meaningful POSIX
executable bit, so the report says that mode verification was unavailable;
Git-index verification checks it on every host.

## Read-only planning

`bundle plan` compares a verified candidate, an optional verified current
bundle, explicit current and desired component closures, and exact destination
and workspace-config snapshots. It emits sorted `add`, `replace`, `remove`,
`preserve-seed`, and `noop` actions plus a digest over every consulted input.

The planner rejects unavailable components, portable path aliases, symlinks or
reparse points, hard links, unowned target collisions, stale configuration,
cross-bundle component mixing, ownership/policy changes, and modified or missing
managed files. Existing or deleted seed files remain user-owned. The candidate,
current source, and destination must be explicit local paths, and source roots
must be separate from the destination.

Planning never writes. Export, materializing add/remove/upgrade, installed-lock
state, second pre-mutation checks, transaction journals, receipts, and rollback
belong to the materializer layer tracked separately in issue #72. Until that
layer exists, a structural plan is review evidence, not authorization to copy or
delete files.
