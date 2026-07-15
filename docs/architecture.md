# Architecture

```text
Windows Token Pool Client
  -> signed + certificate-key-encrypted HTTP :80 (corporate-proxy compatible)
     or certificate-pinned HTTPS :443
  -> session bundle validator / immutable account version
  -> SQLite registry (uploads, expiry, turns, cooldowns)
  -> shared Copilot runtime
  -> FS review and future jobs

Gmail -> automation mail worker -> isolated job subprocess -> Gmail result
```

The desktop app performs the only browser-dependent step. A first-time account uses a
dedicated Edge profile to capture its Copilot UploadFile request, Chathub websocket
prompt template, Microsoft upload cookies, and OAuth refresh token. Subsequent starts
rotate the OAuth token without opening Edge unless Microsoft requires MFA again.

Before an HTTP upload, the app encrypts the complete session ZIP using a random
AES-256-GCM key and encrypts that key with the pinned server certificate (RSA-OAEP).
Requests are timestamped and HMAC-signed, so the persistent upload key is never sent
over the network. HTTPS on port 443 remains available where raw-IP TLS is permitted.

The server never needs a browser. Each decrypted upload is ZIP-bomb/path-traversal checked,
restricted to known session filenames, validated for the expected JSON shapes, and
identified from the token's tenant/object claims. A new immutable session directory is
installed before the SQLite pointer changes, so jobs never observe half an upload.

Every Copilot call reserves a turn in SQLite with `BEGIN IMMEDIATE`; counters therefore
survive restarts and remain correct if later services use the same pool. The 150th turn
is allowed to finish and then that account cools down. HTTP 429/throttle responses also
start a cooldown and requeue the request on another account.

FS review runs as an isolated, timeout-controlled subprocess. Its stdout is streamed to
the journal and per-job log, while a status JSON heartbeat is refreshed at least every
20 seconds. A native library crash or stuck Copilot request cannot silently freeze the
mail worker forever.
