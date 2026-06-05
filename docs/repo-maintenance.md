# Repo Maintenance

## Staleness Convention

Context files should have a `**Last Updated:**` line near the top. When using a file that is 90+ days old, flag it to the user so they can decide whether to update or archive it.

## Changelog

Whenever you add, remove, or significantly change a file, update `CHANGELOG.md` with a version entry listing every added/changed/removed file. Keep format consistent with existing entries.

## CLAUDE.md Length

Keep CLAUDE.md concise — detail belongs in skills, ROUTING.md, and docs, not in the root instruction file. Aim to stay under 100 lines.

## Validation

Run `scripts/validate-skills.sh` before pushing to check skill structure and catch formatting issues.

## Repo Map

`REPO_MAP.md` is a generated, **gitignored** artifact — handy as a single-file overview to paste into a claude.ai project, but it goes stale fast and isn't worth committing or gating in CI (the per-file date column makes it churn on every commit). Run `scripts/generate-repo-map.sh` on demand when you want a fresh local copy; don't commit it.
