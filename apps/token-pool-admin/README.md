# FMBSM Token Pool Admin

Private Windows control center for the FMBSM desktop token contributors and AWS Copilot pool.

The administrator app is a separate executable from Token Pool Client. It is not included in the public colleague package and uses a dedicated `TOKEN_ADMIN_KEY` that must never be committed or bundled with the normal client.

Features:

- live online/offline client presence and installed versions;
- audited force-renew commands with `silent_only` or `allow_visible` interaction policy;
- remote update checks against the approved GitHub release;
- Copilot account availability, expiry, cooldown, and turn counts;
- selected/all real Copilot runtime health tests;
- AWS host, process, disk, memory, and systemd service status;
- command expiry, cancellation, results, and 90-day connectivity history.

Build locally:

```powershell
.\Initialize-AdminConfig.ps1
.\build.ps1 -Version 1.0.0-local -ConfigurationPath .\admin-config.json
```

The initializer creates a 256-bit administrator credential without printing it.
The private `admin-config.json` and server certificate are excluded from Git. The
same credential must later be installed as `TOKEN_ADMIN_KEY` on AWS during the
approved deployment; until then, the local admin app correctly reports that the
live admin API is unavailable.
