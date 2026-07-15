# Operations

## Live progress

From Windows, run:

```powershell
.\scripts\watch-server.ps1
```

It follows both systemd services. Each FS job also has:

```text
/opt/fmbsm-automation/data/mail/state/job-status/<job-id>.json
/opt/fmbsm-automation/data/mail/state/job-status/<job-id>.events.jsonl
```

The authenticated token API `GET /v1/status` exposes privacy-masked pool identities,
turns, expiry, and cooldowns to the desktop app.

## Recovery

- Mail worker restart: in-progress email records become `interrupted` and retry, up to
  the configured attempt limit.
- FS subprocess failure/timeout: the job is moved to `failed`, diagnostic logs are kept,
  and the sender receives an error response.
- Token/API restart: immutable session versions and SQLite state persist under `data/`.
- Failed desktop update: the launcher logs the error and starts the prior version.
- Account needs MFA: other accounts continue; run the desktop app and add/refresh that
  Microsoft account again.

## Deploy

Run `services/automation-mail-worker/deploy.sh` from Git Bash/WSL. It preserves server
data and the existing upload key, adds swap on the small Lightsail instance, provisions
the pinned IP TLS certificate, installs dependencies, compiles Python source, restarts
the mail worker and both API transports, and performs local HTTP/HTTPS health checks.
