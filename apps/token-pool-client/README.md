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
