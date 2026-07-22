# Automation Mail Worker

This AWS service owns the Gmail polling loop, job dispatch, shared Copilot account pool,
and two session-upload transports: pinned HTTPS and proxy-compatible encrypted HTTP.

## Email jobs

- `[optimda-extract-dates]` extracts PDF signature dates and signers.
- `[optimda-effectif]` extracts only effectif and payroll evidence (charges de
  personnel, salaires et traitements, charges sociales, and equivalents) into one
  Excel workbook. A failed PDF is recorded as an error row without stopping the job.
- `[fs-review]` runs the financial-statement review framework. Attach
  `financial_statements_N.pdf`, optional `financial_statements_N_1.pdf`, and
  `bg_standardized.xlsx`. Add `year=2025` to the subject to override the default year.

Every accepted request is first saved as an RFC 822 file and entered in the durable
FIFO queue before its Gmail message is marked read. Its acknowledgement includes the
job ID, queue position, and an estimate based on completed jobs. The polling loop
continues accepting mail while a job runs, and a second notice is sent when a queued
job starts. FS progress is written every 20 seconds to
`data/mail/state/job-status/<job-id>.json` and to the systemd journal. The final FS
outputs and diagnostics are returned in one ZIP.

Queue state is persisted in `data/mail/state/processed.json`; queued and interrupted
jobs are replayed after a process or server restart. Copilot page and review-batch
responses are cached inside the job so a retry resumes completed calls. Transient
mail, network, and Copilot failures are retried, while malformed inputs fail once and
are archived with a clear email response. Result delivery is recorded before the job
is archived to avoid sending a duplicate result after a crash.

`MAX_QUEUED_JOBS` limits backlog, `MIN_FREE_DISK_BYTES` prevents accepting work when
storage is low, and `QUEUE_DEFAULT_*_SECONDS` supplies initial ETA estimates until
the queue has historical timings. `MAX_PROCESSING_ATTEMPTS` bounds restart/retry
loops. Keep `SEND_RETRY_NOTIFICATIONS=true` when senders should be told that a
transient failure was retained for retry.

The worker accepts trigger subjects only from exact addresses in
`AUTHORIZED_JOB_SENDERS` or exact domains in `AUTHORIZED_JOB_SENDER_DOMAINS`.
At least one allowlist must be configured or the worker refuses to start. Matching is
case-insensitive and exact: allowing `mazars.fr` does not allow a subdomain or a name
such as `mazars.fr.attacker.example`. Unauthorized matching messages are marked read
without starting a job or sending a reply.

## Shared Copilot pool

The token API installs immutable, validated session versions beneath
`data/copilot/accounts/`. SQLite tracks uploads, rolling one-hour turns, cooldowns,
and throttle failures across jobs and process restarts. The server refreshes access
tokens from an uploaded refresh token before expiry; accounts requiring MFA are
skipped until a colleague runs the desktop token app again.

Every upload must include a refresh token that Microsoft successfully exchanges for a
new Copilot access token. The returned token must belong to the same account, have the
expected Copilot audience/client, and use a tenant listed in
`COPILOT_ALLOWED_TENANT_IDS`. The pool also enforces `COPILOT_MAX_ACCOUNTS`, so a
publicly downloadable client credential is not the server's only trust boundary.

## Token client artifact mirror

The HTTP token service also serves immutable, versioned Token Pool Client assets from
`TOKEN_CLIENT_ARTIFACT_DIR`. It does not expose directory listings and accepts only
the expected `token-client-v*` paths and asset names. GitHub Actions uploads through
the separate `fmbsm-artifacts` account, which is chrooted to the artifact directory,
restricted to SFTP, and has no shell or forwarding access. The client obtains its
release manifest from GitHub and rejects mirrored bytes with the wrong SHA-256, so
the mirror cannot authorize an update by itself.

The release workflow requires repository secrets `TOKEN_ARTIFACT_SFTP_KEY` and
`TOKEN_ARTIFACT_SSH_HOST_KEY`. They are deployment-only credentials and are never
included in the desktop app.
Public artifact requests are limited per source IP by
`TOKEN_CLIENT_DOWNLOADS_PER_HOUR` (120 by default); clients fall back to GitHub if
the mirror is unavailable or rate-limited.

On a replacement server, configure the restricted account with the uploader's public
key before deploying the service:

```bash
sudo bash scripts/configure_artifact_sftp.sh 'ssh-ed25519 AAAA... github-actions-token-client-artifacts'
```

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
