# FMBSM Automation Project

This repository is a monorepo: it can contain multiple independent desktop apps,
AWS services, and shared packages without making one app "the main project."

## Current projects

| Area | Project | Purpose |
| --- | --- | --- |
| `apps/` | `token-pool-client` | Standalone Windows app that refreshes and uploads Copilot sessions; no Git/Python/admin rights required. |
| `services/` | `automation-mail-worker` | AWS Gmail worker, shared Copilot pool/runtime, signature extraction, and FS review. |
| `scripts/` | Operations tools | Live AWS logs and repeatable end-to-end email tests. |
| `docs/` | Architecture/operations | Deployment, recovery, and repository workflow documentation. |

Source dossiers, generated workbooks/PDFs, browser profiles, tokens, credentials, and
server data are intentionally excluded from Git.

## Install the Token Pool Client

Once the repository and first `token-client-v*` release are public, colleagues can
run this one command in a normal (non-admin) PowerShell window:

```powershell
irm https://raw.githubusercontent.com/cheetos06/Automation_Project_FMBSM/main/apps/token-pool-client/installer/Install-TokenPoolClient.ps1 | iex
```

The installer creates a Start Menu shortcut under **FMBSM**. At every launch it checks
only Token Pool Client releases, downloads an update only when that app changed, and
verifies the release SHA-256 before starting it.

## Safe Git workflow

`main` is the production branch. New work should use a short-lived branch:

```text
feature/add-new-job
fix/email-timeout
chore/update-documentation
```

Open a pull request, let CI run, test the branch, then merge it. App releases are
path-scoped: changing an AWS service does not release or update the desktop client.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the practical workflow.
