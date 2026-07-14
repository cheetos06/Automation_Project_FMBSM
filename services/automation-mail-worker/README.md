# Automation Mail Worker

This AWS service owns the Gmail polling loop, job dispatch, shared Copilot account pool,
and two session-upload transports: pinned HTTPS and proxy-compatible encrypted HTTP.

## Email jobs

- `[optimda-extract-dates]` extracts PDF signature dates and signers.
- `[fs-review]` runs the financial-statement review framework. Attach
  `financial_statements_N.pdf`, optional `financial_statements_N_1.pdf`, and
  `bg_standardized.xlsx`. Add `year=2025` to the subject to override the default year.

Every accepted request receives an acknowledgement with a job ID. FS progress is
written every 20 seconds to `data/mail/state/job-status/<job-id>.json` and to the
systemd journal. The final FS outputs and diagnostics are returned in one ZIP.

## Shared Copilot pool

The token API installs immutable, validated session versions beneath
`data/copilot/accounts/`. SQLite tracks uploads, rolling one-hour turns, cooldowns,
and throttle failures across jobs and process restarts. The server refreshes access
tokens from an uploaded refresh token before expiry; accounts requiring MFA are
skipped until a colleague runs the desktop token app again.

## Deploy

Create `.env` from `.env.example`, preserve the existing Gmail App Password, then run
`bash deploy.sh` from Git Bash or WSL. The script adds a 2 GB swap file when needed,
creates a long-lived IP-address TLS certificate, installs the three systemd services,
and runs HTTP/HTTPS health checks before restarting the mail worker.

Useful commands:

```bash
sudo journalctl -u fmbsm-email-bot -u fmbsm-token-api -u fmbsm-token-api-http -f
curl --cacert /opt/fmbsm-automation/data/tls/server.crt https://35.180.210.11/health
curl http://127.0.0.1/health
```
