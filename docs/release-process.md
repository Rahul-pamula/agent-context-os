# Release process

Context OS releases one canonical full-template artifact: a deterministic tar of
the complete non-development component closure. GitHub's automatically generated
source archives are convenient repository snapshots, but they are not the
canonical template artifact.

## Assets and identity

For version `X.Y.Z`, the immutable release contains exactly:

- `agent-context-os-template-vX.Y.Z.tar` — deterministic uncompressed USTAR;
- `agent-context-os-template-vX.Y.Z.bundle.lock.json` — detached bundle lock;
- `agent-context-os-template-vX.Y.Z.provenance.json` — outer identity and digest
  binding;
- `agent-context-os-template-vX.Y.Z.OFFLINE-VERIFY.md` — version-specific offline
  verification instructions; and
- `SHA256SUMS` — SHA-256 values for the other four assets.

The archive contains exactly the paths recorded by the lock, beneath one
`agent-context-os-template-vX.Y.Z/` root. Development files, including CI,
tests, release tooling, the changelog, and contributor-only documentation, are
excluded. File order is the lock's portable path order. UIDs and GIDs are zero,
owner and group names are empty, modes are `0755` only for locked executable
files and `0644` otherwise, and every mtime is the source commit epoch.

The provenance binds the repository, anticipated tag, exact commit, template
identity, archive identity, whole lock-file digest, internal `bundle_sha256`,
instructions digest, generator version, source mode, and fixed epoch. It is
deterministic and deliberately excludes runner names, workflow IDs, temporary
paths, and wall-clock generation times.

## Qualification and publication

Only dispatch `.github/workflows/release.yml` from `main`, with the exact
reviewed 40-hex commit. Before dispatch:

1. merge the release-preparation PR after canonical validation, hosted Linux and
   Windows validation, and exact-SHA Claude plus free-Hermes reviews;
2. confirm the version constants, example workspace, and dated changelog agree;
3. enable GitHub immutable releases for the repository; and
4. confirm the release tag and release do not already exist.

The workflow uses the checked-in [v0.12.0 release notes](releases/v0.12.0.md) as
the immutable published description.

The workflow then fails closed through these gates:

1. require the input commit to equal both checked-out `HEAD` and `origin/main`,
   require a completely clean source, and require immutable releases enabled;
2. run the canonical validator on that exact source;
3. build twice from its Linux Git index and compare every artifact byte;
4. verify the same Actions candidate in separate Linux and Windows extraction
   jobs, including execution of `python -m contextos bundle check` from the
   extracted archive;
5. only after both candidate jobs pass, create or confirm the exact lightweight
   tag through the GitHub API and stage all five assets in an unpublished draft;
6. download those staged release assets—not the Actions copies—and verify them
   again in separate Linux and Windows directories; and
7. only after both staged-asset jobs pass, recheck `main`, tag, draft state,
   exact asset names, and immutability policy, publish the draft, and require
   GitHub's immutable release attestation to verify.

Linux must report executable-mode verification as `true`. Windows must report
it as `false`, because directory sources there do not expose portable POSIX
executable bits. That is an explicit limitation, not a skipped success.

## Failure policy

- Before the tag exists, publish nothing; retain bounded Actions diagnostics and
  rerun only against the same commit.
- After the tag exists, never move or force-replace it. A failed staged release
  remains a draft for inspection. A rerun may resume only when both the tag and
  every downloaded draft asset are byte-identical to the same candidate; do not
  overwrite assets or delete forensic evidence automatically.
- If deterministic source or artifact verification fails after the tag exists,
  retire that version and fix the problem in a patch release.
- After publication, tags and assets are immutable. Correct errors with a notice
  and a new patch release, never by replacing bytes.

Consumers should follow the version-specific offline instructions and obtain
the expected digest through a channel they trust. Co-located checksums prove
consistency; GitHub's immutable-release attestation and independently observed
release identity provide publisher provenance.
