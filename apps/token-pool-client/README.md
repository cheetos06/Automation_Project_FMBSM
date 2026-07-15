# Token Pool Client

The Token Pool Client is a standalone Windows desktop app. Colleagues do not need
Git, Python, Node.js, or administrator rights; Microsoft Edge is the only external
requirement.

On first use, **Add Microsoft account** opens an isolated Edge profile. The colleague
completes Microsoft sign-in/MFA once, and the app captures the account's Copilot
upload/websocket session. On later starts it silently rotates the saved OAuth refresh
token, encrypts the renewed session to the pinned server certificate, HMAC-signs the
request, and uploads it through the corporate proxy. It then shows the AWS pool's
accounts, turn totals, and cooldown state. The upload key is never sent on the wire.

The launcher checks only GitHub releases whose tag begins with `token-client-v`.
Changes to any other app/service in this monorepo do not trigger a client update.
Downloaded ZIPs are SHA-256 verified and installed under `%LOCALAPPDATA%`; the prior
working version remains available if an update fails.

## Install and update performance

The visible executable is about 3.45 MB. The first install is larger because account
onboarding needs an embedded Python/Tk/cryptography runtime and Playwright's Node
driver (about 52 MB compressed). That runtime is downloaded once in concurrent,
proxy-safe parts from the AWS mirror, with GitHub as an automatic fallback. Windows'
native ZIP extractor is used when available.

Normal releases publish a separate app layer of about 3.31 MB. The launcher verifies
that layer, connects it to the cached runtime with a directory junction, and retains
the previous version for rollback. Exact dependency pins and a normalized runtime
fingerprint prevent build timestamps from causing unnecessary full downloads. The
exact public installer completed a clean office-network installation in 2m22s versus
the prior 10–13 minutes; the installed v1.0.5 launcher automatically updated to
v1.0.7 in 8.04s without downloading the runtime. A no-change check does not download
an asset.

Production builds also validate `runtime-compatibility.json` against the exact Python
version, requirements files, and PyInstaller spec. A real runtime change therefore
fails the release build until compatibility is reviewed explicitly; source-only app
changes keep using the cached runtime.

The AWS mirror is only a speed layer: GitHub remains the version authority, and every
mirrored package must match the SHA-256 value from the GitHub release manifest before
activation. Progress and fallback details are written to
`%LOCALAPPDATA%\FMBSM\TokenPoolClient\launcher.log`.

Install without Git or administrator rights:

```powershell
irm https://raw.githubusercontent.com/cheetos06/Automation_Project_FMBSM/main/apps/token-pool-client/installer/Install-TokenPoolClient.ps1 | iex
```

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
python -m token_pool_client
```

Build the standalone package with a local, uncommitted `client-config.json` and
`server.crt`:

```powershell
.\build.ps1 -Version 1.0.0
```

For migration/testing on the original OPTIMDA machine:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m token_pool_client --legacy-build2 "C:\path\to\OPTIMDA\Build 2"
```
