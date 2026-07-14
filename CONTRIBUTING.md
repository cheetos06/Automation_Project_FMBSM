# Contributing

## Branches

Keep `main` deployable. Create one branch per change from the latest `main`:

```powershell
git switch main
git pull
git switch -c feature/short-description
```

Use `feature/`, `fix/`, `chore/`, or `docs/` prefixes. Do not mix unrelated apps or
services in one pull request unless the change truly spans them.

## Pull requests

1. Run the relevant tests locally.
2. Commit only the intended files.
3. Push the branch and open a draft pull request while testing.
4. Mark it ready after the live or packaged test passes.
5. Merge to `main`; do not force-push `main`.

## Secrets and test data

Never commit `.env`, app passwords, upload keys, private certificates, OAuth/session
files, browser profiles, real client PDFs, workbooks, or generated outputs. The root
`.gitignore` blocks the known forms, and CI performs an additional secret scan.

## Adding another app/job

- New colleague-facing programs belong in `apps/<name>/`.
- New AWS workloads belong in `services/<name>/`.
- A new email job should plug into the automation worker's subject dispatcher and use
  the shared `copilot_service` registry/pool rather than introducing its own counters.
- Put code in `packages/` only when two independent projects truly import it.
